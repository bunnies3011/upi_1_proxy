"""Exception hierarchy cho luồng nghiệp vụ iDEAL (`payments/ideal/`).

Toàn bộ subclass của `IdealFlowError` là lỗi domain đã lường trước theo
Fail_Fast_Policy (Requirement 14.1, 14.3, 14.4): mỗi lỗi mang `error_code`
non-empty (mã lỗi ổn định lộ ra API/log) và `step` non-empty (bước flow xảy
ra lỗi, dùng để log realtime định danh chính xác điểm lỗi).

`IdealFlowHandler.run()` bắt boundary DUY NHẤT `IdealFlowError` ở cuối
method và map sang `JobResult(status=ERROR, error_code=e.error_code,
error_message=redact(str(e)))`. Exception không thuộc hierarchy này (bug
nội tại chưa lường trước) được để propagate lên `Job_Manager` — KHÔNG
catch-all ở boundary iDEAL.

Payment_Module_Boundary: module này KHÔNG import bất kỳ thứ gì từ
`app.core.*`. `IdealFlowError` kế thừa `Exception` trần, KHÔNG kế thừa
`CoreError` (thuộc `app.core.errors`) — mỗi payment module có exception
hierarchy độc lập của riêng nó.

_Requirements: 1.4, 1.7, 2.2, 2.4, 2.6, 3.4, 3.5, 4.2, 4.6, 4.8, 5.5, 5.6,
6.3, 7.6, 14.1, 14.3, 14.4_
"""

from __future__ import annotations

# Re-export the shared ChatGPT login error so it is discoverable from the iDEAL
# error module. `ChatgptLoginError` is what the shared login surface raises; the
# iDEAL `LoginError` below stays an `IdealFlowError` subclass (its own hierarchy
# + tests). Both are caught at `IdealFlowHandler.run()`'s boundary.
from app.payments._chatgpt.errors import ChatgptLoginError


class IdealFlowError(Exception):
    """Base cho MỌI lỗi domain trong luồng iDEAL.

    Payment_Module_Boundary: KHÔNG kế thừa `CoreError` — `payments/ideal/`
    có hierarchy exception hoàn toàn độc lập với `core/`.

    Attributes:
        error_code: Mã lỗi ổn định lộ ra API/log. Non-empty.
        step: Bước flow xảy ra lỗi (dùng cho log realtime). Non-empty.

    Raises:
        ValueError: Nếu `error_code` hoặc `step` rỗng — đây là lỗi lập
            trình (subclass định nghĩa sai), không phải lỗi runtime của
            flow, nên fail fast ngay tại chỗ định nghĩa exception.
    """

    def __init__(self, error_code: str, step: str, message: str | None = None) -> None:
        if not error_code:
            raise ValueError(
                "IdealFlowError requires a non-empty 'error_code' (stable "
                "error code exposed via API/log — Requirement 14.4)."
            )
        if not step:
            raise ValueError(
                "IdealFlowError requires a non-empty 'step' (used for "
                "realtime logging to identify the flow step that failed)."
            )

        self.error_code: str = error_code
        self.step: str = step
        super().__init__(message or f"[{step}] {error_code}")


class LoginError(IdealFlowError):
    """Requirement 1.4: đăng nhập ChatGPT pure-HTTP thất bại.

    Attributes:
        reason: 1 trong `invalid_credential`, `mfa_required`,
            `account_locked`, `network_error` (Requirement 1.4).
    """

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason: str = reason
        super().__init__(
            error_code="login_failed",
            step="login",
            message=message or f"Login failed: reason={reason}",
        )


#: Ánh xạ tên model → giá trị `step` mặc định cho `RequiredFieldMissingError`.
#: Khi caller không truyền `step` tường minh, dùng map này để log realtime
#: định danh CHÍNH XÁC bước flow đang parse response nào (Requirement 14.2).
#: Model không nằm trong map fallback về `"parse_response"` (generic).
_MODEL_TO_STEP: dict[str, str] = {
    "CheckoutSession": "checkout",
    "StripePaymentPageInit": "stripe_init",
    "StripeElementsSession": "stripe_elements",
    "IdealTransactionInitiate": "transaction_initiate",
    "IssuerBank": "transaction_initiate",
}


