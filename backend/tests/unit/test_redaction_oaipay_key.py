"""Redaction field-name backstop for OaiPay YesCaptcha key."""

from __future__ import annotations

from app.core.redaction import redact_dict

_MASK = "***REDACTED***"


def test_redact_yescaptcha_api_key() -> None:
    result = redact_dict({"yescaptcha_api_key": "sk-abc", "other": "ok"})
    assert result["yescaptcha_api_key"] == _MASK
    assert result["other"] == "ok"


def test_redact_api_key() -> None:
    result = redact_dict({"api_key": "sk-abc", "label": "x"})
    assert result["api_key"] == _MASK
    assert result["label"] == "x"
