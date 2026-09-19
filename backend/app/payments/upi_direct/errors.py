"""Exception hierarchy for the UPI Direct flow (`payments/upi_direct/`)."""

from __future__ import annotations


class UpiDirectFlowError(Exception):
    def __init__(self, error_code: str, step: str, message: str | None = None) -> None:
        if not error_code:
            raise ValueError("UpiDirectFlowError requires a non-empty 'error_code'")
        if not step:
            raise ValueError("UpiDirectFlowError requires a non-empty 'step'")
        self.error_code: str = error_code
        self.step: str = step
        super().__init__(message or f"[{step}] {error_code}")


class ConfigError(UpiDirectFlowError):
    def __init__(self, detail: str | None = None, message: str | None = None) -> None:
        self.detail = detail
        super().__init__(
            error_code="upi_direct_config_invalid",
            step="validate_config",
            message=message or f"UPI Direct config invalid (detail={detail})",
        )


class LoginStepError(UpiDirectFlowError):
    def __init__(
        self,
        reason: str,
        message: str | None = None,
        *,
        is_transport: bool = False,
    ) -> None:
        self.reason = reason
        self.is_transport = is_transport
        super().__init__(
            error_code="login_failed",
            step="login",
            message=message or f"Login failed: reason={reason}",
        )


class RunTimeoutError(UpiDirectFlowError):
    def __init__(
        self, timeout_seconds: float | None = None, message: str | None = None
    ) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(
            error_code="upi_direct_run_timeout",
            step="run",
            message=message
            or f"UPI Direct run timed out (timeout_seconds={timeout_seconds})",
        )


class NotImplementedStepError(UpiDirectFlowError):
    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            error_code="not_implemented",
            step="post_login",
            message=message
            or "UPI Direct post-login steps not implemented in this phase",
        )


class CheckoutError(UpiDirectFlowError):
    def __init__(self, detail: str | None = None, message: str | None = None) -> None:
        self.detail = detail
        super().__init__(
            error_code="checkout_failed",
            step="checkout",
            message=message or f"Checkout failed (detail={detail})",
        )


class PromoUpdateError(UpiDirectFlowError):
    def __init__(self, detail: str | None = None, message: str | None = None) -> None:
        self.detail = detail
        super().__init__(
            error_code="promo_update_failed",
            step="promo_update",
            message=message or f"Promo update failed (detail={detail})",
        )


class ProcessorEntityMismatchError(UpiDirectFlowError):
    def __init__(
        self, actual: str | None = None, message: str | None = None
    ) -> None:
        self.actual = actual
        super().__init__(
            error_code="processor_entity_mismatch",
            step="checkout",
            message=message
            or f"Processor entity mismatch (expected=openai_llc actual={actual})",
        )


class StripeInitError(UpiDirectFlowError):
    def __init__(self, detail: str | None = None, message: str | None = None) -> None:
        self.detail = detail
        super().__init__(
            error_code="stripe_init_failed",
            step="stripe_init",
            message=message or f"Stripe init failed (detail={detail})",
        )


class AmountUnverifiedError(UpiDirectFlowError):
    def __init__(self, detail: str | None = None, message: str | None = None) -> None:
        self.detail = detail
        super().__init__(
            error_code="amount_unverified",
            step="amount_gate",
            message=message or f"Amount/currency unverified (detail={detail})",
        )


class NoFreeOfferError(UpiDirectFlowError):
    def __init__(self, amount_minor: int | None = None, message: str | None = None) -> None:
        self.amount_minor = amount_minor
        super().__init__(
            error_code="no_free_offer",
            step="amount_gate",
            message=message or f"Free offer required but amount={amount_minor}",
        )


class ElementsError(UpiDirectFlowError):
    def __init__(self, detail: str | None = None, message: str | None = None) -> None:
        self.detail = detail
        super().__init__(
            error_code="elements_failed",
            step="elements",
            message=message or f"Elements session failed (detail={detail})",
        )


class StripeTokenError(UpiDirectFlowError):
    def __init__(self, detail: str | None = None, message: str | None = None) -> None:
        self.detail = detail
        super().__init__(
            error_code="stripe_token_failed",
            step="stripe_token",
            message=message or f"Stripe token config failed (detail={detail})",
        )


class AlreadyPaidError(UpiDirectFlowError):
    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            error_code="already_paid",
            step="checkout",
            message=message or "Account already has active Plus",
        )


