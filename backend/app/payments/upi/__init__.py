"""UPI payment channel — vendor-QR integration (`payments/upi/`).

Single integration point between `core/` and `payments/upi/` (mirrors
`payments/ideal/__init__.py`). Two public functions are called once at startup
by `app.bootstrap`:

    - `register_upi_namespace(settings)`: registers the `upi.*` settings
      whitelist and seeds every key with its default (only when unset).
    - `register_upi_handler(...)`: builds the `UpiLicensePool` + `UpiFlowHandler`
      and registers the handler into `JobManager` under `"upi"`.

Importing this package triggers NO registration side-effect — both functions take
the `core/` singletons as parameters, keeping the Payment_Module_Boundary intact.

Payment_Module_Boundary: this package imports only `app.core.*` and
`app.payments._chatgpt.*` — NOTHING from `app.payments.ideal`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Final

from app.core import http_client as http
from app.core.job_manager import JobManager
from app.core.session_cache import AccountSessionCache
from app.core.settings_store import SettingsRepository, TypeConstraint
from app.payments.upi.flow import UpiFlowHandler
from app.payments.upi.license_pool import UpiLicensePool
from app.payments.upi.vendor_client import CapybaraClient

__all__ = [
    "register_upi_namespace",
    "register_upi_handler",
]

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

_NAMESPACE: Final[str] = "upi"
_PAYMENT_METHOD: Final[str] = "upi"

#: Default vendor host — matches `CapybaraClient`'s own default.
_DEFAULT_VENDOR_BASE_URL: Final[str] = "https://pix.capybara.cv"

#: How often (seconds) the pool re-verifies a code's remaining credit via
#: `key/verify`. Kept in code (not a settings key) — a low-frequency,
#: server-IP egress cadence, not an operator knob for this phase.
_LICENSE_REFRESH_INTERVAL_SECONDS: Final[float] = 300.0

#: Error codes (from `payments/upi/errors.py`) whose jobs are worth auto-retrying
#: — transient stream / timeout failures, NOT durable ones (no license credit,
#: ineligible account, etc.).
_DEFAULT_AUTO_RETRY_BLOCKED_CODES: Final[list[str]] = [
    "upi_stream_error",
    "upi_run_timeout",
]

#: Type constraints for the `upi.*` whitelist. `low_credit_threshold` was
#: intentionally dropped (validation removed low-credit alerting).
_UPI_KEY_CONSTRAINTS: Final[dict[str, TypeConstraint]] = {
    "license_codes": TypeConstraint(type="list_str"),
    "max_concurrent": TypeConstraint(type="int", min=1, max=10),
    "run_timeout_seconds": TypeConstraint(type="int", min=1, max=3600),
    "eligibility_precheck": TypeConstraint(type="bool"),
    "auto_retry_blocked_codes": TypeConstraint(type="list_str"),
    "vendor_base_url": TypeConstraint(type="string", min=1, max=300),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def register_upi_namespace(settings: SettingsRepository) -> None:
    """Register the `upi.*` namespace and seed defaults (only when unset).

    Raises:
        NamespaceAlreadyRegisteredError: `upi` already registered (Fail_Fast).
        SettingsValidationError: a seeded default violates its constraint.
    """
    settings.register_namespace(_NAMESPACE, _UPI_KEY_CONSTRAINTS)

    _upi_defaults: dict[str, object] = {
        # No codes by default — the operator lists PK codes via the Settings API.
        "upi.license_codes": [],
        # Separate concurrency budget from iDEAL; conservative default (long
        # vendor runs × proxy/ban risk). Hard cap 10.
        "upi.max_concurrent": 3,
        # Wall-clock cap for a single vendor `run` stream.
        "upi.run_timeout_seconds": 120,
        # Operator toggle — off by default; when on, runs BEFORE acquire.
        "upi.eligibility_precheck": False,
        "upi.auto_retry_blocked_codes": list(_DEFAULT_AUTO_RETRY_BLOCKED_CODES),
        "upi.vendor_base_url": _DEFAULT_VENDOR_BASE_URL,
    }
    for key, default_value in _upi_defaults.items():
        if await settings.get(key) is None:
            await settings.set(key, default_value)


def register_upi_handler(
    job_manager: JobManager,
    settings: SettingsRepository,
    session_cache: AccountSessionCache,
    qr_output_dir: Path,
    logger_factory: Callable[[str], logging.Logger] = logging.getLogger,
) -> UpiFlowHandler:
    """Build the pool + handler and register it into `JobManager` as `"upi"`.

    Must be called after `register_upi_namespace()` (the handler reads `upi.*`
    live). Returns the handler for tests/integration that need a direct reference.

    Raises:
        HandlerAlreadyRegisteredError: `"upi"` already registered (Fail_Fast).
        HandlerContractError: the handler is missing a required contract method.
    """
    pool_logger = logger_factory("upi.license_pool")

    # A long-lived session for the pool's `key/verify` egress (server IP, not
    # proxied), created lazily on first use so nothing is opened at registration.
    pool_session_holder: dict[str, http.AsyncSession] = {}

    def _pool_vendor_factory() -> CapybaraClient:
        session = pool_session_holder.get("session")
        if session is None:
            session = http.create_async_client()
            pool_session_holder["session"] = session
        return CapybaraClient(
            session,
            base_url=_DEFAULT_VENDOR_BASE_URL,
            logger=pool_logger,
            known_secrets=None,
        )

    license_pool = UpiLicensePool(
        settings,
        _pool_vendor_factory,
        refresh_interval_seconds=_LICENSE_REFRESH_INTERVAL_SECONDS,
    )

    def _run_vendor_factory(
        session: http.AsyncSession,
        base_url: str,
        known_secrets: list[str],
        logger: logging.Logger,
    ) -> CapybaraClient:
        return CapybaraClient(
            session, base_url=base_url, logger=logger, known_secrets=known_secrets
        )

    handler = UpiFlowHandler(
        settings=settings,
        session_cache=session_cache,
        qr_output_dir=qr_output_dir,
        logger_factory=logger_factory,
        license_pool=license_pool,
        vendor_client_factory=_run_vendor_factory,
    )

    job_manager.register_handler(_PAYMENT_METHOD, handler)
    return handler
