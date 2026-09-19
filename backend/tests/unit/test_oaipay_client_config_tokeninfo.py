"""Unit tests for OaipayClient captcha config + token_info."""

from __future__ import annotations

import logging

import pytest

from app.payments.upi_oaipay.errors import CaptchaConfigError
from app.payments.upi_oaipay.models import CaptchaConfig
from app.payments.upi_oaipay.oaipay_client import OaipayClient
from tests.support.fake_http import FakeAsyncSession, FakeResponse

BASE = "https://oaipay.12001234.xyz"
ACCESS = "eyJ.access.TOKEN_MARKER_oaipay"


def _client(session: FakeAsyncSession) -> OaipayClient:
    logger = logging.getLogger("test.oaipay.client")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return OaipayClient(session, base_url=BASE, logger=logger, known_secrets=[ACCESS])


async def test_get_captcha_config_ok() -> None:
    session = FakeAsyncSession()
    session.route(
        "GET",
        f"{BASE}/api/captcha/config",
        FakeResponse(json_body={"provider": "turnstile", "siteKey": "0xkey"}),
    )
    cfg = await _client(session).get_captcha_config()
    assert isinstance(cfg, CaptchaConfig)
    assert cfg.provider == "turnstile"
    assert cfg.site_key == "0xkey"


async def test_get_captcha_config_non_2xx() -> None:
    session = FakeAsyncSession()
    session.route(
        "GET",
        f"{BASE}/api/captcha/config",
        FakeResponse(status_code=500, text="err"),
    )
    with pytest.raises(CaptchaConfigError):
        await _client(session).get_captcha_config()


async def test_get_captcha_config_invalid_json() -> None:
    session = FakeAsyncSession()
    session.route(
        "GET",
        f"{BASE}/api/captcha/config",
        FakeResponse(status_code=200, text="not-json"),
    )
    with pytest.raises(CaptchaConfigError):
        await _client(session).get_captcha_config()


async def test_token_info_never_raises() -> None:
    session = FakeAsyncSession()
    session.route(
        "POST",
        f"{BASE}/api/token-info",
        FakeResponse(status_code=500, text="boom"),
    )
    result = await _client(session).token_info(
        ACCESS, {"checkout": "a", "promotion": "b"}
    )
    assert result is None
