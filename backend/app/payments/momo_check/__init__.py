"""MoMo payment-method probe.

Method key: `momo_check`. Settings namespace: `momo_check.*`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Final

from app.core.job_manager import JobManager
from app.core.session_cache import AccountSessionCache
from app.core.settings_store import SettingsRepository, TypeConstraint
from app.payments.momo_check.probe import run_momo_check_post_login
from app.payments.upi_direct.flow import UpiDirectFlowHandler

__all__ = [
    "register_momo_check_namespace",
    "register_momo_check_handler",
]

_NAMESPACE: Final[str] = "momo_check"
_PAYMENT_METHOD: Final[str] = "momo_check"

_KEY_CONSTRAINTS: Final[dict[str, TypeConstraint]] = {
    "proxy_checkout": TypeConstraint(type="list_str"),
    "proxy_promotion": TypeConstraint(type="list_str"),
    "max_concurrent": TypeConstraint(type="int", min=1, max=50),
    "run_timeout_seconds": TypeConstraint(type="int", min=5, max=3600),
    "stripe_request_timeout_seconds": TypeConstraint(type="int", min=1, max=120),
    "require_promo": TypeConstraint(type="bool"),
}


async def register_momo_check_namespace(settings: SettingsRepository) -> None:
    settings.register_namespace(_NAMESPACE, _KEY_CONSTRAINTS)

    defaults: dict[str, object] = {
        "momo_check.proxy_checkout": [],
        "momo_check.proxy_promotion": [],
        "momo_check.max_concurrent": 5,
        "momo_check.run_timeout_seconds": 300,
        "momo_check.stripe_request_timeout_seconds": 30,
        "momo_check.require_promo": True,
    }
    for key, default_value in defaults.items():
        if await settings.get(key) is None:
            await settings.set(key, default_value)


def register_momo_check_handler(
    job_manager: JobManager,
    settings: SettingsRepository,
    session_cache: AccountSessionCache,
    qr_output_dir: Path,
    logger_factory: Callable[[str], logging.Logger] = logging.getLogger,
) -> UpiDirectFlowHandler:
    handler = UpiDirectFlowHandler(
        settings=settings,
        session_cache=session_cache,
        qr_output_dir=qr_output_dir,
        logger_factory=logger_factory,
        payment_method=_PAYMENT_METHOD,
        max_concurrent_key="momo_check.max_concurrent",
        run_timeout_key="momo_check.run_timeout_seconds",
        require_promo_key="momo_check.require_promo",
        proxy_checkout_key="momo_check.proxy_checkout",
        proxy_promotion_key="momo_check.proxy_promotion",
        post_login_runner=run_momo_check_post_login,
    )
    job_manager.register_handler(_PAYMENT_METHOD, handler)
    return handler
