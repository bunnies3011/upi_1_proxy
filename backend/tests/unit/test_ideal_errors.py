"""Unit test cho `payments/ideal/errors.py`.

Verify: mọi subclass đều là `IdealFlowError`, `error_code`/`step` non-empty
đúng theo từng class, constructor raise `ValueError` nếu tạo `IdealFlowError`
trực tiếp với `error_code`/`step` rỗng, và field riêng của từng subclass lưu
đúng.

_Requirements: 1.4, 1.7, 2.2, 2.4, 2.6, 3.4, 3.5, 4.2, 4.6, 4.8, 5.5, 5.6,
6.3, 7.6, 14.1, 14.3, 14.4_
"""

from __future__ import annotations

import pytest

from app.payments.ideal.errors import (
    AddressValidationRejectedError,
    ApproveFailedError,
    ConfirmInvalidResponseError,
    IdealFlowError,
    InvalidTransactionViewError,
    LoginError,
    NoIssuerAvailableError,
    QrRenderError,
    RedirectValidationError,
    RefreshPollExhaustedError,
    RequiredFieldMissingError,
    StripeRetryExhaustedError,
)


def test_base_ideal_flow_error_valid() -> None:
    print("[PASS] TC-01 — IdealFlowError base khởi tạo hợp lệ :: error_code/step lưu đúng")
    err = IdealFlowError(error_code="some_error", step="some_step")
    assert isinstance(err, Exception)
    assert err.error_code == "some_error"
    assert err.step == "some_step"


@pytest.mark.parametrize(
    "error_code,step",
    [("", "step"), ("code", ""), ("", "")],
)
def test_base_rejects_empty_error_code_or_step(error_code: str, step: str) -> None:
    with pytest.raises(ValueError):
        IdealFlowError(error_code=error_code, step=step)
    print(
        f"[PASS] TC-02 — IdealFlowError raise ValueError khi rỗng "
        f":: error_code={error_code!r} step={step!r}"
    )


def test_login_error() -> None:
    err = LoginError(reason="invalid_credential")
    assert isinstance(err, IdealFlowError)
    assert err.error_code == "login_failed"
    assert err.step == "login"
    assert err.reason == "invalid_credential"
    print("[PASS] TC-03 — LoginError :: error_code=login_failed step=login reason lưu đúng")


def test_required_field_missing_error() -> None:
    # `_MODEL_TO_STEP` trong errors.py map "CheckoutSession" → "checkout" để
    # log realtime chỉ đúng bước flow (checkout create) chứ không phải fallback
    # generic "parse_response". Test này verify default step lấy từ map, KHÔNG
    # phải fallback (fallback được test riêng ở model không trong map).
    err = RequiredFieldMissingError(model_name="CheckoutSession", missing_field="checkout_session_id")
    assert isinstance(err, IdealFlowError)
    assert err.error_code == "required_field_missing"
    assert err.step == "checkout"
    assert err.model_name == "CheckoutSession"
    assert err.missing_field == "checkout_session_id"
    print("[PASS] TC-04 — RequiredFieldMissingError :: step map từ _MODEL_TO_STEP, fields lưu đúng")


def test_required_field_missing_error_default_fallback_step() -> None:
    # Model không có trong `_MODEL_TO_STEP` → fallback về "parse_response".
    err = RequiredFieldMissingError(model_name="UnknownModel", missing_field="some_field")
    assert err.step == "parse_response"
    print("[PASS] TC-04b — RequiredFieldMissingError :: fallback step=parse_response cho model lạ")


def test_required_field_missing_error_custom_step() -> None:
    err = RequiredFieldMissingError(
        model_name="IdealTransactionInitiate",
        missing_field="supportedIssuers",
        step="transaction_initiate",
    )
    assert err.step == "transaction_initiate"
    print("[PASS] TC-05 — RequiredFieldMissingError :: step override hoạt động đúng")


def test_stripe_retry_exhausted_error() -> None:
    err = StripeRetryExhaustedError(request_name="init", attempts=3)
    assert isinstance(err, IdealFlowError)
    assert err.error_code == "stripe_retry_exhausted"
    assert err.step == "stripe_request"
    assert err.request_name == "init"
    assert err.attempts == 3
    print("[PASS] TC-06 — StripeRetryExhaustedError :: fields lưu đúng")


def test_address_validation_rejected_error() -> None:
    err = AddressValidationRejectedError(rejected_field="address_postal_code")
    assert isinstance(err, IdealFlowError)
    assert err.error_code == "address_validation_rejected"
    assert err.step == "stripe_confirm"
    assert err.rejected_field == "address_postal_code"
    print("[PASS] TC-07 — AddressValidationRejectedError :: fields lưu đúng")