class RequiredFieldMissingError(IdealFlowError):
    """Requirement 1.7, 2.2, 2.4, 5.6: response bắt buộc thiếu field —
    Fail_Fast_Policy (không suy diễn giá trị mặc định).

    Attributes:
        model / model_name: Tên model đang parse (2 alias tương đương —
            `model` là canonical dùng ở `models.py`, `model_name` giữ để
            backward-compat với test/unit cũ). Truy cập cả 2 attribute
            đều trả cùng giá trị sau khởi tạo.
        field / missing_field: Tên field bị thiếu (2 alias tương đương).

    Constructor chấp nhận cả 2 style keyword args:
        - `RequiredFieldMissingError(field=..., model=...)` (canonical mới —
          dùng bởi `models.py::parse_*`).
        - `RequiredFieldMissingError(model_name=..., missing_field=..., ...)`
          (legacy — dùng bởi test cũ).
        - Positional cũng chấp nhận theo thứ tự (model_name, missing_field)
          cho backward-compat.
    """

    def __init__(
        self,
        model_name: str | None = None,
        missing_field: str | None = None,
        step: str | None = None,
        message: str | None = None,
        *,
        field: str | None = None,
        model: str | None = None,
    ) -> None:
        # Chấp nhận cả 2 alias — canonical `field`/`model` (models.py mới)
        # và legacy `missing_field`/`model_name` (test cũ). Fail-fast nếu
        # cả 2 cùng thiếu.
        resolved_model = model if model is not None else model_name
        resolved_field = field if field is not None else missing_field
        if resolved_model is None or resolved_field is None:
            raise ValueError(
                "RequiredFieldMissingError requires 'model'/'model_name' and "
                "'field'/'missing_field' — either keyword style is accepted."
            )

        # Giữ CẢ 2 attribute alias để cả code mới (`.field`/`.model`) và test
        # cũ (`.missing_field`/`.model_name`) đều đọc được.
        self.model: str = resolved_model
        self.model_name: str = resolved_model
        self.field: str = resolved_field
        self.missing_field: str = resolved_field

        resolved_step = step or _MODEL_TO_STEP.get(resolved_model, "parse_response")
        super().__init__(
            error_code="required_field_missing",
            step=resolved_step,
            message=message
            or f"{resolved_model} response is missing required field: '{resolved_field}'",
        )


class StripeRetryExhaustedError(IdealFlowError):
    """Requirement 2.6: hết `ideal.stripe_max_retry_attempts` vẫn lỗi
    mạng/timeout/5xx trên request `init` hoặc `elements_sessions`.

    Attributes:
        request_name: `init` hoặc `elements_sessions`.
        attempts: Số lần đã thử (bao gồm lần đầu).
    """

    def __init__(self, request_name: str, attempts: int, message: str | None = None) -> None:
        self.request_name: str = request_name
        self.attempts: int = attempts
        super().__init__(
            error_code="stripe_retry_exhausted",
            step="stripe_request",
            message=message
            or f"Stripe request '{request_name}' exhausted retries after {attempts} attempt(s)",
        )


#: Ánh xạ tên request Stripe → giá trị `step` dùng khi raise
#: `StripeHttpClientError`. Giữ mapping ở module-level để `IdealFlowHandler`
#: log realtime định danh CHÍNH XÁC bước flow gặp 4xx (Requirement 2.8).
_STRIPE_REQUEST_STEP_MAP: dict[str, str] = {
    "init": "stripe_init",
    "elements_sessions": "stripe_elements",
    "update_billing": "stripe_update_billing",
    "confirm": "stripe_confirm",
    "refresh_poll": "refresh_poll",
    "follow_redirect": "follow_redirect",
}