class AlreadyPaidConflictError(UpiDirectFlowError):
    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            error_code="already_paid_conflict",
            step="checkout",
            message=message or "Already-paid marker present but live plan is free",
        )


class AlreadyPaidUnverifiedError(UpiDirectFlowError):
    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            error_code="already_paid_unverified",
            step="checkout",
            message=message or "Already-paid marker present but entitlement unverified",
        )


class ConfirmFailedError(UpiDirectFlowError):
    def __init__(self, detail: str | None = None, message: str | None = None) -> None:
        self.detail = detail
        super().__init__(
            error_code="confirm_failed",
            step="confirm",
            message=message or f"UPI confirm failed (detail={detail})",
        )


class ApproveFailedError(UpiDirectFlowError):
    def __init__(
        self,
        detail: str | None = None,
        message: str | None = None,
        *,
        error_code: str = "approve_failed",
        result: str | None = None,
        http_status: int | None = None,
    ) -> None:
        self.detail = detail
        self.result = result
        self.http_status = http_status
        super().__init__(
            error_code=error_code,
            step="approve",
            message=message or f"Approve failed (detail={detail})",
        )

    @classmethod
    def from_outcome(
        cls,
        *,
        http_status: int,
        result: str | None,
        detail: str,
    ) -> "ApproveFailedError":
        if result == "blocked":
            return cls(
                detail=detail,
                error_code="approve_blocked",
                result=result,
                http_status=http_status,
                message=(
                    "Account blocked by ChatGPT approve (result=blocked) "
                    f"(http_status={http_status}, detail={detail})"
                ),
            )
        if result in {"needs_review", "pending_review", "review_required"}:
            return cls(
                detail=detail,
                error_code="approve_needs_review",
                result=result,
                http_status=http_status,
                message=(
                    "ChatGPT approve requires review "
                    f"(result={result}, http_status={http_status}, detail={detail})"
                ),
            )
        if result == "invalid_promotion":
            return cls(
                detail=detail,
                error_code="approve_invalid_promotion",
                result=result,
                http_status=http_status,
                message=(
                    "ChatGPT approve rejected invalid promotion "
                    f"(http_status={http_status}, detail={detail})"
                ),
            )
        return cls(
            detail=detail,
            result=result,
            http_status=http_status,
            message=f"Approve failed (http_status={http_status}, detail={detail})",
        )


class ApproveOutcomeUnknownError(UpiDirectFlowError):
    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            error_code="approve_outcome_unknown",
            step="approve",
            message=message or "Approve outcome ambiguous after read reconciliation",
        )


class NoQrFoundError(UpiDirectFlowError):
    def __init__(self, detail: str | None = None, message: str | None = None) -> None:
        self.detail = detail
        super().__init__(
            error_code="no_qr_found",
            step="qr",
            message=message or f"No bound valid QR (detail={detail})",
        )


class StripeSetupFailedError(UpiDirectFlowError):
    def __init__(self, detail: str | None = None, message: str | None = None) -> None:
        self.detail = detail
        super().__init__(
            error_code="stripe_setup_failed",
            step="qr",
            message=message or f"Stripe setup did not create QR (detail={detail})",
        )


class QrFetchError(UpiDirectFlowError):
    def __init__(self, detail: str | None = None, message: str | None = None) -> None:
        self.detail = detail
        super().__init__(
            error_code="qr_fetch_failed",
            step="qr",
            message=message or f"QR fetch/validate failed (detail={detail})",
        )


class QrRenderError(UpiDirectFlowError):
    def __init__(self, detail: str | None = None, message: str | None = None) -> None:
        self.detail = detail
        super().__init__(
            error_code="qr_render_failed",
            step="qr",
            message=message or f"QR render failed (detail={detail})",
        )


__all__ = [
    "UpiDirectFlowError",
    "ConfigError",
    "LoginStepError",
    "RunTimeoutError",
    "NotImplementedStepError",
    "CheckoutError",
    "PromoUpdateError",
    "ProcessorEntityMismatchError",
    "StripeInitError",
    "AmountUnverifiedError",
    "NoFreeOfferError",
    "ElementsError",
    "StripeTokenError",
    "AlreadyPaidError",
    "AlreadyPaidConflictError",
    "AlreadyPaidUnverifiedError",
    "ConfirmFailedError",
    "ApproveFailedError",
    "ApproveOutcomeUnknownError",
    "NoQrFoundError",
    "StripeSetupFailedError",
    "QrFetchError",
    "QrRenderError",
]
