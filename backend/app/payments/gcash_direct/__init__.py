"""GCash Direct payment channel - ChatGPT+Stripe GCash redirect link."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Final

from app.core.job_manager import JobManager
from app.core.session_cache import AccountSessionCache
from app.core.settings_store import SettingsRepository, TypeConstraint
from app.payments.gcash_direct.flow import GcashDirectFlowHandler

__all__ = [
    "register_gcash_direct_namespace",
    "register_gcash_direct_handler",
]

_NAMESPACE: Final[str] = "gcash_direct"
_PAYMENT_METHOD: Final[str] = "gcash_direct"

_KEY_CONSTRAINTS: Final[dict[str, TypeConstraint]] = {
    "proxy_checkout": TypeConstraint(type="list_str"),
    "proxy_promotion": TypeConstraint(type="list_str"),
    "max_concurrent": TypeConstraint(type="int", min=1, max=50),
    "run_timeout_seconds": TypeConstraint(type="int", min=5, max=3600),
    "stripe_request_timeout_seconds": TypeConstraint(type="int", min=1, max=120),
    "approve_error_retries": TypeConstraint(type="int", min=1, max=50),
    "require_promo": TypeConstraint(type="bool"),
    "headless_qr_enabled": TypeConstraint(type="bool"),
    "headless_poll_seconds": TypeConstraint(type="int", min=0, max=1800),
    "browser_qr_enabled": TypeConstraint(type="bool"),
    "browser_qr_capture_enabled": TypeConstraint(type="bool"),
    "browser_qr_timeout_seconds": TypeConstraint(type="int", min=5, max=120),
    "browser_hold_seconds": TypeConstraint(type="int", min=0, max=1800),
    "browser_hold_max_active": TypeConstraint(type="int", min=0, max=100),
    "browser_headless": TypeConstraint(type="bool"),
    "browser_use_proxy": TypeConstraint(type="bool"),
}


async def register_gcash_direct_namespace(settings: SettingsRepository) -> None:
    """Register the `gcash_direct.*` namespace and seed defaults."""
    settings.register_namespace(_NAMESPACE, _KEY_CONSTRAINTS)

    defaults: dict[str, object] = {
        "gcash_direct.proxy_checkout": [],
        "gcash_direct.proxy_promotion": [],
        "gcash_direct.max_concurrent": 3,
        "gcash_direct.run_timeout_seconds": 300,
        "gcash_direct.stripe_request_timeout_seconds": 30,
        "gcash_direct.approve_error_retries": 1,
        "gcash_direct.require_promo": True,
        "gcash_direct.headless_qr_enabled": True,
        "gcash_direct.headless_poll_seconds": 300,
        "gcash_direct.browser_qr_enabled": True,
        "gcash_direct.browser_qr_capture_enabled": True,
        "gcash_direct.browser_qr_timeout_seconds": 60,
        "gcash_direct.browser_hold_seconds": 300,
        "gcash_direct.browser_hold_max_active": 50,
        "gcash_direct.browser_headless": True,
        "gcash_direct.browser_use_proxy": False,
    }
    for key, default_value in defaults.items():
        if await settings.get(key) is None:
            await settings.set(key, default_value)

    # Match the standalone bot/manual flow: keep the browser leg local/direct
    # after the checkout link is ready. Some GCash callbacks land on plain
    # chatgpt.com when the browser leg uses the proxy.
    if await settings.get("gcash_direct.browser_use_proxy") is True:
        await settings.set("gcash_direct.browser_use_proxy", False)
    if await settings.get("gcash_direct.browser_qr_timeout_seconds") == 35:
        await settings.set("gcash_direct.browser_qr_timeout_seconds", 60)
    if await settings.get("gcash_direct.browser_hold_max_active") == 5:
        await settings.set("gcash_direct.browser_hold_max_active", 50)


def register_gcash_direct_handler(
    job_manager: JobManager,
    settings: SettingsRepository,
    session_cache: AccountSessionCache,
    qr_output_dir: Path,
    logger_factory: Callable[[str], logging.Logger] = logging.getLogger,
) -> GcashDirectFlowHandler:
    """Build the handler and register it into `JobManager` as `gcash_direct`."""
    handler = GcashDirectFlowHandler(
        settings=settings,
        session_cache=session_cache,
        qr_output_dir=qr_output_dir,
        logger_factory=logger_factory,
        plus_signal_callback=lambda job_id, detail: job_manager.mark_plus_verified_from_signal(
            job_id,
            source="gcash_browser",
            detail=detail,
        ),
    )
    job_manager.register_handler(_PAYMENT_METHOD, handler)
    return handler