class StripeHttpClientError(IdealFlowError):
    """Requirement 2.6: Stripe trả HTTP status 4xx (lỗi client — validate,
    auth, param invalid) trên 1 trong các request thuộc Stripe flow. Không
    retry (Requirement 2.6 cấm retry 4xx — dữ liệu đã sai thì retry cũng sai).

    Payment_Module_Boundary: exception này thuộc `payments/ideal/` — KHÔNG
    được lộ ra `core/`. `IdealFlowHandler.run()` catch tại boundary duy nhất
    `IdealFlowError`.

    Attributes:
        request_name: Tên request Stripe (khớp key của `_STRIPE_REQUEST_STEP_MAP`
            — ví dụ `init`, `elements_sessions`). Non-empty.
        http_status: Mã HTTP status Stripe trả về (400-499).
        body_snippet: Đoạn đầu response body (đã cắt ngắn) — dùng cho log
            realtime giúp debug nguyên nhân 4xx, KHÔNG log full body để tránh
            leak dữ liệu nhạy cảm/spam log. `None` nếu không đọc được body.
    """

    def __init__(
        self,
        request_name: str,
        http_status: int,
        body_snippet: str | None = None,
        message: str | None = None,
    ) -> None:
        if not request_name:
            raise ValueError(
                "StripeHttpClientError requires a non-empty 'request_name' "
                "to determine the step (realtime log — Requirement 2.8)."
            )
        step = _STRIPE_REQUEST_STEP_MAP.get(request_name, "stripe_request")
        self.request_name: str = request_name
        self.http_status: int = http_status
        self.body_snippet: str | None = body_snippet
        default_msg = (
            f"Stripe '{request_name}' returned HTTP {http_status} "
            f"(client error, not retrying)"
        )
        if body_snippet:
            default_msg = f"{default_msg} — body: {body_snippet}"
        super().__init__(
            error_code="stripe_http_4xx",
            step=step,
            message=message or default_msg,
        )


class AddressValidationRejectedError(IdealFlowError):
    """Requirement 3.4: Stripe từ chối field địa chỉ/thông tin cá nhân
    trong request `payment_pages/{id}/confirm`.

    Attributes:
        rejected_field: Tên field bị Stripe reject.
    """

    def __init__(self, rejected_field: str, message: str | None = None) -> None:
        self.rejected_field: str = rejected_field
        super().__init__(
            error_code="address_validation_rejected",
            step="stripe_confirm",
            message=message or f"Stripe rejected address field: '{rejected_field}'",
        )


class ConfirmInvalidResponseError(IdealFlowError):
    """Requirement 3.5: response `confirm` là HTTP lỗi HOẶC HTTP thành công
    nhưng thiếu trạng thái xác nhận hợp lệ.

    Attributes:
        http_status: Mã HTTP status nhận được, nếu có.
    """

    def __init__(self, http_status: int | None = None, message: str | None = None) -> None:
        self.http_status: int | None = http_status
        super().__init__(
            error_code="confirm_invalid_response",
            step="stripe_confirm",
            message=message or f"Invalid confirm response (http_status={http_status})",
        )


