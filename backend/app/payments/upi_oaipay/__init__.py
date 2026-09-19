"""UPI no-CDK (OaiPay long-link-stream) payment channel (`payments/upi_oaipay/`).

Method key: `upi_nocdk`. Settings namespace: `oaipay.*`.

Importing this package is side-effect-free — registration happens via
`register_oaipay_namespace` / `register_oaipay_handler` (wired in bootstrap).

Payment_Module_Boundary: only `app.core.*` + `app.payments._chatgpt.*` +
`app.payments.upi_oaipay.*`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Final

from app.core import http_client as http
from app.core.job_manager import JobManager
from app.core.session_cache import AccountSessionCache
from app.core.settings_store import SettingsRepository, TypeConstraint
from app.payments.upi_oaipay.flow import OaipayFlowHandler
from app.payments.upi_oaipay.oaipay_client import OaipayClient

__all__ = [
    "register_oaipay_namespace",
    "register_oaipay_handler",
]

_NAMESPACE: Final[str] = "oaipay"
_PAYMENT_METHOD: Final[str] = "upi_nocdk"
_DEFAULT_BASE_URL: Final[str] = "https://oaipay.12001234.xyz"

_OAIPAY_KEY_CONSTRAINTS: Final[dict[str, TypeConstraint]] = {
    "base_url": TypeConstraint(type="string", min=1, max=300),
    "proxy_checkout": TypeConstraint(type="list_str"),
    "proxy_promotion": TypeConstraint(type="list_str"),
    "yescaptcha_api_key": TypeConstraint(type="string", min=0, max=200),
    "max_concurrent": TypeConstraint(type="int", min=1, max=10),
    "run_timeout_seconds": TypeConstraint(type="int", min=1, max=3600),
}


async def register_oaipay_namespace(settings: SettingsRepository) -> None:
    """Register the `oaipay.*` namespace and seed defaults (only when unset)."""
    settings.register_namespace(_NAMESPACE, _OAIPAY_KEY_CONSTRAINTS)

    defaults: dict[str, object] = {
        "oaipay.base_url": _DEFAULT_BASE_URL,
        "oaipay.proxy_checkout": [],
        "oaipay.proxy_promotion": [],
        "oaipay.yescaptcha_api_key": "",
        "oaipay.max_concurrent": 3,
        "oaipay.run_timeout_seconds": 300,
    }
    for key, default_value in defaults.items():
        if await settings.get(key) is None:
            await settings.set(key, default_value)


def register_oaipay_handler(
    job_manager: JobManager,
    settings: SettingsRepository,
    session_cache: AccountSessionCache,
    qr_output_dir: Path,
    logger_factory: Callable[[str], logging.Logger] = logging.getLogger,
) -> OaipayFlowHandler:
    """Build the handler and register it into `JobManager` as `"upi_nocdk`."""

    def _client_factory(
        session: http.AsyncSession,
        base_url: str,
        known_secrets: list[str],
        logger: logging.Logger,
    ) -> OaipayClient:
        return OaipayClient(
            session,
            base_url=base_url,
            logger=logger,
            known_secrets=known_secrets,
        )

    handler = OaipayFlowHandler(
        settings=settings,
        session_cache=session_cache,
        qr_output_dir=qr_output_dir,
        logger_factory=logger_factory,
        oaipay_client_factory=_client_factory,
    )
    job_manager.register_handler(_PAYMENT_METHOD, handler)
    return handler
