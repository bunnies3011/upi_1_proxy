"""Property test cho `StripeClient._build_confirm_request` — **Property 8**.

**Property 8: Request confirm luôn chứa đầy đủ BillingAddress + type=ideal**

*Với mọi* `BillingAddress` hợp lệ (name, email + `address` dict với
`country="NL"` cùng line1/line2/city/postal_code/state là chuỗi non-empty
ngẫu nhiên), `StripeClient._build_confirm_request(...)` luôn:

- Đặt `payment_method_data[type] == "ideal"` (R3.2).
- Chèn ĐẦY ĐỦ 6 field billing details vào form_data với đúng giá trị input:
  - `payment_method_data[billing_details][name]`
  - `payment_method_data[billing_details][email]`
  - `payment_method_data[billing_details][address][country]`
  - `payment_method_data[billing_details][address][line1]`
  - `payment_method_data[billing_details][address][line2]`
  - `payment_method_data[billing_details][address][city]`
  - `payment_method_data[billing_details][address][postal_code]`
  - `payment_method_data[billing_details][address][state]`
- KHÔNG chèn bất kỳ field chọn ngân hàng nào — cụ thể assert
  `payment_method_data[ideal][bank]`, `payment_method_data[ideal][issuer]`,
  `bank`, `issuer` đều KHÔNG có trong form_data (R3.2 — Stripe iDEAL flow
  hiện tại KHÔNG chọn issuer bank ở bước confirm).

Test gọi TRỰC TIẾP `_build_confirm_request` (unpack `url, form_data,
headers`) — KHÔNG cần `httpx.MockTransport` vì method này thuần tuý build
tuple, không phát HTTP request.

**Validates: Requirements 3.2**
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from app.payments.ideal.models import BillingAddress
from app.payments.ideal.stripe_client import StripeClient
from tests.support.fake_http import FakeAsyncSession


# ---------------------------------------------------------------------------
# Fake Settings — duck-typed thay thế `SettingsRepository`.
#
# `_build_confirm_request` KHÔNG đọc Settings (build tuple thuần tuý), nhưng
# `StripeClient.__init__` yêu cầu 1 `settings` object có `.get(key)` async.
# Trả `None` cho mọi key là đủ — không key nào được truy cập trong test này.
# ---------------------------------------------------------------------------


class _FakeSettings:
    """Trả `None` cho mọi key — bám semantics `SettingsRepository.get`.

    `_build_confirm_request` không đụng tới Settings, nên implementation
    trivial như thế này là đủ. Nếu có key nào bị đọc bất ngờ trong tương
    lai, test sẽ fail sớm ở `_read_*` (KeyError/None handling) — không
    che khuyết lỗi.
    """

    async def get(self, key: str) -> Any | None:
        return None


# ---------------------------------------------------------------------------
# Hypothesis strategies
#
# `BillingAddress` sinh từ:
#   - name: chuỗi text non-empty (loại whitespace-only để tránh giá trị
#     "vô hình" khi hiển thị counter-example).
#   - email: local@domain đơn giản, alphanumeric.
#   - address.country: cố định "NL" (Requirement 3.1 — locale Hà Lan).
#   - line1/line2/city/postal_code/state: chuỗi alnum+space non-empty.
#
# checkout_session_id / publishable_key / elements_session_id: chuỗi alnum
# 8-30 ký tự để đại diện shape token Stripe thực tế (không cần prefix
# `cs_`/`pk_` — method không parse prefix).
# ---------------------------------------------------------------------------

_NON_EMPTY_TEXT = st.text(
    alphabet=st.characters(
        min_codepoint=0x21,  # loại whitespace + control chars đầu bảng ASCII
        max_codepoint=0x7E,  # printable ASCII, tránh Unicode gây noise
        blacklist_categories=("Cs",),  # loại surrogate
    ),
    min_size=1,
    max_size=40,
)

_ALNUM_TOKEN = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    min_size=8,
    max_size=30,
)

_EMAIL_LOCAL = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
    min_size=1,
    max_size=20,
)
_EMAIL_DOMAIN = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz",
    min_size=2,
    max_size=15,
)


@st.composite
def _billing_addresses(draw: st.DrawFn) -> BillingAddress:
    """Sinh 1 `BillingAddress` hợp lệ locale NL.

    `country` cố định "NL" (R3.1). Các field còn lại là chuỗi non-empty
    ngẫu nhiên để cover phổ input đủ rộng cho property assertion.
    """
    name = draw(_NON_EMPTY_TEXT)
    email_local = draw(_EMAIL_LOCAL)
    email_domain = draw(_EMAIL_DOMAIN)
    email = f"{email_local}@{email_domain}.nl"
    address = {
        "country": "NL",
        "line1": draw(_NON_EMPTY_TEXT),
        "line2": draw(_NON_EMPTY_TEXT),
        "city": draw(_NON_EMPTY_TEXT),
        "postal_code": draw(_NON_EMPTY_TEXT),
        "state": draw(_NON_EMPTY_TEXT),
    }
    return BillingAddress(name=name, email=email, address=address)


# ---------------------------------------------------------------------------
# Property test — Property 8
# ---------------------------------------------------------------------------


@settings(max_examples=50, deadline=None)
@given(
    billing=_billing_addresses(),
    checkout_session_id=_ALNUM_TOKEN,
    publishable_key=_ALNUM_TOKEN,
    elements_session_id=_ALNUM_TOKEN,
)
def test_build_confirm_request_contains_full_billing_and_type_ideal(
    billing: BillingAddress,
    checkout_session_id: str,
    publishable_key: str,
    elements_session_id: str,
) -> None:
    """*Với mọi* `BillingAddress` hợp lệ + token session/pk/elements ngẫu
    nhiên, `_build_confirm_request(...)` sinh `form_data`:

        - `payment_method_data[type] == "ideal"`.
        - 6 field billing (name/email/address[country|line1|line2|city|
          postal_code|state]) tồn tại và giá trị = giá trị tương ứng
          trong `BillingAddress` input.
        - KHÔNG chèn field chọn ngân hàng nào (`payment_method_data[ideal]
          [bank]`, `payment_method_data[ideal][issuer]`, `bank`, `issuer`).

    Property này là bằng chứng shape wire request confirm ổn định độc lập
    với input billing — Fail_Fast_Policy khoá chặt boundary Stripe ↔ iDEAL
    flow: không có nhánh code nào cho phép chèn issuer/bank vào request
    confirm ở bước này (R3.2).

    **Validates: Requirements 3.2**
    """
    async def _build() -> tuple[str, bytes, dict[str, str]]:
        # `_build_confirm_request` KHÔNG phát request thực; FakeAsyncSession
        # chỉ phục vụ contract `StripeClient.__init__`.
        async with FakeAsyncSession() as fake_session:
            client = StripeClient(
                http_client=fake_session,
                settings=_FakeSettings(),  # duck-typed thay SettingsRepository
                logger=logging.getLogger("test.stripe_client.confirm_body"),
            )
            return client._build_confirm_request(
                checkout_session_id=checkout_session_id,
                publishable_key=publishable_key,
                elements_session_id=elements_session_id,
                billing=billing,
            )

    url, content_bytes, headers = asyncio.run(_build())
    # `_build_confirm_request` trả `content_bytes` (URL-encoded). Parse
    # lại thành dict cho các assert bên dưới.
    from urllib.parse import parse_qs
    parsed = parse_qs(content_bytes.decode("utf-8"), keep_blank_values=True)
    form_data = {k: v[0] for k, v in parsed.items()}

    # -------- Type = ideal (R3.2) --------
    assert form_data["payment_method_data[type]"] == "ideal", (
        "form_data phải chứa 'payment_method_data[type]' = 'ideal' "
        f"nhưng nhận: {form_data.get('payment_method_data[type]')!r} (R3.2)."
    )

    # -------- 6 field billing_details với đúng giá trị --------
    assert (
        form_data["payment_method_data[billing_details][name]"] == billing.name
    ), (
        "billing_details[name] mismatch: "
        f"form_data={form_data.get('payment_method_data[billing_details][name]')!r}, "
        f"billing.name={billing.name!r} (R3.2)."
    )
    assert (
        form_data["payment_method_data[billing_details][email]"] == billing.email
    ), (
        "billing_details[email] mismatch: "
        f"form_data={form_data.get('payment_method_data[billing_details][email]')!r}, "
        f"billing.email={billing.email!r} (R3.2)."
    )

    address = billing.address
    for field in ("country", "line1", "line2", "city", "postal_code", "state"):
        key = f"payment_method_data[billing_details][address][{field}]"
        assert form_data[key] == address[field], (
            f"billing_details[address][{field}] mismatch: "
            f"form_data={form_data.get(key)!r}, "
            f"billing.address[{field!r}]={address[field]!r} (R3.2)."
        )

    # -------- KHÔNG field chọn ngân hàng nào --------
    for forbidden_key in (
        "payment_method_data[ideal][bank]",
        "payment_method_data[ideal][issuer]",
        "bank",
        "issuer",
    ):
        assert forbidden_key not in form_data, (
            f"form_data KHÔNG được chứa field chọn ngân hàng "
            f"{forbidden_key!r} (R3.2 — Stripe iDEAL confirm không chọn "
            f"issuer ở bước này) nhưng nhận: {form_data.get(forbidden_key)!r}."
        )


if __name__ == "__main__":  # pragma: no cover
    # Cho phép chạy file trực tiếp để smoke test cục bộ — production dùng
    # `pytest` từ backend/.
    import pytest

    pytest.main([__file__, "-v"])
