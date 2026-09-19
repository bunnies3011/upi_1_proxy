"""UpiDirectFlowHandler — in-house ChatGPT+Stripe UPI QR (`payments/upi_direct/`).

Phase 2: post-login A/B checkout → promo → Stripe init → amount gate → elements
→ sanitized `confirm_pending` stub.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
from contextlib import AsyncExitStack
from dataclasses import asdict
from pathlib import Path
from typing import Any, Awaitable, Callable, Final

from app.core import http_client as http
from app.core.payment_flow import (
    AccountLineError,
    Job,
    JobResult,
    JobStatus,
    ParsedAccount,
    PaymentFlowHandler,
    ProxyLeaseHealth,
)
from app.core.proxy_format import materialize_proxy, sanitize_proxy_text
from app.core.proxy_pool import ProxyLease
from app.core.redaction import redact_message
from app.core.session_cache import AccountSessionCache
from app.core.settings_store import SettingsRepository
from app.payments._chatgpt.errors import ChatgptLoginError
from app.payments._chatgpt.login_client import ChatgptLoginClient
from app.payments._chatgpt.models import ChatgptAccount, SessionBundle
from app.payments._chatgpt.models import parse_account_line as _parse_account_line
from app.payments._chatgpt.plan_status import check_plan_status as _check_plan_status_fn
from app.payments._chatgpt.session import (
    _safe_build_session_from_cache as _safe_build_session_from_cache_fn,
    resolve_session as _resolve_session_fn,
)
from app.payments.upi_direct.cancel_helpers import (
    await_cancelable,
    check_stop,
    check_stop_or_pause,
)
from app.payments.upi_direct.errors import (
    AlreadyPaidConflictError,
    AlreadyPaidError,
    CheckoutError,
    LoginStepError,
    RunTimeoutError,
    UpiDirectFlowError,
)
from app.payments.upi_direct.post_login_flow import run_post_login
from app.payments.upi_direct.proxy_pools import (
    pick_and_materialize,
    proxy_secret_values,
    resolve_proxy_pools,
)
from app.payments.upi_direct.network_safety import (
    _MAX_HTML_BYTES,
    extract_intent_state_from_hosted_html,
    fetch_bounded_bytes,
    is_allowed_hosted_instructions_url,
)
from app.payments.upi_direct.redacting_logger import RedactingLogger

_SETTING_MAX_CONCURRENT: Final[str] = "upi_direct.max_concurrent"
_SETTING_RUN_TIMEOUT: Final[str] = "upi_direct.run_timeout_seconds"
_SETTING_REQUIRE_PROMO: Final[str] = "upi_direct.require_promo"
_DEFAULT_RUN_TIMEOUT_SECONDS: Final[float] = 300.0
_DEFAULT_HTTP_TIMEOUT_SECONDS: Final[float] = 30.0
_ACCOUNT_KEY_HEX_LENGTH: Final[int] = 32
_NETWORK_LOGIN_REASON: Final[str] = "network_error"
_TOKEN_INVALIDATED_CODE: Final[str] = "token_invalidated"

PostLoginRunner = Callable[..., Awaitable[JobResult]]


def _is_token_invalidated_checkout(exc: CheckoutError) -> bool:
    """Return True only for ChatGPT's explicit invalidated-token response."""
    if getattr(exc, "status_code", None) != 401:
        return False

    payload = getattr(exc, "response_payload", None)
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("code") == _TOKEN_INVALIDATED_CODE:
            return True

    detail = str(getattr(exc, "detail", "") or "").lower()
    return _TOKEN_INVALIDATED_CODE in detail


