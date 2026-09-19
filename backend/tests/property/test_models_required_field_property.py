"""Property test cho `payments/ideal/models.py` — parser response bắt buộc
thiếu field → Fail_Fast_Policy (Property 3).

**Property 3: Response bắt buộc thiếu field → Fail_Fast (tham số hoá theo
model)**

Tham số hoá theo mỗi model (`CheckoutSession`, `StripePaymentPageInit`,
`StripeElementsSession`, `IdealTransactionInitiate`): sinh dict payload
đầy đủ field bắt buộc, xoá ngẫu nhiên đúng 1 field bắt buộc → assert
parser tương ứng raise `RequiredFieldMissingError` với `field` và `model`
KHỚP CHÍNH XÁC field vừa xoá và tên model đang parse.

**Validates: Requirements 1.7, 2.2, 2.4, 5.6**

Fail_Fast_Policy (Glossary): thiếu field bắt buộc → dừng bước đó ngay,
KHÔNG suy diễn giá trị mặc định, KHÔNG catch-all silent. Property này
verify:
    1. Parser thực sự raise (không im lặng nuốt lỗi).
    2. Exception mang thông tin ĐỦ để log realtime định danh CHÍNH XÁC
       field bị thiếu (`exc.field`) và model đang parse (`exc.model`) —
       yêu cầu log rõ nguyên nhân của Fail_Fast_Policy.

Tổ chức file: 4 test function riêng biệt cho 4 model, mỗi test dùng
`@given(missing_field=st.sampled_from(<required_fields_of_that_model>))`
— tham số hoá SÁT với danh sách field bắt buộc của TỪNG model (thay vì
1 test lồng dùng chung `MODEL_CASES`), để hypothesis shrink counter-example
gọn hơn và trace test failure định danh trực tiếp model bị vi phạm.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.payments.ideal.errors import RequiredFieldMissingError
from app.payments.ideal.models import (
    parse_checkout_session,
    parse_ideal_transaction_initiate,
    parse_stripe_elements_session,
    parse_stripe_payment_page_init,
)

# ---------------------------------------------------------------------------
# Sample valid payload — đầy đủ field bắt buộc, string non-empty (R1.7 dùng
# `payload.get(field)` truthiness để check thiếu, nên "" cũng bị coi là
# thiếu — sample phải dùng string non-empty để đảm bảo baseline hợp lệ).
# ---------------------------------------------------------------------------

_CHECKOUT_SESSION_MODEL_NAME = "CheckoutSession"
_CHECKOUT_SESSION_REQUIRED_FIELDS = (
    "checkout_session_id",
    "publishable_key",
    "processor_entity",
)
_CHECKOUT_SESSION_VALID_PAYLOAD: dict[str, Any] = {
    "checkout_session_id": "cs_test_abc123",
    "publishable_key": "pk_live_test",
    "processor_entity": "openai_ie",
    # Field không bắt buộc — có default an toàn ở parser, thiếu KHÔNG chặn.
    "client_secret": "cs_secret_xyz",
    "status": "open",
    "payment_status": "unpaid",
    "requires_manual_approval": False,
}

_STRIPE_PAYMENT_PAGE_INIT_MODEL_NAME = "StripePaymentPageInit"
_STRIPE_PAYMENT_PAGE_INIT_REQUIRED_FIELDS = ("init_checksum", "config_id")
_STRIPE_PAYMENT_PAGE_INIT_VALID_PAYLOAD: dict[str, Any] = {
    "init_checksum": "checksum_abc123",
    "config_id": "cfg_test_xyz",
}

_STRIPE_ELEMENTS_SESSION_MODEL_NAME = "StripeElementsSession"
_STRIPE_ELEMENTS_SESSION_REQUIRED_FIELDS = ("session_id",)
_STRIPE_ELEMENTS_SESSION_VALID_PAYLOAD: dict[str, Any] = {
    "session_id": "elements_sess_abc123",
}

_IDEAL_TRANSACTION_INITIATE_MODEL_NAME = "IdealTransactionInitiate"
_IDEAL_TRANSACTION_INITIATE_REQUIRED_FIELDS = ("view", "supportedIssuers")
_IDEAL_TRANSACTION_INITIATE_VALID_PAYLOAD: dict[str, Any] = {
    "view": "INITIAL_VIEW",
    "amount": "10.99",
    "creditorName": "Test Merchant",
    # `supportedIssuers` non-empty, mỗi item đầy đủ 4 field bắt buộc của
    # IssuerBank (R5.7) — đảm bảo baseline payload không vô tình vi phạm
    # bất kỳ Fail_Fast_Policy nào KHÁC field đang bị xoá ra.
    "supportedIssuers": [
        {
            "id": "INGBNL2A",
            "deeplinkType": "URL",
            "deeplink": "https://ideal.example/pay?tx=abc",
            "availabilityStatus": "AVAILABLE",
        }
    ],
}


def _delete_field(payload: dict[str, Any], field_name: str) -> dict[str, Any]:
    """Deep copy payload rồi xoá đúng 1 field bắt buộc — đảm bảo mỗi
    hypothesis example thao tác trên bản sao độc lập, không side-effect
    lên sample gốc cấp module."""
    mutated = deepcopy(payload)
    del mutated[field_name]
    return mutated


# ---------------------------------------------------------------------------
# CheckoutSession — 3 field bắt buộc (R1.7).
# ---------------------------------------------------------------------------


@given(missing_field=st.sampled_from(_CHECKOUT_SESSION_REQUIRED_FIELDS))
@settings(max_examples=20)
def test_parse_checkout_session_raises_when_required_field_missing(
    missing_field: str,
) -> None:
    """R1.7: response `checkout` thiếu 1 trong 3 field bắt buộc
    (`checkout_session_id`, `publishable_key`, `processor_entity`) → parser
    raise `RequiredFieldMissingError` với `field`/`model` khớp chính xác."""
    payload = _delete_field(_CHECKOUT_SESSION_VALID_PAYLOAD, missing_field)

    with pytest.raises(RequiredFieldMissingError) as exc_info:
        parse_checkout_session(payload)

    exc = exc_info.value
    assert exc.field == missing_field
    assert exc.model == _CHECKOUT_SESSION_MODEL_NAME


# ---------------------------------------------------------------------------
# StripePaymentPageInit — 2 field bắt buộc (R2.2).
# ---------------------------------------------------------------------------


@given(missing_field=st.sampled_from(_STRIPE_PAYMENT_PAGE_INIT_REQUIRED_FIELDS))
@settings(max_examples=20)
def test_parse_stripe_payment_page_init_raises_when_required_field_missing(
    missing_field: str,
) -> None:
    """R2.2: response `payment_pages/{id}/init` thiếu `init_checksum` hoặc
    `config_id` → parser raise `RequiredFieldMissingError` với `field`/
    `model` khớp chính xác."""
    payload = _delete_field(
        _STRIPE_PAYMENT_PAGE_INIT_VALID_PAYLOAD, missing_field
    )

    with pytest.raises(RequiredFieldMissingError) as exc_info:
        parse_stripe_payment_page_init(payload)

    exc = exc_info.value
    assert exc.field == missing_field
    assert exc.model == _STRIPE_PAYMENT_PAGE_INIT_MODEL_NAME


# ---------------------------------------------------------------------------
# StripeElementsSession — 1 field bắt buộc (R2.4).
# ---------------------------------------------------------------------------


@given(missing_field=st.sampled_from(_STRIPE_ELEMENTS_SESSION_REQUIRED_FIELDS))
@settings(max_examples=20)
def test_parse_stripe_elements_session_raises_when_required_field_missing(
    missing_field: str,
) -> None:
    """R2.4: response `elements/sessions` thiếu `session_id` → parser raise
    `RequiredFieldMissingError` với `field`/`model` khớp chính xác."""
    payload = _delete_field(
        _STRIPE_ELEMENTS_SESSION_VALID_PAYLOAD, missing_field
    )

    with pytest.raises(RequiredFieldMissingError) as exc_info:
        parse_stripe_elements_session(payload)

    exc = exc_info.value
    assert exc.field == missing_field
    assert exc.model == _STRIPE_ELEMENTS_SESSION_MODEL_NAME


# ---------------------------------------------------------------------------
# IdealTransactionInitiate — 2 field bắt buộc (R5.6: `view`, `supportedIssuers`).
# ---------------------------------------------------------------------------


@given(missing_field=st.sampled_from(_IDEAL_TRANSACTION_INITIATE_REQUIRED_FIELDS))
@settings(max_examples=20)
def test_parse_ideal_transaction_initiate_raises_when_required_field_missing(
    missing_field: str,
) -> None:
    """R5.6: response `initiate` thiếu `view` HOẶC `supportedIssuers` →
    parser raise `RequiredFieldMissingError` với `field`/`model` khớp chính
    xác. Field không bắt buộc (`amount`, `creditorName`) không nằm trong
    tham số hoá — hypothesis chỉ sinh giá trị thuộc `REQUIRED_FIELDS`."""
    payload = _delete_field(
        _IDEAL_TRANSACTION_INITIATE_VALID_PAYLOAD, missing_field
    )

    with pytest.raises(RequiredFieldMissingError) as exc_info:
        parse_ideal_transaction_initiate(payload)

    exc = exc_info.value
    assert exc.field == missing_field
    assert exc.model == _IDEAL_TRANSACTION_INITIATE_MODEL_NAME
