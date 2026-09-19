"""Unit tests for OaiPay frozen models."""

from __future__ import annotations

import dataclasses

import pytest

from app.payments.upi_oaipay.models import (
    CaptchaConfig,
    LongLinkResult,
    OaipayProgressEvent,
    TurnstileToken,
)


def test_captcha_config_frozen() -> None:
    cfg = CaptchaConfig(provider="turnstile", site_key="0xkey")
    assert cfg.provider == "turnstile"
    assert cfg.site_key == "0xkey"
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.provider = "local"  # type: ignore[misc]


def test_turnstile_token() -> None:
    tok = TurnstileToken(token="tok", user_agent="UA/1")
    assert tok.token == "tok"
    assert tok.user_agent == "UA/1"


def test_progress_event_defaults() -> None:
    ev = OaipayProgressEvent(type="started")
    assert ev.step is None
    assert ev.queued is None


def test_long_link_result_defaults() -> None:
    r = LongLinkResult(
        ok=True,
        provider_redirect_url="data:image/png;base64,AA",
        long_url="https://pay.example/x",
        stripe_redirect_url=None,
        cs_id=None,
        upi_intent_id=None,
        upi_expires_at=None,
    )
    assert r.fallback is False
    assert r.provider_error is None