class UpiDirectFlowHandler:
    def __init__(
        self,
        *,
        settings: SettingsRepository,
        session_cache: AccountSessionCache,
        qr_output_dir: Path,
        logger_factory: Callable[[str], logging.Logger],
        payment_method: str = "upi_direct",
        max_concurrent_key: str = _SETTING_MAX_CONCURRENT,
        run_timeout_key: str = _SETTING_RUN_TIMEOUT,
        require_promo_key: str = _SETTING_REQUIRE_PROMO,
        proxy_checkout_key: str = "upi_direct.proxy_checkout",
        proxy_promotion_key: str = "upi_direct.proxy_promotion",
        post_login_runner: PostLoginRunner = run_post_login,
    ) -> None:
        self._settings = settings
        self._session_cache = session_cache
        self._qr_output_dir = qr_output_dir
        self._logger_factory = logger_factory
        self._payment_method = payment_method
        self._max_concurrent_key = max_concurrent_key
        self._run_timeout_key = run_timeout_key
        self._require_promo_key = require_promo_key
        self._proxy_checkout_key = proxy_checkout_key
        self._proxy_promotion_key = proxy_promotion_key
        self._post_login_runner = post_login_runner
        runner_params = inspect.signature(post_login_runner).parameters
        self._post_login_accepts_proxy_lease = (
            "proxy_lease" in runner_params
            or any(
                param.kind == inspect.Parameter.VAR_KEYWORD
                for param in runner_params.values()
            )
        )

    def parse_account_line(self, line: str) -> ParsedAccount | AccountLineError:
        return _parse_account_line(line)

    def get_max_concurrent_key(self) -> str:
        return self._max_concurrent_key

    def get_account_dedup_key(self, parsed: ParsedAccount) -> str | None:
        if not isinstance(parsed, ChatgptAccount):
            return None
        key = parsed.email.strip().casefold()
        return key or None

    async def check_plan_status(self, job: Job) -> dict[str, Any]:
        logger = self._logger_factory(f"{self._payment_method}.check_plan.{job.job_id}")
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

        pools = None
        try:
            pools = await resolve_proxy_pools(
                self._settings,
                checkout_key=self._proxy_checkout_key,
                promotion_key=self._proxy_promotion_key,
            )
        except Exception as exc:
            logger.debug("check_plan proxy resolve skipped: %s", exc)

        def _get_client_kwargs(use_proxy: bool) -> dict[str, Any]:
            kw: dict[str, Any] = {
                "allow_redirects": True,
                "timeout": _DEFAULT_HTTP_TIMEOUT_SECONDS,
                "headers": {
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    "Accept-Language": "en-IN,en;q=0.9",
                },
            }
            if use_proxy and pools is not None:
                proxy_sel = pick_and_materialize(pools.checkout_lines)
                if proxy_sel is not None:
                    kw["proxy"] = proxy_sel.materialized_url
            return kw

        # 2. If cached session exists, check plan via cached session first.
        cached_plan_res: dict[str, Any] | None = None
        if session is not None:
            for attempt in range(2):
                client_kwargs = _get_client_kwargs(use_proxy=(attempt == 0))
                try:
                    async with http.create_async_client(**client_kwargs) as http_client:
                        res = await _check_plan_status_fn(http_client, session, logger)
                        if res.get("plan") == "plus":
                            # OpenAI confirmed Plus directly!
                            return res
                        if res.get("plan") == "free":
                            cached_plan_res = res
                            # If Stripe was NOT paid / not succeeded, free from OpenAI is definitive
                            if stripe_intent_state is not None and stripe_intent_state != "succeeded":
                                return res
                            break
                        err = str(res.get("error", ""))
                        if "403" not in err and "transport" not in err and "timeout" not in err:
                            break
                except Exception as exc:
                    logger.debug("cached session check error for %s: %s", parsed.email, exc)

        # 3. If Stripe payment SUCCEEDED (or session miss/corrupt), verify actual Plus on OpenAI via fresh login!
        # This handles:
        #   a) Pre-payment cached session lagging with 'free' / 401 token.
        #   b) Stripe payment succeeded BUT OpenAI hasn't provisioned Plus (prevents false positive Plus).
        if (stripe_intent_state == "succeeded" or session is None) and parsed.password:
            for login_attempt in range(2):
                client_kwargs = _get_client_kwargs(use_proxy=True)
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
                    logger.warning("check_plan fresh login attempt %d failed for %s: %s", login_attempt + 1, parsed.email, exc)

        # 4. Fallback if fresh login couldn't run or wasn't required
        if cached_plan_res is not None:
            return cached_plan_res
        if session is None:
            return {"plan": "unknown", "error": "no_session_cached", "email": parsed.email}
        return {"plan": "unknown", "error": "check_plan_failed", "email": parsed.email}


    async def run(self, job: Job, proxy_lease: ProxyLease | None) -> JobResult:
        known_secrets: list[str] = []
        base_logger = self._logger_factory(f"{self._payment_method}.flow.{job.job_id}")
        logger = RedactingLogger(base_logger, {"secrets": known_secrets})
        health_box: list[ProxyLeaseHealth | None] = [None]

        try:
            async with AsyncExitStack() as stack:
                run_timeout = await self._resolve_run_timeout()
                try:
                    async with asyncio.timeout(run_timeout):
                        return await self._run_owned(
                            job=job,
                            proxy_lease=proxy_lease,
                            stack=stack,
                            logger=logger,
                            known_secrets=known_secrets,
                            health_box=health_box,
                        )
                except TimeoutError as exc:
                    raise RunTimeoutError(timeout_seconds=run_timeout) from exc
        except asyncio.CancelledError:
            logger.info("flow cancelled: job_id=%s", job.job_id)
            return JobResult(
                status=JobStatus.STOPPED,
                proxy_lease_health=health_box[0],
            )
        except (UpiDirectFlowError, ChatgptLoginError) as exc:
            return self._map_error(
                job, exc, known_secrets, logger, health_box[0], proxy_lease
            )
        except Exception as exc:
            # Catch-all secret-aware boundary. An unexpected exception (e.g. a
            # ValueError from proxy/URL parsing that embeds the raw proxy line,
            # which `materialize_proxy` puts in its message) would otherwise
            # reach JobManager, which persists and broadcasts the raw
            # `str(exc)`. Redact with the job's known secrets first and never
            # surface the original message. CancelledError is BaseException, so
            # it is handled above and not swallowed here.
            if job.cancellation_token.is_cancelled():
                return JobResult(
                    status=JobStatus.STOPPED, proxy_lease_health=health_box[0]
                )
            safe = sanitize_proxy_text(redact_message(str(exc), known_secrets))
            logger.error(
                "flow unexpected error: job_id=%s type=%s message=%s",
                job.job_id,
                exc.__class__.__name__,
                safe,
            )
            return JobResult(
                status=JobStatus.ERROR,
                error_code=f"{self._payment_method}_internal_error",
                error_message=safe,
                proxy_lease_health=health_box[0],
            )

    async def _run_owned(
        self,
        *,
        job: Job,
        proxy_lease: ProxyLease | None,
        stack: AsyncExitStack,
        logger: logging.LoggerAdapter,
        known_secrets: list[str],
        health_box: list[ProxyLeaseHealth | None],
    ) -> JobResult:
        stop = check_stop(job)
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

        self._register_account_secrets(parsed, known_secrets)
        pools = await resolve_proxy_pools(
            self._settings,
            checkout_key=self._proxy_checkout_key,
            promotion_key=self._proxy_promotion_key,
        )
        for secret in proxy_secret_values(pools.checkout_lines + pools.promotion_lines):
            if secret not in known_secrets:
                known_secrets.append(secret)
        if proxy_lease is not None:
            for secret in proxy_secret_values([proxy_lease.materialized_url]):
                if secret not in known_secrets:
                    known_secrets.append(secret)

        logger.info(
            "flow start email=%s mode=%s",
            parsed.email,
            "direct" if proxy_lease is None else "proxied",
        )

        request_timeout = await self._resolve_request_timeout()
        login_kwargs: dict[str, Any] = {
            "allow_redirects": True,
            "timeout": request_timeout,
        }
        if proxy_lease is not None:
            login_kwargs["proxy"] = materialize_proxy(proxy_lease.materialized_url)

        login_session = await stack.enter_async_context(
            http.create_async_client(**login_kwargs)
        )
        chatgpt_login = ChatgptLoginClient(
            http_client=login_session,
            session_cache=self._session_cache,
            logger=logger,  # type: ignore[arg-type]
        )

        stop = check_stop(job)
        if stop is not None:
            return stop

        account_key = self._compute_account_key(parsed.email)
        try:
            session = await await_cancelable(
                job,
                _resolve_session_fn(
                    login_client=chatgpt_login,
                    session_cache=self._session_cache,
                    parsed=parsed,
                    account_key=account_key,
                    logger=logger,  # type: ignore[arg-type]
                ),
            )
        except asyncio.CancelledError:
            raise
        except ChatgptLoginError as exc:
            if (
                proxy_lease is not None
                and getattr(exc, "reason", "") == _NETWORK_LOGIN_REASON
            ):
                health_box[0] = ProxyLeaseHealth.DEAD
            raise LoginStepError(
                reason=getattr(exc, "reason", "login_failed"),
                message=str(exc),
                is_transport=getattr(exc, "reason", "") == _NETWORK_LOGIN_REASON,
            ) from exc

        access_token = session.access_token
        if access_token and access_token not in known_secrets:
            known_secrets.append(access_token)

        if proxy_lease is not None:
            health_box[0] = ProxyLeaseHealth.ALIVE

        stop = check_stop_or_pause(job)
        if stop is not None:
            return JobResult(
                status=stop.status,
                pause_requested=stop.pause_requested,
                proxy_lease_health=health_box[0],
            )

        require_promo = await self._resolve_require_promo()
        runner_kwargs = {
            "job": job,
            "stack": stack,
            "logger": logger,
            "known_secrets": known_secrets,
            "health_box": health_box,
            "pools": pools,
            "settings": self._settings,
            "require_promo": require_promo,
            "qr_output_dir": self._qr_output_dir,
        }
        if self._post_login_accepts_proxy_lease:
            runner_kwargs["proxy_lease"] = proxy_lease
        runner_kwargs.update(
            await self._extra_post_login_runner_kwargs(
                job=job,
                proxy_lease=proxy_lease,
            )
        )
        try:
            return await self._post_login_runner(session=session, **runner_kwargs)
        except CheckoutError as exc:
            if not parsed.password or not _is_token_invalidated_checkout(exc):
                raise

            logger.warning(
                "checkout rejected cached session with token_invalidated; "
                "clearing account cache and signing in again once"
            )
            await self._session_cache.clear(account_key)
            chatgpt_login.reset_openai_cookies()
            try:
                session = await await_cancelable(
                    job,
                    _resolve_session_fn(
                        login_client=chatgpt_login,
                        session_cache=self._session_cache,
                        parsed=parsed,
                        account_key=account_key,
                        logger=logger,  # type: ignore[arg-type]
                    ),
                )
            except asyncio.CancelledError:
                raise
            except ChatgptLoginError as login_exc:
                raise LoginStepError(
                    reason=getattr(login_exc, "reason", "login_failed"),
                    message=str(login_exc),
                    is_transport=(
                        getattr(login_exc, "reason", "") == _NETWORK_LOGIN_REASON
                    ),
                ) from login_exc

            # `resolve_session` normally persists the fresh login. Saving again
            # is harmless and also covers injected/custom resolvers.
            await self._session_cache.save(account_key, asdict(session))
            if session.access_token and session.access_token not in known_secrets:
                known_secrets.append(session.access_token)
            logger.info("fresh login complete; retrying checkout once")
            return await self._post_login_runner(session=session, **runner_kwargs)

    async def _extra_post_login_runner_kwargs(
        self,
        *,
        job: Job,
        proxy_lease: ProxyLease | None,
    ) -> dict[str, Any]:
        return {}

    def _map_error(
        self,
        job: Job,
        exc: UpiDirectFlowError | ChatgptLoginError,
        known_secrets: list[str],
        logger: logging.LoggerAdapter,
        lease_health: ProxyLeaseHealth | None,
        proxy_lease: ProxyLease | None,
    ) -> JobResult:
        if job.cancellation_token.is_cancelled():
            return JobResult(status=JobStatus.STOPPED, proxy_lease_health=lease_health)

        health = lease_health
        if isinstance(exc, LoginStepError) and exc.is_transport and proxy_lease is not None:
            health = ProxyLeaseHealth.DEAD
        elif (
            isinstance(exc, ChatgptLoginError)
            and getattr(exc, "reason", "") == _NETWORK_LOGIN_REASON
            and proxy_lease is not None
        ):
            health = ProxyLeaseHealth.DEAD

        error_code = getattr(exc, "error_code", f"{self._payment_method}_error")
        step = getattr(exc, "step", "?")
        error_message = sanitize_proxy_text(redact_message(str(exc), known_secrets))
        plan: str | None = None
        if isinstance(exc, AlreadyPaidError):
            plan = "plus"
        elif isinstance(exc, AlreadyPaidConflictError):
            plan = "free"

        logger.warning(
            "flow error: job_id=%s step=%s error_code=%s message=%s",
            job.job_id,
            step,
            error_code,
            error_message,
        )
        return JobResult(
            status=JobStatus.ERROR,
            error_code=error_code,
            error_message=error_message,
            plan=plan,
            proxy_lease_health=health,
        )

    async def _resolve_run_timeout(self) -> float:
        value = await self._settings.get(self._run_timeout_key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
        return _DEFAULT_RUN_TIMEOUT_SECONDS

    async def _resolve_require_promo(self) -> bool:
        value = await self._settings.get(self._require_promo_key)
        if value is None:
            return True
        return bool(value)

    async def _resolve_request_timeout(self) -> float:
        key = f"{self._payment_method}.stripe_request_timeout_seconds"
        try:
            value = await self._settings.get(key)
        except Exception:
            return _DEFAULT_HTTP_TIMEOUT_SECONDS
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
        return _DEFAULT_HTTP_TIMEOUT_SECONDS

    @staticmethod
    def _register_account_secrets(
        parsed: ChatgptAccount, known_secrets: list[str]
    ) -> None:
        if parsed.password:
            known_secrets.append(parsed.password)
        if parsed.totp_secret:
            known_secrets.append(parsed.totp_secret)
        if parsed.access_token:
            known_secrets.append(parsed.access_token)

    @staticmethod
    def _compute_account_key(email: str) -> str:
        return hashlib.sha256(email.encode("utf-8")).hexdigest()[
            :_ACCOUNT_KEY_HEX_LENGTH
        ]


def _static_conformance_check(handler: UpiDirectFlowHandler) -> PaymentFlowHandler:
    return handler


__all__ = ["UpiDirectFlowHandler"]
