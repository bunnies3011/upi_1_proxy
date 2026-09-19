"""Property test cho `StripeClient.confirm` — **Property 9**.

**Property 9: Lỗi validate địa chỉ từ Stripe luôn dẫn tới Fail_Fast kèm
đúng field bị từ chối**

*Với mọi* `rejected_field` ∈ {`address_line1`, `address_line2`,
`address_city`, `address_postal_code`, `address_state`, `name`, `email`},
khi Stripe trả HTTP 400 với payload:

```
{"error": {"type": "invalid_request_error",
           "param": <rejected_field>,
           "message": "<rejected_field> is invalid"}}
```

thì `StripeClient.confirm(...)` phải:

- Fail_Fast_Policy: raise `AddressValidationRejectedError` NGAY LẬP TỨC
  (không retry — R3.4/R3.5).
- Attach `rejected_field` **chính xác** = field mà Stripe từ chối (tức là
  `error.param` được ánh xạ nguyên vẹn sang `exc.rejected_field`).

Sử dụng `httpx.MockTransport` để bắt request `confirm` tại wire-level và
trả cứng response 400. Fake `SettingsRepository` async `get()` trả `None`
cho mọi key → `_read_*` fallback về default nội bộ của `StripeClient`
(giá trị cụ thể không ảnh hưởng test vì `confirm` **không retry**).

**Validates: Requirements 3.4**
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.payments.ideal.errors import AddressValidationRejectedError
from app.payments.ideal.models import BillingAddress
from app.payments.ideal.stripe_client import StripeClient
from tests.support.fake_http import FakeAsyncSession, FakeResponse

# ---------------------------------------------------------------------------
# Fake Settings — duck-typed thay `SettingsRepository`.
#
# `StripeClient._read_request_timeout_seconds` là setting duy nhất được đọc
# ở `confirm()` (R3.5: KHÔNG retry, nên `_read_max_retry_attempts` /
# `_read_retry_backoff_seconds` không được gọi). Trả `None` để fallback về
# default nội bộ — giá trị cụ thể không ảnh hưởng property, vì response
# 400 được emit bởi MockTransport trước khi timeout có thể chạm.
# ---------------------------------------------------------------------------


class _FakeSettings:
    """Trả `None` cho mọi key — buộc `_read_*` dùng default nội bộ."""

    async def get(self, key: str) -> Any | None:  # noqa: ARG002 — key unused
        return None


# ---------------------------------------------------------------------------
# Danh sách field Stripe có thể reject ở request `confirm` (R3.4).
# Trùng khớp `_ADDRESS_ERROR_KEYWORDS` trong `stripe_client.py`.
# ---------------------------------------------------------------------------

_REJECTED_FIELDS: tuple[str, ...] = (
    "address_line1",
    "address_line2",
    "address_city",
    "address_postal_code",
    "address_state",
    "name",
    "email",
)


# ---------------------------------------------------------------------------
# Fake BillingAddress builder — dữ liệu tối thiểu hợp lệ về mặt shape để
# `_build_confirm_request` không raise `ValueError` ở khối validate input.
# Giá trị cụ thể không được Stripe assert (server đã bị mock trả 400).
# ---------------------------------------------------------------------------


def _make_fake_billing() -> BillingAddress:
    """Tạo `BillingAddress` fake NL đủ field, không rỗng ở mức tối thiểu."""
    return BillingAddress(
        name="Jan",
        email="a@b.c",
        address={
            "country": "NL",
            "line1": "Kerkstraat 1",
            "line2": "",
            "city": "Amsterdam",
            "postal_code": "1012 AB",
            "state": "",
        },
    )


# ---------------------------------------------------------------------------
# Property test — 1 test duy nhất, hypothesis pick `rejected_field`.
# ---------------------------------------------------------------------------


@settings(max_examples=50, deadline=None)
@given(rejected_field=st.sampled_from(_REJECTED_FIELDS))
def test_address_validation_rejected_extracts_correct_field(
    rejected_field: str,
) -> None:
    """*Với mọi* `rejected_field` ∈ 7 field địa chỉ/thông tin cá nhân,
    Stripe trả HTTP 400 với `error.param=<rejected_field>` phải khiến
    `confirm()` raise `AddressValidationRejectedError` với
    `exc.rejected_field == rejected_field`.

    Fail_Fast_Policy (R3.4): `_extract_address_rejected_field` ánh xạ
    `error.param` nguyên vẹn sang `rejected_field` của exception — property
    này chốt hợp đồng đó ĐÚNG với TOÀN BỘ 7 field.

    **Validates: Requirements 3.4**
    """

    def _handler(call):  # noqa: ANN001
        # R3.4 — shape response Stripe trả khi validate địa chỉ fail:
        # `error.type=invalid_request_error`, `error.param=<field>`,
        # `error.message="<field> is invalid"`. HTTP 400.
        return FakeResponse(
            400,
            json_body={
                "error": {
                    "type": "invalid_request_error",
                    "param": rejected_field,
                    "message": f"{rejected_field} is invalid",
                }
            },
        )

    async def _run() -> None:
        fake_session = FakeAsyncSession(default_handler=_handler)
        client = StripeClient(
            http_client=fake_session,
            settings=_FakeSettings(),
            logger=logging.getLogger(
                "test.stripe_client.confirm_address_reject"
            ),
        )
        await client.confirm(
            checkout_session_id="cs_abc",
            publishable_key="pk_test",
            elements_session_id="es_test",
            billing=_make_fake_billing(),
        )

    with pytest.raises(AddressValidationRejectedError) as exc_info:
        asyncio.run(_run())

    # Assert chính xác `rejected_field` được ánh xạ nguyên vẹn từ
    # `error.param` sang `exc.rejected_field` (R3.4).
    assert exc_info.value.rejected_field == rejected_field, (
        f"AddressValidationRejectedError.rejected_field kỳ vọng "
        f"= {rejected_field!r} nhưng nhận {exc_info.value.rejected_field!r} "
        "— mapping `error.param` → `rejected_field` không đúng (R3.4)."
    )


if __name__ == "__main__":  # pragma: no cover
    # Cho phép chạy file trực tiếp để smoke test cục bộ — production dùng
    # `pytest` từ backend/.
    pytest.main([__file__, "-v"])