class ApproveFailedError(IdealFlowError):
    """Requirement 4.2: request `approve` lỗi ở bất kỳ dạng nào (lỗi kết
    nối, 4xx/5xx, hoặc 200 nhưng payload không có `result=approved`).

    Caller (`IdealFlowHandler`) KHÔNG BAO GIỜ tự gọi lại `approve` sau khi
    bắt được exception này — Requirement 4.2 cấm retry để tránh approve
    trùng lặp giao dịch.

    Attributes:
        http_status: Mã HTTP status nhận được, nếu có.
        detail: Mô tả chi tiết lỗi (payload/lý do), nếu có.
        result: Giá trị `result` trong payload ChatGPT (VD `"blocked"`,
            `"needs_review"`) khi HTTP 200 nhưng không phải `"approved"`.
            Dùng để phân loại `error_code` cụ thể ở subclass helper method
            (`from_payload`) — Fail_Fast với thông báo actionable.
    """

    def __init__(
        self,
        http_status: int | None = None,
        detail: str | None = None,
        message: str | None = None,
        error_code: str = "approve_failed",
        result: str | None = None,
    ) -> None:
        self.http_status: int | None = http_status
        self.detail: str | None = detail
        self.result: str | None = result
        super().__init__(
            error_code=error_code,
            step="checkout_approve",
            message=message
            or f"Approve failed (http_status={http_status}, detail={detail})",
        )

    @classmethod
    def from_payload(
        cls,
        http_status: int,
        payload: dict | None,
        detail: str,
    ) -> "ApproveFailedError":
        """Phân loại chi tiết `error_code` dựa trên field `result` của
        payload ChatGPT khi HTTP 200 nhưng không `result=approved`.

        Mapping:
        - `result="blocked"` → `error_code="approve_blocked"` — account
          hoặc IP/proxy bị ChatGPT anti-fraud chặn. Không phải bug tool,
          user cần đổi proxy/account hoặc chờ cooldown.
        - `result="needs_review"` / `"pending_review"` →
          `error_code="approve_needs_review"` — cần manual review từ
          ChatGPT (KYC/risk check).
        - Các case khác → `error_code="approve_failed"` (generic).
        """
        result_val: str | None = None
        if isinstance(payload, dict):
            raw = payload.get("result")
            if isinstance(raw, str):
                result_val = raw

        if result_val == "blocked":
            return cls(
                http_status=http_status,
                detail=detail,
                error_code="approve_blocked",
                result=result_val,
                message=(
                    "Account blocked by ChatGPT (result=blocked) — "
                    "server-side anti-fraud, NOT a tool bug. Try a "
                    "different proxy or account, or wait 30-60 minutes "
                    "for cooldown."
                ),
            )
        if result_val in ("needs_review", "pending_review", "review_required"):
            return cls(
                http_status=http_status,
                detail=detail,
                error_code="approve_needs_review",
                result=result_val,
                message=(
                    f"Account needs manual review from ChatGPT (result={result_val}) — "
                    "KYC/risk check, wait for ChatGPT to process then retry."
                ),
            )
        return cls(
            http_status=http_status,
            detail=detail,
            error_code="approve_failed",
            result=result_val,
        )


class RefreshPollExhaustedError(IdealFlowError):
    """Requirement 4.6: đã thực hiện đủ `ideal.refresh_poll_max_attempts`
    lượt refresh mà response vẫn không chứa
    `setup_intent.next_action.redirect_to_url.url`.

    Attributes:
        attempts: Số lượt thử refresh đã thực hiện.
    """

    def __init__(self, attempts: int, message: str | None = None) -> None:
        self.attempts: int = attempts
        super().__init__(
            error_code="refresh_poll_exhausted",
            step="refresh_poll",
            message=message or f"Refresh poll exhausted its limit after {attempts} attempt(s)",
        )


class RedirectValidationError(IdealFlowError):
    """Requirement 4.8: response redirect KHÔNG phải HTTP 302 hoặc thiếu
    header `Location`.

    Attributes:
        http_status: Mã HTTP status nhận được, nếu có.
        has_location: Response có chứa header `Location` hay không.
    """

    def __init__(
        self,
        http_status: int | None = None,
        has_location: bool = False,
        message: str | None = None,
    ) -> None:
        self.http_status: int | None = http_status
        self.has_location: bool = has_location
        super().__init__(
            error_code="redirect_validation_failed",
            step="follow_redirect",
            message=message
            or f"Invalid redirect (http_status={http_status}, has_location={has_location})",
        )