def test_confirm_invalid_response_error() -> None:
    err = ConfirmInvalidResponseError(http_status=400)
    assert isinstance(err, IdealFlowError)
    assert err.error_code == "confirm_invalid_response"
    assert err.step == "stripe_confirm"
    assert err.http_status == 400

    err_none = ConfirmInvalidResponseError()
    assert err_none.http_status is None
    print("[PASS] TC-08 — ConfirmInvalidResponseError :: http_status optional lưu đúng")


def test_approve_failed_error() -> None:
    err = ApproveFailedError(http_status=502, detail="gateway error")
    assert isinstance(err, IdealFlowError)
    assert err.error_code == "approve_failed"
    assert err.step == "checkout_approve"
    assert err.http_status == 502
    assert err.detail == "gateway error"
    print("[PASS] TC-09 — ApproveFailedError :: http_status/detail lưu đúng")


def test_refresh_poll_exhausted_error() -> None:
    err = RefreshPollExhaustedError(attempts=5)
    assert isinstance(err, IdealFlowError)
    assert err.error_code == "refresh_poll_exhausted"
    assert err.step == "refresh_poll"
    assert err.attempts == 5
    print("[PASS] TC-10 — RefreshPollExhaustedError :: attempts lưu đúng")


def test_redirect_validation_error() -> None:
    err = RedirectValidationError(http_status=200, has_location=False)
    assert isinstance(err, IdealFlowError)
    assert err.error_code == "redirect_validation_failed"
    assert err.step == "follow_redirect"
    assert err.http_status == 200
    assert err.has_location is False
    print("[PASS] TC-11 — RedirectValidationError :: http_status/has_location lưu đúng")


def test_invalid_transaction_view_error() -> None:
    err = InvalidTransactionViewError(actual_view="EXPIRED_VIEW")
    assert isinstance(err, IdealFlowError)
    assert err.error_code == "invalid_transaction_view"
    assert err.step == "transaction_initiate"
    assert err.actual_view == "EXPIRED_VIEW"
    print("[PASS] TC-12 — InvalidTransactionViewError :: actual_view lưu đúng")


def test_no_issuer_available_error() -> None:
    err = NoIssuerAvailableError(available_issuer_ids=["ABNANL2A", "RABONL2U"])
    assert isinstance(err, IdealFlowError)
    assert err.error_code == "no_issuer_available"
    assert err.step == "issuer_select"
    assert err.available_issuer_ids == ["ABNANL2A", "RABONL2U"]

    # Copy list — mutate input sau khi raise KHÔNG ảnh hưởng exception.
    ids = ["X"]
    err2 = NoIssuerAvailableError(available_issuer_ids=ids)
    ids.append("Y")
    assert err2.available_issuer_ids == ["X"]
    print("[PASS] TC-13 — NoIssuerAvailableError :: available_issuer_ids lưu đúng (copy, không alias)")


def test_qr_render_error() -> None:
    err = QrRenderError(reason="encode_too_long")
    assert isinstance(err, IdealFlowError)
    assert err.error_code == "qr_render_failed"
    assert err.step == "qr_render"
    assert err.reason == "encode_too_long"

    err_none = QrRenderError()
    assert err_none.reason is None
    print("[PASS] TC-14 — QrRenderError :: reason optional lưu đúng")


ALL_SUBCLASSES = [
    LoginError(reason="invalid_credential"),
    RequiredFieldMissingError(model_name="M", missing_field="f"),
    StripeRetryExhaustedError(request_name="init", attempts=1),
    AddressValidationRejectedError(rejected_field="f"),
    ConfirmInvalidResponseError(),
    ApproveFailedError(),
    RefreshPollExhaustedError(attempts=1),
    RedirectValidationError(),
    InvalidTransactionViewError(actual_view="X"),
    NoIssuerAvailableError(available_issuer_ids=[]),
    QrRenderError(),
]


@pytest.mark.parametrize("err", ALL_SUBCLASSES, ids=lambda e: type(e).__name__)
def test_every_subclass_is_ideal_flow_error_with_non_empty_code_and_step(err: IdealFlowError) -> None:
    assert isinstance(err, IdealFlowError)
    assert isinstance(err.error_code, str) and err.error_code != ""
    assert isinstance(err.step, str) and err.step != ""
    print(
        f"[PASS] TC-15 — {type(err).__name__} là IdealFlowError với error_code/step non-empty "
        f":: error_code={err.error_code!r} step={err.step!r}"
    )
