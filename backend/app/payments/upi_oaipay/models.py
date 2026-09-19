"""Typed results for the OaiPay client (`payments/upi_oaipay/`).

Payment_Module_Boundary: imports NOTHING from `app.core.*` or
`app.payments.ideal`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CaptchaConfig:
    """Parsed GET `/api/captcha/config` response."""

    provider: str
    site_key: str | None


@dataclass(frozen=True)
class TurnstileToken:
    """YesCaptcha solution — token + solver-reported UA (load-bearing for OaiPay)."""

    token: str
    user_agent: str | None


@dataclass(frozen=True)
class OaipayProgressEvent:
    """Non-final SSE event (`started` / `progress` / `error`)."""

    type: str
    step: int | None = None
    total: int | None = None
    desc: str | None = None
    active: int | None = None
    max_active: int | None = None
    queued: int | None = None


@dataclass(frozen=True)
class LongLinkResult:
    """Parsed final `done` event result from long-link-stream."""

    ok: bool
    provider_redirect_url: str | None
    long_url: str | None
    stripe_redirect_url: str | None
    cs_id: str | None
    upi_intent_id: str | None
    upi_expires_at: str | None
    fallback: bool = False
    provider_error: str | None = None


__all__ = [
    "CaptchaConfig",
    "TurnstileToken",
    "OaipayProgressEvent",
    "LongLinkResult",
]
