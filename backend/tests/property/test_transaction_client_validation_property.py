"""Property test cho `payments/ideal/transaction_client.py` và
`payments/ideal/models.py::parse_ideal_transaction_initiate` — **Property 17:
Transaction initiate response được validate view và issuer đầy đủ**.

4 kịch bản độc lập theo Fail_Fast_Policy (Requirement 5.4, 5.5, 5.6, 5.7):

    1. `view != "INITIAL_VIEW"` → `TransactionClient.initiate()` raise
       `InvalidTransactionViewError` (KHÔNG phải từ `parse_*` — parse chỉ
       validate "có/không có" field, việc so sánh giá trị `view` xảy ra
       trong `TransactionClient.initiate()` SAU khi parse để giữ được
       `actual_view` trong exception).
    2. `supportedIssuers` thiếu hoặc rỗng → `parse_ideal_transaction_initiate`
       raise `RequiredFieldMissingError` với `field="supportedIssuers"`.
    3. IssuerBank thiếu 1 trong 4 field bắt buộc (`id`, `deeplinkType`,
       `deeplink`, `availabilityStatus`) → propagate `RequiredFieldMissingError`
       với `model="IssuerBank"` và `field == <field bị thiếu>`.
    4. Payload hợp lệ đầy đủ → parse thành công thành
       `IdealTransactionInitiate` với `view == "INITIAL_VIEW"` và
       `supportedIssuers` non-empty.

**Validates: Requirements 5.4, 5.5, 5.6, 5.7**
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from hypothesis import given, settings, strategies as st

from app.payments.ideal.errors import (
    InvalidTransactionViewError,
    RequiredFieldMissingError,
)
from app.payments.ideal.models import (
    DeviceProfile,
    IdealTransactionInitiate,
    parse_ideal_transaction_initiate,
)
from app.payments.ideal.transaction_client import TransactionClient
from tests.support.fake_http import FakeAsyncSession, FakeResponse

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

_VALID_ISSUER: dict[str, Any] = {
    "id": "INGBNL2A",
    "deeplinkType": "URL",
    "deeplink": "https://x/y",
    "availabilityStatus": "AVAILABLE",
}

_FAKE_DEVICE_PROFILE = DeviceProfile(
    language="nl-NL",
    timeZone="Europe/Amsterdam",
    screenWidth=1920,
    screenHeight=1080,
    screenAvailableWidth=1920,
    screenAvailableHeight=1040,
    colorDepth=24,
)

_ENCODED_TX_URL = "abc"
_FAKE_SIG = "sig-value-xyz"

_ISSUER_REQUIRED_FIELDS: tuple[str, ...] = (
    "id",
    "deeplinkType",
    "deeplink",
    "availabilityStatus",
)


# ---------------------------------------------------------------------------
# Test 1 — view != INITIAL_VIEW → raise từ TransactionClient.initiate()
# ---------------------------------------------------------------------------


@given(
    view=st.text(
        min_size=1,
        max_size=30,
        alphabet=st.characters(blacklist_categories=("Cc", "Cs")),
    ).filter(lambda v: v != "INITIAL_VIEW"),
)
@settings(max_examples=50, deadline=None)
def test_invalid_view_raises_from_transaction_client(view: str) -> None:
    """Property 17 — nhánh Requirement 5.5.

    Response HTTP 2xx với body hợp lệ (đầy đủ `supportedIssuers` + 4 field
    IssuerBank) NHƯNG `view != "INITIAL_VIEW"` → `TransactionClient.initiate()`
    raise `InvalidTransactionViewError(actual_view=<view>)`.

    Xác nhận exception phát sinh Ở `TransactionClient.initiate()` chứ KHÔNG
    phải từ `parse_ideal_transaction_initiate` — parse chỉ Fail_Fast khi
    THIẾU field `view` (falsy), còn validate GIÁ TRỊ `view` là trách nhiệm
    của client (để mang được `actual_view` cụ thể trong exception).
    """
    response_body: dict[str, Any] = {
        "view": view,
        "amount": "10.99",
        "creditorName": "Test",
        "supportedIssuers": [dict(_VALID_ISSUER)],
    }

    def _handler(call):  # noqa: ANN001
        return FakeResponse(200, json_body=response_body)

    fake_session = FakeAsyncSession(default_handler=_handler)

    async def _scenario() -> InvalidTransactionViewError:
        client = TransactionClient(
            http_client=fake_session,
            logger=logging.getLogger("test.transaction_client.validation.view"),
        )
        with pytest.raises(InvalidTransactionViewError) as exc_info:
            await client.initiate(
                encoded_tx_url=_ENCODED_TX_URL,
                sig=_FAKE_SIG,
                device_profile=_FAKE_DEVICE_PROFILE,
            )
        return exc_info.value

    exc = asyncio.run(_scenario())
    assert exc.actual_view == view, (
        "InvalidTransactionViewError phải mang actual_view = giá trị `view` "
        f"thực tế nhận được — expected {view!r}, got {exc.actual_view!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — supportedIssuers rỗng → RequiredFieldMissingError(field="supportedIssuers")
# ---------------------------------------------------------------------------


@given(view=st.just("INITIAL_VIEW"))
@settings(max_examples=5, deadline=None)
def test_empty_supported_issuers_raises(view: str) -> None:
    """Property 17 — nhánh Requirement 5.6.

    `view == "INITIAL_VIEW"` nhưng `supportedIssuers = []` (list rỗng) →
    `parse_ideal_transaction_initiate` raise `RequiredFieldMissingError`
    với `field == "supportedIssuers"` và `model == "IdealTransactionInitiate"`.

    Bao trùm cả case field bị thiếu hoàn toàn HOẶC là list rỗng — logic
    `if not raw_issuers` trong parser xử lý cả 2.
    """
    payload: dict[str, Any] = {
        "view": view,
        "amount": "10.99",
        "creditorName": "Test",
        "supportedIssuers": [],
    }

    with pytest.raises(RequiredFieldMissingError) as exc_info:
        parse_ideal_transaction_initiate(payload)

    exc = exc_info.value
    assert exc.field == "supportedIssuers", (
        "RequiredFieldMissingError phải có field='supportedIssuers' — "
        f"got field={exc.field!r}"
    )
    assert exc.model == "IdealTransactionInitiate", (
        "RequiredFieldMissingError cho supportedIssuers phải có "
        f"model='IdealTransactionInitiate' — got model={exc.model!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — IssuerBank thiếu 1 trong 4 field bắt buộc
# ---------------------------------------------------------------------------


@given(missing=st.sampled_from(_ISSUER_REQUIRED_FIELDS))
@settings(max_examples=20, deadline=None)
def test_issuer_missing_field_raises(missing: str) -> None:
    """Property 17 — nhánh Requirement 5.7.

    IssuerBank thiếu 1 trong 4 field bắt buộc (`id`, `deeplinkType`,
    `deeplink`, `availabilityStatus`) → `parse_issuer_bank` (được gọi từ
    `parse_ideal_transaction_initiate`) raise `RequiredFieldMissingError`
    với `model == "IssuerBank"` và `field == <field bị thiếu>`.
    """
    issuer_dict = {k: v for k, v in _VALID_ISSUER.items() if k != missing}
    payload: dict[str, Any] = {
        "view": "INITIAL_VIEW",
        "amount": "10.99",
        "creditorName": "Test",
        "supportedIssuers": [issuer_dict],
    }

    with pytest.raises(RequiredFieldMissingError) as exc_info:
        parse_ideal_transaction_initiate(payload)

    exc = exc_info.value
    assert exc.model == "IssuerBank", (
        "RequiredFieldMissingError cho issuer thiếu field phải có "
        f"model='IssuerBank' — got model={exc.model!r}"
    )
    assert exc.field == missing, (
        "RequiredFieldMissingError phải chỉ đúng field bị thiếu — "
        f"expected field={missing!r}, got field={exc.field!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 — payload hợp lệ → parse thành công (deterministic, không hypothesis)
# ---------------------------------------------------------------------------


def test_valid_payload_parses_successfully() -> None:
    """Property 17 — nhánh Requirement 5.4.

    Payload đầy đủ (`view == "INITIAL_VIEW"`, `supportedIssuers` non-empty
    với đủ 4 field IssuerBank) → parse trả về `IdealTransactionInitiate`
    không exception.
    """
    payload: dict[str, Any] = {
        "view": "INITIAL_VIEW",
        "amount": "10.99",
        "creditorName": "Test",
        "supportedIssuers": [dict(_VALID_ISSUER)],
    }

    result = parse_ideal_transaction_initiate(payload)

    assert isinstance(result, IdealTransactionInitiate), (
        f"parse phải trả IdealTransactionInitiate — got {type(result).__name__}"
    )
    assert result.view == "INITIAL_VIEW", (
        f"result.view phải là 'INITIAL_VIEW' — got {result.view!r}"
    )
    assert len(result.supportedIssuers) >= 1, (
        "result.supportedIssuers phải có tối thiểu 1 phần tử — "
        f"got len={len(result.supportedIssuers)}"
    )


if __name__ == "__main__":  # pragma: no cover
    # Cho phép chạy file trực tiếp (`python3 test/...py`) để smoke test
    # nhanh cục bộ — production dùng `pytest`.
    pytest.main([__file__, "-v"])
