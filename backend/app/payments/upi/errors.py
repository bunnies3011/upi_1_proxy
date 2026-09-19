"""Exception hierarchy for the UPI vendor flow (`payments/upi/`).

Every subclass of `UpiFlowError` is a foreseen domain failure (Fail_Fast_Policy):
it carries a non-empty `error_code` (a stable code exposed via API/log) and a
non-empty `step` (the flow step that failed, used for realtime logging). The UPI
flow handler (later phase) catches `UpiFlowError` at a single boundary and maps
it to `JobResult(status=ERROR, error_code=...)`. A bug not modelled here is left
to propagate — there is no catch-all at the UPI boundary.

The full hierarchy is defined here up front (not split across phases) so the
vendor client and the license pool can import and raise these without a forward
dependency. Wiring `error_code` → `JobResult` happens in the handler phase.

Payment_Module_Boundary: this module imports NOTHING from `app.core.*`.
`UpiFlowError` extends the built-in `Exception` — each payment module owns an
independent exception hierarchy.
"""

from __future__ import annotations


class UpiFlowError(Exception):
    """Base for every foreseen domain failure in the UPI flow.

    Attributes:
        error_code: Stable error code exposed via API/log. Non-empty.
        step: Flow step where the error occurred (realtime logging). Non-empty.

    Raises:
        ValueError: If `error_code` or `step` is empty — that is a programming
            error (a subclass defined incorrectly), not a runtime flow error, so
            it fails fast where the exception is constructed.
    """

    def __init__(self, error_code: str, step: str, message: str | None = None) -> None:
        if not error_code:
            raise ValueError(
                "UpiFlowError requires a non-empty 'error_code' (stable code "
                "exposed via API/log)."
            )
        if not step:
            raise ValueError(
                "UpiFlowError requires a non-empty 'step' (used for realtime "
                "logging to identify the flow step that failed)."
            )
        self.error_code: str = error_code
        self.step: str = step
        super().__init__(message or f"[{step}] {error_code}")


class LicenseError(UpiFlowError):
    """A `key/verify` call failed at the transport/HTTP/parse level, so the
    license state for the PK codes could not be established.

    Attributes:
        detail: Redacted description of the failure cause, if available.
    """

    def __init__(self, detail: str | None = None, message: str | None = None) -> None:
        self.detail: str | None = detail
        super().__init__(
            error_code="upi_license_error",
            step="license_verify",
            message=message or f"License verify failed (detail={detail})",
        )


class LicenseExhaustedError(UpiFlowError):
    """No PK code in the pool has remaining credit — a durable failure that
    needs an operator to top up the pool (NOT auto-retryable).

    Attributes:
        checked_codes: Number of codes inspected before giving up.
    """

    def __init__(self, checked_codes: int = 0, message: str | None = None) -> None:
        self.checked_codes: int = checked_codes
        super().__init__(
            error_code="upi_no_license_credit",
            step="license_acquire",
            message=message
            or f"No UPI license code has remaining credit (checked {checked_codes})",
        )


class ChallengeError(UpiFlowError):
    """The `chatgpt/challenge` call failed or returned an unusable payload
    (missing `nonce`/`expires`/`mac`).

    Attributes:
        detail: Redacted description of the failure cause, if available.
    """

    def __init__(self, detail: str | None = None, message: str | None = None) -> None:
        self.detail: str | None = detail
        super().__init__(
            error_code="upi_challenge_error",
            step="challenge",
            message=message or f"Challenge request failed (detail={detail})",
        )


class EligibilityError(UpiFlowError):
    """The `account/check` precheck failed or reported the account ineligible
    for the UPI vendor flow.

    Attributes:
        detail: Redacted description of the failure cause, if available.
    """

    def __init__(self, detail: str | None = None, message: str | None = None) -> None:
        self.detail: str | None = detail
        super().__init__(
            error_code="upi_ineligible",
            step="eligibility",
            message=message or f"Eligibility precheck failed (detail={detail})",
        )


class VendorRunError(UpiFlowError):
    """The vendor `run` completed but reported a business failure
    (`result.ok=false`) — the vendor could not produce a QR.

    Attributes:
        vendor_code: The vendor's `result.code`, if present.
        detail: Redacted description of the failure cause, if available.
    """

    def __init__(
        self,
        vendor_code: str | None = None,
        detail: str | None = None,
        message: str | None = None,
    ) -> None:
        self.vendor_code: str | None = vendor_code
        self.detail: str | None = detail
        super().__init__(
            error_code="upi_run_failed",
            step="run",
            message=message
            or f"Vendor run reported failure (code={vendor_code}, detail={detail})",
        )


class VendorStreamError(UpiFlowError):
    """The NDJSON `run` stream was truncated, malformed, or hit a transport
    error before a final `done` event arrived. Transient candidate for
    `upi.auto_retry_blocked_codes`.

    Attributes:
        detail: Redacted description of the failure cause, if available.
    """

    def __init__(self, detail: str | None = None, message: str | None = None) -> None:
        self.detail: str | None = detail
        super().__init__(
            error_code="upi_stream_error",
            step="run",
            message=message or f"Vendor NDJSON stream failed (detail={detail})",
        )


class VendorRunCancelled(UpiFlowError):
    """The `run` stream was interrupted because the cancellation token was set.
    Terminal (the job is not auto-retried) — mapped to STOPPED by the handler.
    """

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            error_code="upi_run_cancelled",
            step="run",
            message=message or "Vendor run cancelled by request",
        )


class VendorTimeoutError(UpiFlowError):
    """The `run` wall-clock cap (`upi.run_timeout_seconds`) elapsed before the
    stream produced a final result.

    Attributes:
        timeout_seconds: The wall-clock cap that elapsed, if known.
    """

    def __init__(
        self, timeout_seconds: float | None = None, message: str | None = None
    ) -> None:
        self.timeout_seconds: float | None = timeout_seconds
        super().__init__(
            error_code="upi_run_timeout",
            step="run",
            message=message or f"Vendor run timed out (timeout_seconds={timeout_seconds})",
        )


class QrDecodeError(UpiFlowError):
    """Decoding the `qr_image_png` data URI into PNG bytes failed (bad prefix,
    invalid base64, or empty payload).

    Attributes:
        reason: Short description of what was wrong with the data URI, if known.
    """

    def __init__(self, reason: str | None = None, message: str | None = None) -> None:
        self.reason: str | None = reason
        super().__init__(
            error_code="upi_qr_decode_failed",
            step="qr_decode",
            message=message or f"Failed to decode UPI QR image (reason={reason})",
        )


__all__ = [
    "UpiFlowError",
    "LicenseError",
    "LicenseExhaustedError",
    "ChallengeError",
    "EligibilityError",
    "VendorRunError",
    "VendorStreamError",
    "VendorRunCancelled",
    "VendorTimeoutError",
    "QrDecodeError",
]
