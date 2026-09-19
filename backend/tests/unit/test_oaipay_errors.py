"""Unit tests for OaiPay error hierarchy."""

from __future__ import annotations

import pytest

from app.payments.upi_oaipay.errors import (
    CaptchaConfigError,
    CaptchaSolveError,
    ConfigError,
    OaipayFlowError,
    QrDecodeError,
    RunCancelledError,
    RunFailedError,
    RunTimeoutError,
    StreamError,
)


def test_base_rejects_empty_error_code() -> None:
    with pytest.raises(ValueError, match="error_code"):
        OaipayFlowError(error_code="", step="x")


def test_base_rejects_empty_step() -> None:
    with pytest.raises(ValueError, match="step"):
        OaipayFlowError(error_code="x", step="")


@pytest.mark.parametrize(
    ("cls", "error_code", "step"),
    [
        (CaptchaConfigError, "oaipay_captcha_config_error", "captcha_config"),
        (CaptchaSolveError, "oaipay_captcha_error", "captcha_solve"),
        (StreamError, "oaipay_stream_error", "long_link_stream"),
        (RunFailedError, "oaipay_run_failed", "long_link_stream"),
        (RunCancelledError, "oaipay_run_cancelled", "long_link_stream"),
        (RunTimeoutError, "oaipay_run_timeout", "long_link_stream"),
        (QrDecodeError, "oaipay_qr_decode_failed", "qr_decode"),
        (ConfigError, "oaipay_config_invalid", "validate_config"),
    ],
)
def test_subclass_codes(cls, error_code: str, step: str) -> None:
    if cls is RunTimeoutError:
        exc = cls(timeout_seconds=30.0)
    elif cls is RunCancelledError:
        exc = cls()
    elif cls is QrDecodeError:
        exc = cls(reason="x")
    else:
        exc = cls(detail="x")
    assert exc.error_code == error_code
    assert exc.step == step
