"""OaipayFlowHandler — OaiPay UPI no-CDK flow (`payments/upi_oaipay/`).

No license pool. Two HTTP clients: login proxied, OaiPay+captcha un-proxied.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Final
from urllib.parse import urlparse

from app.core import http_client as http
from app.core.payment_flow import (
    AccountLineError,
    Job,
    JobResult,
    JobStatus,
    ParsedAccount,
    PaymentFlowHandler,
)
from app.core.proxy_format import materialize_proxy
from app.core.proxy_pool import ProxyLease
from app.core.redaction import redact_message
from app.core.session_cache import AccountSessionCache
from app.core.settings_store import SettingsRepository
from app.payments._chatgpt.errors import ChatgptLoginError
from app.payments._chatgpt.login_client import ChatgptLoginClient
from app.payments._chatgpt.models import ChatgptAccount, parse_account_line as _parse_account_line
from app.payments._chatgpt.plan_status import check_plan_status as _check_plan_status_fn
from app.payments._chatgpt.session import (
    _safe_build_session_from_cache as _safe_build_session_from_cache_fn,
    resolve_session as _resolve_session_fn,
)
from app.payments.upi_oaipay.captcha import solve_turnstile_yescaptcha
from app.payments.upi_oaipay.errors import (
    AlreadyPaidError,
    CaptchaSolveError,
    ConfigError,
    OaipayFlowError,
    QrDecodeError,
    RunCancelledError,
    RunFailedError,
    RunTimeoutError,
)
from app.payments.upi_oaipay.models import LongLinkResult, TurnstileToken
from app.payments.upi_direct.network_safety import (
    _MAX_HTML_BYTES,
    extract_intent_state_from_hosted_html,
    fetch_bounded_bytes,
    is_allowed_hosted_instructions_url,
)

if TYPE_CHECKING:  # pragma: no cover
    from app.payments.upi_oaipay.oaipay_client import OaipayClient

_SETTING_MAX_CONCURRENT: Final[str] = "oaipay.max_concurrent"
_SETTING_BASE_URL: Final[str] = "oaipay.base_url"
_SETTING_PROXY_CHECKOUT: Final[str] = "oaipay.proxy_checkout"
_SETTING_PROXY_PROMOTION: Final[str] = "oaipay.proxy_promotion"
_SETTING_YESCAPTCHA: Final[str] = "oaipay.yescaptcha_api_key"
_SETTING_RUN_TIMEOUT: Final[str] = "oaipay.run_timeout_seconds"

_DEFAULT_BASE_URL: Final[str] = "https://oaipay.12001234.xyz"
_DEFAULT_RUN_TIMEOUT_SECONDS: Final[float] = 300.0
_DEFAULT_HTTP_TIMEOUT_SECONDS: Final[float] = 30.0
_QR_DATA_URI_PREFIX: Final[str] = "data:image/png;base64,"
_ACCOUNT_KEY_HEX_LENGTH: Final[int] = 32
_ALLOWED_HOSTS: Final[frozenset[str]] = frozenset({"oaipay.12001234.xyz"})
_PROXY_USERINFO_RE = re.compile(r"://([^:/@]+):([^@/]+)@")
# OaiPay retries "User is already paid" server-side; fail-fast as plan=plus
# so Free re-run export excludes the account (already Plus, no QR needed).
_ALREADY_PAID_MARKERS: Final[tuple[str, ...]] = (
    "user is already paid",
    "already paid",
)

_STOPPED_RESULT: Final[JobResult] = JobResult(status=JobStatus.STOPPED)
_PAUSED_RESULT: Final[JobResult] = JobResult(
    status=JobStatus.STOPPED, pause_requested=True
)


def _parse_vendor_expires_at(value: str | None) -> float | None:
    """Best-effort parse vendor ISO-ish timestamp to epoch seconds."""
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()

OaipayClientFactory = Callable[
    ["http.AsyncSession", str, list[str], logging.Logger], "OaipayClient"
]
CaptchaSolver = Callable[..., Any]


def _check_stop(job: Job) -> "JobResult | None":
    if job.cancellation_token.is_cancelled():
        return _STOPPED_RESULT
    return None


def _check_stop_or_pause(job: Job) -> "JobResult | None":
    if job.cancellation_token.is_cancelled():
        return _STOPPED_RESULT
    if job.cancellation_token.is_paused():
        return _PAUSED_RESULT
    return None


def _is_allowed_base_url(base_url: str) -> bool:
    try:
        parsed = urlparse(base_url.strip())
    except Exception:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    return parsed.hostname.lower() in _ALLOWED_HOSTS


def _proxy_userinfos(pool_lines: list[str]) -> list[str]:
    secrets: list[str] = []
    for line in pool_lines:
        match = _PROXY_USERINFO_RE.search(line)
        if match:
            secrets.append(match.group(2))
            secrets.append(f"{match.group(1)}:{match.group(2)}")
    return secrets


class OaipayFlowHandler:
    """Orchestrator for UPI no-CDK via OaiPay — implements PaymentFlowHandler."""

    def __init__(
        self,
        *,
        settings: SettingsRepository,
        session_cache: AccountSessionCache,
        qr_output_dir: Path,
        logger_factory: Callable[[str], logging.Logger],
        oaipay_client_factory: OaipayClientFactory,
        captcha_solver: CaptchaSolver | None = None,
    ) -> None:
        self._settings = settings
        self._session_cache = session_cache
        self._qr_output_dir = qr_output_dir
        self._logger_factory = logger_factory
        self._oaipay_client_factory = oaipay_client_factory
        self._captcha_solver = captcha_solver or solve_turnstile_yescaptcha

    def parse_account_line(self, line: str) -> ParsedAccount | AccountLineError:
        return _parse_account_line(line)

    def get_max_concurrent_key(self) -> str:
        return _SETTING_MAX_CONCURRENT

    async def check_plan_status(self, job: Job) -> dict[str, Any]:
        logger = self._logger_factory(f"oaipay.check_plan.{job.job_id}")
        parsed = self.parse_account_line(job.account_line)
        if isinstance(parsed, AccountLineError):
            return {"plan": "unknown", "error": f"invalid_account_line: {parsed.reason}"}
        # 1. Check Stripe hosted instructions URL if present on the job.
        stripe_intent_state: str | None = None
        if job.payment_link and is_allowed_hosted_instructions_url(job.payment_link):
            try:
                async with http.create_async_client(
                    allow_redirects=False,
                    timeout=_DEFAULT_HTTP_TIMEOUT_SECONDS,
                ) as stripe_client:
                    html_bytes = await fetch_bounded_bytes(
                        stripe_client,
                        job.payment_link,
                        max_bytes=_MAX_HTML_BYTES,
                    )
                    stripe_intent_state = extract_intent_state_from_hosted_html(html_bytes)
                    logger.info("stripe hosted check for %s: intent_state=%s", parsed.email, stripe_intent_state)
            except Exception as exc:
                logger.warning("stripe hosted check error for %s: %s", parsed.email, exc)

        account_key = self._compute_account_key(parsed.email)
        cached = await self._session_cache.get(account_key)
        session = _safe_build_session_from_cache_fn(cached.payload, logger) if cached else None

        client_kwargs: dict[str, Any] = {
            "allow_redirects": True,
            "timeout": _DEFAULT_HTTP_TIMEOUT_SECONDS,
            "headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
            },
        }

        cached_plan_res: dict[str, Any] | None = None
        if session is not None:
            try:
                async with http.create_async_client(**client_kwargs) as http_client:
                    res = await _check_plan_status_fn(http_client, session, logger)
                    if res.get("plan") == "plus":
                        return res
                    if res.get("plan") == "free":
                        cached_plan_res = res
                        if stripe_intent_state is not None and stripe_intent_state != "succeeded":
                            return res
            except Exception as exc:
                logger.debug("cached session check error for %s: %s", parsed.email, exc)

        if (stripe_intent_state == "succeeded" or session is None) and parsed.password:
            try:
                async with http.create_async_client(**client_kwargs) as login_http:
                    chatgpt_login = ChatgptLoginClient(
                        http_client=login_http,
                        session_cache=self._session_cache,
                        logger=logger,
                    )
                    fresh_session = await chatgpt_login.login(
                        email=parsed.email,
                        password=parsed.password,
                        totp_secret=parsed.totp_secret,
                    )
                    await self._session_cache.save(account_key, asdict(fresh_session))

                    fresh_res = await _check_plan_status_fn(login_http, fresh_session, logger)
                    logger.info("fresh login plan check for %s: %s", parsed.email, fresh_res)
                    if fresh_res.get("plan") == "plus":
                        return fresh_res
                    if fresh_res.get("plan") == "free":
                        if stripe_intent_state == "succeeded":
                            fresh_res["warning"] = "stripe_succeeded_but_openai_free"
                            fresh_res["raw_plan"] = "openai_free_despite_stripe_succeeded"
                        return fresh_res
            except Exception as exc:
                logger.warning("check_plan fresh login failed for %s: %s", parsed.email, exc)

        if cached_plan_res is not None:
            return cached_plan_res
        if session is None:
            return {"plan": "unknown", "error": "no_session_cached", "email": parsed.email}
        return {"plan": "unknown", "error": "check_plan_failed", "email": parsed.email}


    async def run(self, job: Job, proxy_lease: ProxyLease | None) -> JobResult:
        logger = self._logger_factory(f"oaipay.flow.{job.job_id}")
        known_secrets: list[str] = []

        login_kwargs: dict[str, Any] = {
            "allow_redirects": True,
            "timeout": _DEFAULT_HTTP_TIMEOUT_SECONDS,
        }
        if proxy_lease is not None:
            login_kwargs["proxy"] = materialize_proxy(proxy_lease.materialized_url)

        try:
            async with http.create_async_client(**login_kwargs) as login_client:
                async with http.create_async_client(
                    allow_redirects=True,
                    timeout=_DEFAULT_HTTP_TIMEOUT_SECONDS,
                ) as oaipay_session:
                    return await self._run_inner(
                        job=job,
                        proxy_lease=proxy_lease,
                        login_client=login_client,
                        oaipay_session=oaipay_session,
                        logger=logger,
                        known_secrets=known_secrets,
                    )
        except asyncio.CancelledError:
            logger.info("flow cancelled (task): job_id=%s", job.job_id)
            return _STOPPED_RESULT

    async def _run_inner(
        self,
        *,
        job: Job,
        proxy_lease: ProxyLease | None,
        login_client: http.AsyncSession,
        oaipay_session: http.AsyncSession,
        logger: logging.Logger,
        known_secrets: list[str],
    ) -> JobResult:
        try:
            stop = _check_stop(job)
            if stop is not None:
                return stop

            parsed = _parse_account_line(job.account_line)
            if isinstance(parsed, AccountLineError):
                return JobResult(
                    status=JobStatus.ERROR,
                    error_code="invalid_account_line",
                    error_message=f"[parse_account_line] {parsed.reason}",
                )
            assert isinstance(parsed, ChatgptAccount)

            if parsed.password:
                known_secrets.append(parsed.password)
            if parsed.totp_secret:
                known_secrets.append(parsed.totp_secret)
            if parsed.access_token:
                known_secrets.append(parsed.access_token)

            yescaptcha_key = await self._resolve_yescaptcha_key()
            if yescaptcha_key:
                known_secrets.append(yescaptcha_key)

            checkout_lines, promotion_lines = await self._resolve_proxy_pools()
            for secret in _proxy_userinfos(checkout_lines + promotion_lines):
                if secret and secret not in known_secrets:
                    known_secrets.append(secret)

            logger.info(
                "flow start email=%s mode=%s",
                parsed.email,
                "direct" if proxy_lease is None else "proxied",
            )

            chatgpt = ChatgptLoginClient(
                http_client=login_client,
                session_cache=self._session_cache,
                logger=logger,
            )

            stop = _check_stop(job)
            if stop is not None:
                return stop

            account_key = self._compute_account_key(parsed.email)
            session = await _resolve_session_fn(
                login_client=chatgpt,
                session_cache=self._session_cache,
                parsed=parsed,
                account_key=account_key,
                logger=logger,
            )
            access_token = session.access_token
            if access_token and access_token not in known_secrets:
                known_secrets.append(access_token)

            stop = _check_stop_or_pause(job)
            if stop is not None:
                return stop

            base_url = await self._resolve_base_url()
            if not _is_allowed_base_url(base_url):
                raise ConfigError(detail="base_url not https or host not allowlisted")

            if not checkout_lines or not promotion_lines:
                raise ConfigError(detail="proxy pool empty")

            proxy_pools = {
                "checkout": "\n".join(checkout_lines),
                "promotion": "\n".join(promotion_lines),
            }

            client = self._oaipay_client_factory(
                oaipay_session, base_url, known_secrets, logger
            )

            stop = _check_stop_or_pause(job)
            if stop is not None:
                return stop

            captcha_cfg = await client.get_captcha_config()
            turnstile = TurnstileToken(token="", user_agent=None)
            if captcha_cfg.provider == "turnstile":
                if not yescaptcha_key:
                    raise CaptchaSolveError(detail="no api key")
                if not (captcha_cfg.site_key or "").strip():
                    raise CaptchaSolveError(detail="missing site_key")
                stop = _check_stop_or_pause(job)
                if stop is not None:
                    return stop
                turnstile = await self._captcha_solver(
                    session=oaipay_session,
                    page_url=f"{base_url.rstrip('/')}/",
                    site_key=captcha_cfg.site_key or "",
                    api_key=yescaptcha_key,
                    logger=logger,
                    known_secrets=known_secrets,
                    cancellation_token=job.cancellation_token,
                )
                if turnstile.token and turnstile.token not in known_secrets:
                    known_secrets.append(turnstile.token)

            stop = _check_stop_or_pause(job)
            if stop is not None:
                return stop

            await client.token_info(
                access_token,
                proxy_pools,
                turnstile_token=turnstile.token,
                user_agent=turnstile.user_agent,
            )

            def _on_progress(event: Any) -> None:
                desc = (getattr(event, "desc", None) or "") or ""
                logger.info(
                    redact_message(
                        f"[{event.type}] step={event.step}/{event.total} "
                        f"queued={event.queued} {desc}".rstrip(),
                        known_secrets,
                    )
                )
                # Fail-fast: account already Plus — OaiPay will keep retrying.
                lowered = desc.lower()
                if any(marker in lowered for marker in _ALREADY_PAID_MARKERS):
                    raise AlreadyPaidError(detail="User is already paid")

            run_timeout = await self._resolve_run_timeout()
            try:
                result = await asyncio.wait_for(
                    client.long_link_stream(
                        access_token,
                        proxy_pools,
                        turnstile_token=turnstile.token,
                        user_agent=turnstile.user_agent,
                        cancellation_token=job.cancellation_token,
                        on_progress=_on_progress,
                    ),
                    timeout=run_timeout,
                )
            except asyncio.TimeoutError as exc:
                raise RunTimeoutError(timeout_seconds=run_timeout) from exc

            return self._terminal_from_result(result, job.job_id, logger)

        except RunCancelledError:
            logger.info("flow cancelled during run: job_id=%s", job.job_id)
            return _STOPPED_RESULT
        except (OaipayFlowError, ChatgptLoginError) as exc:
            if job.cancellation_token.is_cancelled():
                return _STOPPED_RESULT
            error_message = redact_message(str(exc), known_secrets)
            logger.warning(
                "flow error: job_id=%s step=%s error_code=%s message=%s",
                job.job_id,
                getattr(exc, "step", "?"),
                exc.error_code,
                error_message,
            )
            # Already-paid (and any error text with the same marker) means
            # the account is Plus — persist plan so Free export skips it.
            plan_hint: str | None = None
            lowered_msg = error_message.lower()
            if isinstance(exc, AlreadyPaidError) or any(
                marker in lowered_msg for marker in _ALREADY_PAID_MARKERS
            ):
                plan_hint = "plus"
            return JobResult(
                status=JobStatus.ERROR,
                error_code=exc.error_code,
                error_message=error_message,
                plan=plan_hint,
            )

    def _terminal_from_result(
        self, result: LongLinkResult, job_id: str, logger: logging.Logger
    ) -> JobResult:
        provider_err = (result.provider_error or "").strip()
        if result.ok and not result.fallback and not provider_err:
            if not (result.long_url or "").strip():
                raise RunFailedError(detail="missing long_url")
            png_path = self._decode_and_write_qr(
                result.provider_redirect_url, job_id
            )
            logger.info("flow qr_ready artifact=%s", png_path)
            return JobResult(
                status=JobStatus.QR_READY,
                artifact_path=str(png_path),
                payment_link=result.long_url,
                qr_expires_at=_parse_vendor_expires_at(result.upi_expires_at),
            )
        detail = provider_err or f"fallback={result.fallback}"
        if any(marker in detail.lower() for marker in _ALREADY_PAID_MARKERS):
            raise AlreadyPaidError(detail=detail)
        raise RunFailedError(detail=detail)

    def _decode_and_write_qr(self, data_uri: str | None, job_id: str) -> Path:
        if not data_uri or not data_uri.startswith(_QR_DATA_URI_PREFIX):
            raise QrDecodeError(reason="missing 'data:image/png;base64,' prefix")
        b64_payload = data_uri[len(_QR_DATA_URI_PREFIX) :]
        try:
            png_bytes = base64.b64decode(b64_payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise QrDecodeError(reason=f"invalid base64: {exc}") from exc
        if not png_bytes:
            raise QrDecodeError(reason="empty PNG payload")
        self._qr_output_dir.mkdir(parents=True, exist_ok=True)
        png_path = self._qr_output_dir / f"{job_id}.png"
        png_path.write_bytes(png_bytes)
        return png_path

    async def _resolve_base_url(self) -> str:
        value = await self._settings.get(_SETTING_BASE_URL)
        return value if isinstance(value, str) and value else _DEFAULT_BASE_URL

    async def _resolve_yescaptcha_key(self) -> str:
        value = await self._settings.get(_SETTING_YESCAPTCHA)
        return value if isinstance(value, str) else ""

    async def _resolve_proxy_pools(self) -> tuple[list[str], list[str]]:
        checkout = await self._settings.get(_SETTING_PROXY_CHECKOUT)
        promotion = await self._settings.get(_SETTING_PROXY_PROMOTION)
        return (
            self._as_nonempty_lines(checkout),
            self._as_nonempty_lines(promotion),
        )

    @staticmethod
    def _as_nonempty_lines(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        lines: list[str] = []
        for item in value:
            if isinstance(item, str):
                s = item.strip()
                if s:
                    lines.append(s)
        return lines

    async def _resolve_run_timeout(self) -> float:
        value = await self._settings.get(_SETTING_RUN_TIMEOUT)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
        return _DEFAULT_RUN_TIMEOUT_SECONDS

    @staticmethod
    def _compute_account_key(email: str) -> str:
        return hashlib.sha256(email.encode("utf-8")).hexdigest()[
            :_ACCOUNT_KEY_HEX_LENGTH
        ]


def _static_conformance_check(handler: "OaipayFlowHandler") -> PaymentFlowHandler:
    return handler


__all__ = ["OaipayFlowHandler", "OaipayClientFactory"]
