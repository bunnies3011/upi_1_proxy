"""Exception hierarchy for the OaiPay UPI no-CDK flow (`payments/upi_oaipay/`).

Payment_Module_Boundary: imports NOTHING from `app.core.*`.
"""

from __future__ import annotations


class OaipayFlowError(Exception):
    """Base for every foreseen domain failure in the OaiPay flow."""

    def __init__(self, error_code: str, step: str, message: str | None = None) -> None:
        if not error_code:
            raise ValueError(
                "OaipayFlowError requires a non-empty 'error_code' (stable code "
                "exposed via API/log)."
            )
        if not step:
            raise ValueError(
                "OaipayFlowError requires a non-empty 'step' (used for realtime "
                "logging to identify the flow step that failed)."
            )
        self.error_code: str = error_code
        self.step: str = step
        super().__init__(message or f"[{step}] {error_code}")


class CaptchaConfigError(OaipayFlowError):
    """GET `/api/captcha/config` failed or returned a malformed payload."""

    def __init__(self, detail: str | None = None, message: str | None = None) -> None:
        self.detail: str | None = detail
        super().__init__(
            error_code="oaipay_captcha_config_error",
            step="captcha_config",
            message=message or f"Captcha config failed (detail={detail})",
        )


class CaptchaSolveError(OaipayFlowError):
    """YesCaptcha create/poll failed, timed out, empty token, or cancelled."""

    def __init__(self, detail: str | None = None, message: str | None = None) -> None:
        self.detail: str | None = detail
        super().__init__(
            error_code="oaipay_captcha_error",
            step="captcha_solve",
            message=message or f"Captcha solve failed (detail={detail})",
        )


class StreamError(OaipayFlowError):
    """SSE stream truncated/malformed/transport/non-2xx (transient candidate)."""

    def __init__(self, detail: str | None = None, message: str | None = None) -> None:
        self.detail: str | None = detail
        super().__init__(
            error_code="oaipay_stream_error",
            step="long_link_stream",
            message=message or f"OaiPay SSE stream failed (detail={detail})",
        )


class RunFailedError(OaipayFlowError):
    """`done` event reported business failure (ok=false / fallback / provider_error)."""

    def __init__(self, detail: str | None = None, message: str | None = None) -> None:
        self.detail: str | None = detail
        super().__init__(
            error_code="oaipay_run_failed",
            step="long_link_stream",
            message=message or f"OaiPay run failed (detail={detail})",
        )


class AlreadyPaidError(OaipayFlowError):
    """Checkout rejected because the account is already Plus / paid.

    Durable account state — not a transient failure. Flow maps this to
    JobResult(ERROR, plan="plus") so Free re-run export excludes the row.
    """

    def __init__(self, detail: str | None = None, message: str | None = None) -> None:
        self.detail: str | None = detail
        super().__init__(
            error_code="oaipay_already_paid",
            step="long_link_stream",
            message=message or f"User is already paid (detail={detail})",
        )


class RunCancelledError(OaipayFlowError):
    """Cancellation token set mid-stream → STOPPED."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            error_code="oaipay_run_cancelled",
            step="long_link_stream",
            message=message or "OaiPay run cancelled by request",
        )


class RunTimeoutError(OaipayFlowError):
    """Wall-clock cap elapsed (transient auto-retry candidate)."""

    def __init__(
        self, timeout_seconds: float | None = None, message: str | None = None
    ) -> None:
        self.timeout_seconds: float | None = timeout_seconds
        super().__init__(
            error_code="oaipay_run_timeout",
            step="long_link_stream",
            message=message
            or f"OaiPay run timed out (timeout_seconds={timeout_seconds})",
        )


class QrDecodeError(OaipayFlowError):
    """`provider_redirect_url` missing prefix / bad base64 / empty."""

    def __init__(self, reason: str | None = None, message: str | None = None) -> None:
        self.reason: str | None = reason
        super().__init__(
            error_code="oaipay_qr_decode_failed",
            step="qr_decode",
            message=message or f"Failed to decode OaiPay QR image (reason={reason})",
        )


class ConfigError(OaipayFlowError):
    """Generic pre-flight config failure (empty pool, bad base_url, …)."""

    def __init__(self, detail: str | None = None, message: str | None = None) -> None:
        self.detail: str | None = detail
        super().__init__(
            error_code="oaipay_config_invalid",
            step="validate_config",
            message=message or f"OaiPay config invalid (detail={detail})",
        )


__all__ = [
    "OaipayFlowError",
    "CaptchaConfigError",
    "CaptchaSolveError",
    "StreamError",
    "RunFailedError",
    "AlreadyPaidError",
    "RunCancelledError",
    "RunTimeoutError",
    "QrDecodeError",
    "ConfigError",
]
