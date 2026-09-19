"""GcashDirectFlowHandler - UPI Direct login shell with GCash link output."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Final

from app.core.payment_flow import Job
from app.core.proxy_pool import ProxyLease
from app.core.session_cache import AccountSessionCache
from app.core.settings_store import SettingsRepository
from app.payments.gcash_direct.post_login_flow import run_post_login
from app.payments.upi_direct.flow import UpiDirectFlowHandler

_SETTING_MAX_CONCURRENT: Final[str] = "gcash_direct.max_concurrent"
_SETTING_RUN_TIMEOUT: Final[str] = "gcash_direct.run_timeout_seconds"
_SETTING_REQUIRE_PROMO: Final[str] = "gcash_direct.require_promo"


class GcashDirectFlowHandler(UpiDirectFlowHandler):
    def __init__(
        self,
        *,
        settings: SettingsRepository,
        session_cache: AccountSessionCache,
        qr_output_dir: Path,
        logger_factory: Callable[[str], logging.Logger],
        plus_signal_callback: Callable[[str, str], Awaitable[Any]] | None = None,
    ) -> None:
        self._plus_signal_callback = plus_signal_callback
        super().__init__(
            settings=settings,
            session_cache=session_cache,
            qr_output_dir=qr_output_dir,
            logger_factory=logger_factory,
            payment_method="gcash_direct",
            max_concurrent_key=_SETTING_MAX_CONCURRENT,
            run_timeout_key=_SETTING_RUN_TIMEOUT,
            require_promo_key=_SETTING_REQUIRE_PROMO,
            proxy_checkout_key="gcash_direct.proxy_checkout",
            proxy_promotion_key="gcash_direct.proxy_promotion",
            post_login_runner=run_post_login,
        )

    async def _extra_post_login_runner_kwargs(
        self,
        *,
        job: Job,
        proxy_lease: ProxyLease | None,
    ) -> dict[str, Any]:
        if self._plus_signal_callback is None:
            return {}
        return {"plus_signal_callback": self._plus_signal_callback}


__all__ = ["GcashDirectFlowHandler"]