class InvalidTransactionViewError(IdealFlowError):
    """Requirement 5.5: `view != "INITIAL_VIEW"` — giao dịch đang ở trạng
    thái khác (đã hết hạn, đã xử lý...), tiếp tục lấy `supportedIssuers[]`
    là không an toàn.

    Attributes:
        actual_view: Giá trị `view` thực tế nhận được.
    """

    def __init__(self, actual_view: str, message: str | None = None) -> None:
        self.actual_view: str = actual_view
        super().__init__(
            error_code="invalid_transaction_view",
            step="transaction_initiate",
            message=message or f"Transaction view is not 'INITIAL_VIEW': got '{actual_view}'",
        )


class TransactionHttpError(IdealFlowError):
    """Requirement 5.8: request `POST pay.ideal.nl/api/v1/transactions/
    {encoded_tx_url}/initiate` trả về HTTP lỗi (không phải 2xx), hoặc gặp
    lỗi kết nối/timeout ở tầng transport, hoặc payload không parse được
    JSON — Fail_Fast_Policy tại boundary transaction initiate.

    KHÔNG dùng để bọc lỗi thiếu field trong body (đã có
    `RequiredFieldMissingError` từ `parse_ideal_transaction_initiate`) và
    KHÔNG dùng để bọc lỗi giá trị `view` sai (đã có
    `InvalidTransactionViewError`) — chỉ dành cho lỗi ở tầng HTTP/transport/
    JSON-decode xảy ra TRƯỚC khi parse thành `IdealTransactionInitiate`.

    Attributes:
        http_status: Mã HTTP status nhận được, `None` nếu là lỗi transport
            (timeout, connection refused, DNS...) chưa nhận response.
        detail: Mô tả nguyên nhân chi tiết (đã trim/redact), nếu có.
    """

    def __init__(
        self,
        http_status: int | None = None,
        detail: str | None = None,
        message: str | None = None,
    ) -> None:
        self.http_status: int | None = http_status
        self.detail: str | None = detail
        super().__init__(
            error_code="transaction_http_error",
            step="initiate",
            message=message
            or f"Transaction initiate HTTP error (http_status={http_status}, detail={detail})",
        )


class NoIssuerAvailableError(IdealFlowError):
    """Requirement 5.6, 6.3: không có IssuerBank khả dụng khớp cấu hình
    (`ideal.default_issuer`), hoặc `supportedIssuers[]` rỗng.

    Attributes:
        available_issuer_ids: Danh sách `id` của các issuer khả dụng —
            rỗng ở R5.6, non-empty liệt kê ở R6.3.
    """

    def __init__(self, available_issuer_ids: list[str], message: str | None = None) -> None:
        self.available_issuer_ids: list[str] = list(available_issuer_ids)
        super().__init__(
            error_code="no_issuer_available",
            step="issuer_select",
            message=message
            or f"No issuer available matching configuration (available_issuer_ids={self.available_issuer_ids})",
        )


class QrRenderError(IdealFlowError):
    """Requirement 7.6: thư viện QR raise lỗi tường minh (encode dữ liệu
    quá dài, lỗi I/O ghi file...) trong quá trình vẽ ảnh QR PNG từ deeplink.

    Attributes:
        reason: Mô tả nguyên nhân lỗi cụ thể, nếu có.
    """

    def __init__(self, reason: str | None = None, message: str | None = None) -> None:
        self.reason: str | None = reason
        super().__init__(
            error_code="qr_render_failed",
            step="qr_render",
            message=message or f"Error rendering QR PNG (reason={reason})",
        )


__all__ = [
    "IdealFlowError",
    "ChatgptLoginError",
    "LoginError",
    "RequiredFieldMissingError",
    "StripeRetryExhaustedError",
    "StripeHttpClientError",
    "AddressValidationRejectedError",
    "ConfirmInvalidResponseError",
    "ApproveFailedError",
    "RefreshPollExhaustedError",
    "RedirectValidationError",
    "InvalidTransactionViewError",
    "TransactionHttpError",
    "NoIssuerAvailableError",
    "QrRenderError",
]
