"""UpiFlowHandler — orchestrates the UPI vendor-QR flow (`payments/upi/`).

`UpiFlowHandler` implements `PaymentFlowHandler` (`app.core.payment_flow`) and is
registered into `JobManager` as `"upi"` by `payments/upi/__init__.py`. It reuses
the shared ChatGPT login/session surface (`payments/_chatgpt`) — UPI only needs a
`login -> access_token`, NOT Stripe checkout, so it drives a plain
`ChatgptLoginClient` (not iDEAL's checkout `ChatgptClient`).

Flow order (precheck runs BEFORE acquire so an ineligible account reserves no
credit):

    1. Parse the account line -> `ChatgptAccount`; build `known_secrets` with the
       account secrets. `access_token` is added to `known_secrets` BEFORE any
       vendor call so every free-form log/error string is value-redacted.
    2. `resolve_session(...)` -> `SessionBundle`; checkpoint (cancel/pause) after
       the cache has persisted.
    3. If `upi.eligibility_precheck`: `vendor.account_check(access_token)` -> an
       ineligible account raises `EligibilityError` (no credit reserved yet).
    4. `code = await pool.acquire()` (reserves one credit).
    5. `challenge = await vendor.challenge(code)`.
    6. `result = await asyncio.wait_for(vendor.run(...), timeout=run_timeout)`.
    7. Success -> decode `qr_image_png` -> write `<job_id>.png` ->
       `JobResult(QR_READY, artifact_path=..., payment_link=hosted_url)`.

Single error boundary (mirrors `ideal/flow.py`): steps 3-7 run under one
try/except whose `finally` calls `pool.release(code, committed=<success>)`
whenever a code was reserved — a reserved credit is restored on ANY non-success
terminal. `asyncio.TimeoutError` is mapped to `VendorTimeoutError` (never escapes
unmapped); `VendorRunCancelled` / a set cancellation token map to STOPPED; a
vendor credit-specific failure prunes the code via `pool.mark_exhausted(code)`.

Payment_Module_Boundary: imports only `app.core.*` and `app.payments._chatgpt.*`
/ `app.payments.upi.*` — NOTHING from `app.payments.ideal`.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Final

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
from app.payments.upi.errors import (
    EligibilityError,
    LicenseExhaustedError,
    QrDecodeError,
    UpiFlowError,
    VendorRunCancelled,
    VendorRunError,
    VendorTimeoutError,
)
from app.payments.upi_direct.network_safety import (
    _MAX_HTML_BYTES,
    extract_intent_state_from_hosted_html,
    fetch_bounded_bytes,
    is_allowed_hosted_instructions_url,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.payments.upi.license_pool import UpiLicensePool
    from app.payments.upi.vendor_client import CapybaraClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SETTING_MAX_CONCURRENT: Final[str] = "upi.max_concurrent"
_SETTING_VENDOR_BASE_URL: Final[str] = "upi.vendor_base_url"
_SETTING_ELIGIBILITY_PRECHECK: Final[str] = "upi.eligibility_precheck"
_SETTING_RUN_TIMEOUT: Final[str] = "upi.run_timeout_seconds"

_DEFAULT_VENDOR_BASE_URL: Final[str] = "https://pix.capybara.cv"
_DEFAULT_RUN_TIMEOUT_SECONDS: Final[float] = 120.0
_DEFAULT_HTTP_TIMEOUT_SECONDS: Final[float] = 30.0

#: Data-URI prefix the vendor uses for `qr_image_png`.
_QR_DATA_URI_PREFIX: Final[str] = "data:image/png;base64,"

#: SHA-256 hex length used as the session-cache `account_key` (never stores the
#: raw email), matching the iDEAL flow's account-key discipline.
_ACCOUNT_KEY_HEX_LENGTH: Final[int] = 32

#: Vendor `result.code` values (on `result.ok=false`) that indicate the license
#: code itself is out of credit — the handler prunes such a code via
#: `mark_exhausted`. Any other `result.ok=false` is a generic business failure.
_CREDIT_FAILURE_VENDOR_CODES: Final[frozenset[str]] = frozenset(
    {
        "upi_no_license_credit",
        "no_license_credit",
        "license_exhausted",
        "no_credit",
        "insufficient_credit",
        "credit_exhausted",
        "quota_exceeded",
    }
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

#: `(session, base_url, known_secrets, logger) -> CapybaraClient`. Injected so a
#: test can swap in a fake vendor without an HTTP round-trip.
UpiVendorClientFactory = Callable[
    ["http.AsyncSession", str, list[str], logging.Logger], "CapybaraClient"
]


def _check_stop(job: Job) -> "JobResult | None":
    """Cancel-only checkpoint (parse / login stages) — returns STOPPED or None."""
    if job.cancellation_token.is_cancelled():
        return _STOPPED_RESULT
    return None


def _check_stop_or_pause(job: Job) -> "JobResult | None":
    """Cancel + pause checkpoint (after the cache has persisted). Cancel wins."""
    if job.cancellation_token.is_cancelled():
        return _STOPPED_RESULT
    if job.cancellation_token.is_paused():
        return _PAUSED_RESULT
    return None


class UpiFlowHandler:
    """Orchestrator for the UPI vendor-QR flow — implements `PaymentFlowHandler`."""

    def __init__(
        self,
        *,
        settings: SettingsRepository,
        session_cache: AccountSessionCache,
        qr_output_dir: Path,
        logger_factory: Callable[[str], logging.Logger],
        license_pool: "UpiLicensePool",
        vendor_client_factory: UpiVendorClientFactory,
    ) -> None:
        self._settings = settings
        self._session_cache = session_cache
        self._qr_output_dir = qr_output_dir
        self._logger_factory = logger_factory
        self._license_pool = license_pool
        self._vendor_client_factory = vendor_client_factory

    @property
    def license_pool(self) -> "UpiLicensePool":
        """Expose the pool for wiring (e.g. settings-change re-hydration)."""
        return self._license_pool

    # ------------------------------------------------------------------
    # PaymentFlowHandler protocol methods
    # ------------------------------------------------------------------

    def parse_account_line(self, line: str) -> ParsedAccount | AccountLineError:
        """Delegate to the shared `_chatgpt.parse_account_line`."""
        return _parse_account_line(line)

    def get_max_concurrent_key(self) -> str:
        """Return `"upi.max_concurrent"` — read by `JobManager` (no hardcoding)."""
        return _SETTING_MAX_CONCURRENT

    async def check_plan_status(self, job: Job) -> dict[str, Any]:
        """Check Plus for a UPI job via cached ChatGPT session (same as iDEAL).

        Uses `session_cache` + shared `_chatgpt.plan_status` — never imports
        `payments/ideal`. Without this method JobManager returns
        `plan=unknown, error=not_supported` for every UPI "Check Plus" click.
        """
        logger = self._logger_factory(f"upi.check_plan.{job.job_id}")

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


    # ------------------------------------------------------------------
    # Main orchestrator
    # ------------------------------------------------------------------

    async def run(self, job: Job, proxy_lease: ProxyLease | None) -> JobResult:
        """Execute the UPI flow for one job.

        Builds a per-job `AsyncSession` (proxied when `proxy_lease` is non-None),
        then delegates to `_run_inner` which owns the single error boundary and
        the credit `finally`-release. Only `asyncio.CancelledError` (an external
        task cancel) is handled here -> STOPPED; every foreseen domain failure is
        mapped to a `JobResult` inside `_run_inner`.
        """
        logger = self._logger_factory(f"upi.flow.{job.job_id}")
        known_secrets: list[str] = []

        client_kwargs: dict[str, Any] = {
            "allow_redirects": True,
            "timeout": _DEFAULT_HTTP_TIMEOUT_SECONDS,
        }
        if proxy_lease is not None:
            proxy_url = materialize_proxy(proxy_lease.materialized_url)
            client_kwargs["proxy"] = proxy_url

        try:
            async with http.create_async_client(**client_kwargs) as http_client:
                return await self._run_inner(
                    job=job,
                    proxy_lease=proxy_lease,
                    http_client=http_client,
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
        http_client: http.AsyncSession,
        logger: logging.Logger,
        known_secrets: list[str],
    ) -> JobResult:
        """Flow body wrapped by the single error boundary + credit release.

        `code`/`success` live outside the try so the `finally` can release a
        reserved credit exactly once, restoring it on every non-success terminal.
        """
        code: str | None = None
        success = False

        try:
            # -- Step 1: parse account line -------------------------------
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

            logger.info(
                "flow start email=%s mode=%s",
                parsed.email,
                "direct" if proxy_lease is None else "proxied",
            )

            login_client = ChatgptLoginClient(
                http_client=http_client,
                session_cache=self._session_cache,
                logger=logger,
            )

            # -- Step 2: resolve session ----------------------------------
            stop = _check_stop(job)
            if stop is not None:
                return stop

            account_key = self._compute_account_key(parsed.email)
            session = await _resolve_session_fn(
                login_client=login_client,
                session_cache=self._session_cache,
                parsed=parsed,
                account_key=account_key,
                logger=logger,
            )
            access_token = session.access_token
            # access_token VALUE must be a known secret BEFORE any vendor call.
            if access_token and access_token not in known_secrets:
                known_secrets.append(access_token)

            # Checkpoint after the cache has persisted (login write-through).
            stop = _check_stop_or_pause(job)
            if stop is not None:
                return stop

            # Build the per-run vendor client (its own copy of known_secrets now
            # includes access_token, so its logs/errors are value-redacted).
            base_url = await self._resolve_base_url()
            vendor = self._vendor_client_factory(
                http_client, base_url, known_secrets, logger
            )

            def _on_progress(event: Any) -> None:
                label = event.label or ""
                logger.info(
                    redact_message(
                        f"[{event.stage}] {event.percent}% {label}".rstrip(),
                        known_secrets,
                    )
                )

            # -- Step 3: eligibility precheck (BEFORE acquire) ------------
            if await self._eligibility_precheck_enabled():
                stop = _check_stop_or_pause(job)
                if stop is not None:
                    return stop
                check = await vendor.account_check(access_token)
                if not check.eligible:
                    raise EligibilityError(
                        detail=(
                            f"account ineligible (is_paid={check.is_paid}, "
                            f"plan_type={check.plan_type})"
                        )
                    )

            # -- Step 4: reserve a license credit -------------------------
            stop = _check_stop_or_pause(job)
            if stop is not None:
                return stop
            code = await self._license_pool.acquire()

            # -- Step 5: challenge ----------------------------------------
            challenge = await vendor.challenge(code)

            # -- Step 6: run (NDJSON stream) with a wall-clock cap --------
            run_timeout = await self._resolve_run_timeout()
            try:
                result = await asyncio.wait_for(
                    vendor.run(
                        access_token,
                        code,
                        challenge,
                        cancellation_token=job.cancellation_token,
                        on_progress=_on_progress,
                    ),
                    timeout=run_timeout,
                )
            except asyncio.TimeoutError as exc:
                # MUST be mapped, never escape as a bare TimeoutError.
                raise VendorTimeoutError(timeout_seconds=run_timeout) from exc

            # -- Step 7: terminal — success or business failure ----------
            if result.ok:
                png_path = self._decode_and_write_qr(
                    result.qr_image_png, job.job_id
                )
                success = True
                logger.info("flow qr_ready artifact=%s", png_path)
                return JobResult(
                    status=JobStatus.QR_READY,
                    artifact_path=str(png_path),
                    payment_link=result.hosted_url,
                    qr_expires_at=_parse_vendor_expires_at(result.expires_at),
                )

            # result.ok == False — distinguish a credit-specific failure.
            if result.code and result.code in _CREDIT_FAILURE_VENDOR_CODES:
                self._license_pool.mark_exhausted(code)
                raise LicenseExhaustedError(
                    message=(
                        f"vendor reported license credit exhausted "
                        f"(code={result.code})"
                    )
                )
            raise VendorRunError(
                vendor_code=result.code,
                detail="vendor run reported a business failure",
            )

        except VendorRunCancelled:
            logger.info("flow cancelled during run: job_id=%s", job.job_id)
            return _STOPPED_RESULT
        except (UpiFlowError, ChatgptLoginError) as exc:
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
            return JobResult(
                status=JobStatus.ERROR,
                error_code=exc.error_code,
                error_message=error_message,
            )
        finally:
            # Restore a reserved credit on every non-success terminal; keep the
            # decrement on success. No-op if the code was already pruned.
            if code is not None:
                self._license_pool.release(code, committed=success)

    # ------------------------------------------------------------------
    # QR decode + write
    # ------------------------------------------------------------------

    def _decode_and_write_qr(self, data_uri: str | None, job_id: str) -> Path:
        """Decode a `data:image/png;base64,...` URI and write `<job_id>.png`.

        Raises `QrDecodeError` on a missing prefix, invalid base64, or empty
        payload — mapped at the boundary to `JobResult(ERROR, upi_qr_decode_failed)`.
        """
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

    # ------------------------------------------------------------------
    # Settings helpers
    # ------------------------------------------------------------------

    async def _resolve_base_url(self) -> str:
        value = await self._settings.get(_SETTING_VENDOR_BASE_URL)
        return value if isinstance(value, str) and value else _DEFAULT_VENDOR_BASE_URL

    async def _eligibility_precheck_enabled(self) -> bool:
        return bool(await self._settings.get(_SETTING_ELIGIBILITY_PRECHECK))

    async def _resolve_run_timeout(self) -> float:
        value = await self._settings.get(_SETTING_RUN_TIMEOUT)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
        return _DEFAULT_RUN_TIMEOUT_SECONDS

    # ------------------------------------------------------------------
    # Account key helper
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_account_key(email: str) -> str:
        """`sha256(email)[:32]` — the session-cache identifier (no raw email)."""
        return hashlib.sha256(email.encode("utf-8")).hexdigest()[
            :_ACCOUNT_KEY_HEX_LENGTH
        ]


def _static_conformance_check(handler: "UpiFlowHandler") -> PaymentFlowHandler:
    """Static check that `UpiFlowHandler` satisfies `PaymentFlowHandler`.

    Not called at runtime — exists so a type checker confirms the structural
    conformance (the runtime duck-type check lives in `JobManager`).
    """
    return handler


__all__ = ["UpiFlowHandler", "UpiVendorClientFactory"]
