"""KakaoDirectFlowHandler - UPI Direct login shell with Kakao Pay link output."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Final

from app.core.session_cache import AccountSessionCache
from app.core.settings_store import SettingsRepository
from app.payments.kakao_direct.post_login_flow import run_post_login
from app.payments.upi_direct.flow import UpiDirectFlowHandler

_SETTING_MAX_CONCURRENT: Final[str] = "kakao_direct.max_concurrent"
_SETTING_RUN_TIMEOUT: Final[str] = "kakao_direct.run_timeout_seconds"
_SETTING_REQUIRE_PROMO: Final[str] = "kakao_direct.require_promo"


class KakaoDirectFlowHandler(UpiDirectFlowHandler):
    def __init__(
        self,
        *,
        settings: SettingsRepository,
        session_cache: AccountSessionCache,
        qr_output_dir: Path,
        logger_factory: Callable[[str], logging.Logger],
    ) -> None:
        super().__init__(
            settings=settings,
            session_cache=session_cache,
            qr_output_dir=qr_output_dir,
            logger_factory=logger_factory,
            payment_method="kakao_direct",
            max_concurrent_key=_SETTING_MAX_CONCURRENT,
            run_timeout_key=_SETTING_RUN_TIMEOUT,
            require_promo_key=_SETTING_REQUIRE_PROMO,
            proxy_checkout_key="kakao_direct.proxy_checkout",
            proxy_promotion_key="kakao_direct.proxy_promotion",
            post_login_runner=run_post_login,
        )


__all__ = ["KakaoDirectFlowHandler"]
