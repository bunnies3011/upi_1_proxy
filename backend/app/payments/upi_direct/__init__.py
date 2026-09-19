"""UPI Direct payment channel — in-house ChatGPT+Stripe UPI QR.

Method key: `upi_direct`. Settings namespace: `upi_direct.*`.

Importing this package is side-effect-free — registration happens via
`register_upi_direct_namespace` / `register_upi_direct_handler` (wired in bootstrap).

Payment_Module_Boundary: only `app.core.*` + `app.payments._chatgpt.*` +
`app.payments.upi_direct.*`. No import of `app.payments.ideal`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Final

from app.core.job_manager import JobManager
from app.core.session_cache import AccountSessionCache
from app.core.settings_store import SettingsRepository, TypeConstraint
from app.payments.upi_direct.flow import UpiDirectFlowHandler

__all__ = [
    "register_upi_direct_namespace",
    "register_upi_direct_handler",
]

_NAMESPACE: Final[str] = "upi_direct"
_PAYMENT_METHOD: Final[str] = "upi_direct"

_KEY_CONSTRAINTS: Final[dict[str, TypeConstraint]] = {
    "proxy_checkout": TypeConstraint(type="list_str"),
    "proxy_promotion": TypeConstraint(type="list_str"),
    "max_concurrent": TypeConstraint(type="int", min=1, max=50),
    "run_timeout_seconds": TypeConstraint(type="int", min=5, max=3600),
    "stripe_request_timeout_seconds": TypeConstraint(type="int", min=1, max=120),
    "approve_error_retries": TypeConstraint(type="int", min=1, max=50),
    "require_promo": TypeConstraint(type="bool"),
}


async def register_upi_direct_namespace(settings: SettingsRepository) -> None:
    """Register the `upi_direct.*` namespace and seed defaults (only when unset)."""
    settings.register_namespace(_NAMESPACE, _KEY_CONSTRAINTS)

    defaults: dict[str, object] = {
        "upi_direct.proxy_checkout": [],
        "upi_direct.proxy_promotion": [],
        "upi_direct.max_concurrent": 3,
        "upi_direct.run_timeout_seconds": 300,
        "upi_direct.stripe_request_timeout_seconds": 30,
        "upi_direct.approve_error_retries": 1,
        "upi_direct.require_promo": True,
    }
    for key, default_value in defaults.items():
        if await settings.get(key) is None:
            await settings.set(key, default_value)


def register_upi_direct_handler(
    job_manager: JobManager,
    settings: SettingsRepository,
    session_cache: AccountSessionCache,
    qr_output_dir: Path,
    logger_factory: Callable[[str], logging.Logger] = logging.getLogger,
) -> UpiDirectFlowHandler:
    """Build the handler and register it into `JobManager` as `"upi_direct"`."""
    handler = UpiDirectFlowHandler(
        settings=settings,
        session_cache=session_cache,
        qr_output_dir=qr_output_dir,
        logger_factory=logger_factory,
    )
    job_manager.register_handler(_PAYMENT_METHOD, handler)
    return handler
