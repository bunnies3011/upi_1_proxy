"""Kakao Direct payment channel - ChatGPT+Stripe Kakao Pay redirect link."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Final

from app.core.job_manager import JobManager
from app.core.session_cache import AccountSessionCache
from app.core.settings_store import SettingsRepository, TypeConstraint
from app.payments.kakao_direct.flow import KakaoDirectFlowHandler

__all__ = [
    "register_kakao_direct_namespace",
    "register_kakao_direct_handler",
]

_NAMESPACE: Final[str] = "kakao_direct"
_PAYMENT_METHOD: Final[str] = "kakao_direct"

_KEY_CONSTRAINTS: Final[dict[str, TypeConstraint]] = {
    "proxy_checkout": TypeConstraint(type="list_str"),
    "proxy_promotion": TypeConstraint(type="list_str"),
    "max_concurrent": TypeConstraint(type="int", min=1, max=50),
    "run_timeout_seconds": TypeConstraint(type="int", min=5, max=3600),
    "stripe_request_timeout_seconds": TypeConstraint(type="int", min=1, max=120),
    "approve_error_retries": TypeConstraint(type="int", min=1, max=50),
    "require_promo": TypeConstraint(type="bool"),
}


async def register_kakao_direct_namespace(settings: SettingsRepository) -> None:
    """Register the `kakao_direct.*` namespace and seed defaults."""
    settings.register_namespace(_NAMESPACE, _KEY_CONSTRAINTS)

    defaults: dict[str, object] = {
        "kakao_direct.proxy_checkout": [],
        "kakao_direct.proxy_promotion": [],
        "kakao_direct.max_concurrent": 3,
        "kakao_direct.run_timeout_seconds": 300,
        "kakao_direct.stripe_request_timeout_seconds": 30,
        "kakao_direct.approve_error_retries": 1,
        "kakao_direct.require_promo": True,
    }
    for key, default_value in defaults.items():
        if await settings.get(key) is None:
            await settings.set(key, default_value)


def register_kakao_direct_handler(
    job_manager: JobManager,
    settings: SettingsRepository,
    session_cache: AccountSessionCache,
    qr_output_dir: Path,
    logger_factory: Callable[[str], logging.Logger] = logging.getLogger,
) -> KakaoDirectFlowHandler:
    """Build the handler and register it into `JobManager` as `kakao_direct`."""
    handler = KakaoDirectFlowHandler(
        settings=settings,
        session_cache=session_cache,
        qr_output_dir=qr_output_dir,
        logger_factory=logger_factory,
    )
    job_manager.register_handler(_PAYMENT_METHOD, handler)
    return handler
