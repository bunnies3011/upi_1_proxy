"""Property test cho `StripeClient.confirm` — **Property 10**.

**Property 10: Response confirm không hợp lệ luôn dẫn tới Fail_Fast**

*Với mọi* response `confirm` giả lập thuộc 1 trong 4 nhóm sau:

- `http_5xx`               : HTTP 500, body JSON error hợp lệ (dict).
- `http_2xx_missing_state` : HTTP 200, body JSON dict nhưng KHÔNG có
                             `payment_intent`/`setup_intent`/`status`.
- `http_2xx_invalid_json`  : HTTP 200, body raw không parse được JSON
                             (ví dụ trang HTML lỗi bất thường).
- `http_2xx_not_dict`      : HTTP 200, body parse được JSON nhưng là
                             list/scalar chứ không phải object.

`StripeClient.confirm(...)` LUÔN raise `ConfirmInvalidResponseError`
theo Fail_Fast_Policy (R3.5 — KHÔNG retry, chuyển IdealJob sang `error`
ngay lập tức) với `exc.http_status` bằng đúng HTTP status quan sát
được ở response.

Property này CỐ Ý loại trừ nhóm 4xx với error validate địa chỉ
(shape: `error.type=invalid_request_error`, `error.param=<field>`) —
đó là địa hạt của `AddressValidationRejectedError` (R3.4) đã được cover
ở Property 9 (`test_stripe_confirm_address_reject_property.py`, task 16.5).

Sử dụng `httpx.MockTransport` bắt request `confirm` tại wire-level, phát
response cứng theo scenario. Fake `SettingsRepository` async `get()` trả
`None` cho mọi key → `_read_request_timeout_seconds` fallback default nội
bộ (giá trị cụ thể không ảnh hưởng property vì MockTransport phản hồi
ngay, không có delay/timeout thực).

**Validates: Requirements 3.5**
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.payments.ideal.errors import ConfirmInvalidResponseError
from app.payments.ideal.models import BillingAddress
from app.payments.ideal.stripe_client import StripeClient
from tests.support.fake_http import FakeAsyncSession, FakeResponse

# ---------------------------------------------------------------------------
# Fake Settings — duck-typed thay `SettingsRepository`.
#
# `confirm()` chỉ đọc `ideal.stripe_request_timeout_seconds` (KHÔNG retry
# theo R3.5 → 2 setting retry-related không được gọi). Trả `None` cho mọi
# key để `_read_request_timeout_seconds()` fallback về default nội bộ —
# response được emit ngay bởi MockTransport, timeout không bị chạm.
# ---------------------------------------------------------------------------


class _FakeSettings:
    """Trả `None` cho mọi key — buộc `_read_*` dùng default nội bộ."""

    async def get(self, key: str) -> Any | None:  # noqa: ARG002 — key unused
        return None


# ---------------------------------------------------------------------------
# Fake BillingAddress — dữ liệu tối thiểu hợp lệ về mặt shape để
# `_build_confirm_request` không raise `ValueError` ở khối validate input.
# Giá trị cụ thể không ảnh hưởng property (server đã bị mock trả response
# lỗi/thiếu-state theo scenario).
# ---------------------------------------------------------------------------


def _make_fake_billing() -> BillingAddress:
    """Tạo `BillingAddress` fake NL đầy đủ, không rỗng ở mức tối thiểu."""
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
# 4 scenario response — union này chính là tập quantifier "với mọi response
# không hợp lệ" của Property 10. Đại diện đủ 2 trục lỗi theo R3.5:
#
#   Trục 1 — HTTP status lỗi (không thuộc 2xx): `http_5xx`.
#   Trục 2 — HTTP 2xx nhưng payload không hợp lệ: 3 dạng cụ thể
#            (thiếu state / không parse được JSON / JSON không phải dict).
#
# CỐ Ý bỏ nhóm 4xx address-validation vì thuộc `AddressValidationRejectedError`
# (R3.4, task 16.5) — property này giữ boundary phân biệt 2 loại lỗi.
# ---------------------------------------------------------------------------

_INVALID_RESPONSE_SCENARIOS: tuple[str, ...] = (
    "http_5xx",
    "http_2xx_missing_state",
    "http_2xx_invalid_json",
    "http_2xx_not_dict",
)

#: HTTP status kỳ vọng của `exc.http_status` cho mỗi scenario. Mọi scenario
#: `http_2xx_*` phải trả về status 200 nguyên bản. `http_5xx` cố định 500
#: để có sample điểm cụ thể (không parametrize toàn dải 500-599 vì hành vi
#: `confirm()` với mọi status 5xx đều đi cùng nhánh "không phải 2xx" — 500
#: là đại diện đủ).
_EXPECTED_HTTP_STATUS: dict[str, int] = {
    "http_5xx": 500,
    "http_2xx_missing_state": 200,
    "http_2xx_invalid_json": 200,
    "http_2xx_not_dict": 200,
}


def _make_handler(scenario: str):
    """Tạo handler cho `FakeAsyncSession.default_handler` phát response theo `scenario`.

    Contract mỗi call:
        - `http_5xx`: `Response(500, json={"error": "server"})` — dict hợp
          lệ nhưng status không thuộc 2xx → nhánh "R3.5 status khác 2xx"
          trong `confirm()`.
        - `http_2xx_missing_state`: `Response(200, json={"unrelated":
          "field"})` — dict 2xx nhưng KHÔNG có `payment_intent`/
          `setup_intent`/`status` → nhánh "R3.5 payload thiếu confirmation
          state" trong `confirm()`.
        - `http_2xx_invalid_json`: `Response(200, content=b"not json",
          headers={"content-type": "text/html"})` — 2xx body raw không
          parse được JSON → nhánh `response.json()` raise `ValueError`
          → `ConfirmInvalidResponseError` trong `confirm()`.
        - `http_2xx_not_dict`: `Response(200, json=[1, 2, 3])` — 2xx body
          JSON nhưng là list thay vì dict → nhánh `isinstance(payload,
          dict) is False` → `ConfirmInvalidResponseError` trong `confirm()`.

    Handler không stateful (không cần đếm call như test retry) — mỗi
    scenario chỉ có 1 phản hồi vì `confirm()` KHÔNG retry (R3.5).
    """

    def _handler(call):  # noqa: ANN001
        if scenario == "http_5xx":
            return FakeResponse(500, json_body={"error": "server"})
        if scenario == "http_2xx_missing_state":
            return FakeResponse(200, json_body={"unrelated": "field"})
        if scenario == "http_2xx_invalid_json":
            return FakeResponse(
                200,
                content=b"not json",
                headers={"content-type": "text/html"},
            )
        if scenario == "http_2xx_not_dict":
            return FakeResponse(200, json_body=[1, 2, 3])
        raise AssertionError(f"scenario không được hỗ trợ: {scenario!r}")

    return _handler


# ---------------------------------------------------------------------------
# Property 10 — 1 test bao trọn 4 scenario.
# ---------------------------------------------------------------------------


@settings(max_examples=50, deadline=None)
@given(scenario=st.sampled_from(_INVALID_RESPONSE_SCENARIOS))
def test_invalid_confirm_response_raises(scenario: str) -> None:
    """*Với mọi* response `confirm` giả lập thuộc 4 nhóm (`http_5xx` /
    `http_2xx_missing_state` / `http_2xx_invalid_json` / `http_2xx_not_dict`),
    `StripeClient.confirm(...)`:

        - Raise `ConfirmInvalidResponseError` NGAY LẬP TỨC (Fail_Fast_Policy
          R3.5 — KHÔNG retry, chuyển IdealJob sang `error`).
        - Attach `exc.http_status` bằng đúng HTTP status quan sát được
          (500 cho `http_5xx`, 200 cho cả 3 case `http_2xx_*`).

    Property này CỐ Ý bỏ nhóm 4xx address-validation (R3.4 → dùng
    `AddressValidationRejectedError`, đã cover ở Property 9 / task 16.5)
    để giữ boundary phân biệt rõ 2 loại lỗi khác nhau về mặt hợp đồng.

    **Validates: Requirements 3.5**
    """
    handler = _make_handler(scenario)

    async def _run() -> None:
        fake_session = FakeAsyncSession(default_handler=handler)
        client = StripeClient(
            http_client=fake_session,
            settings=_FakeSettings(),  # duck-typed thay SettingsRepository
            logger=logging.getLogger(
                "test.stripe_client.confirm_invalid_response"
            ),
        )
        await client.confirm(
            checkout_session_id="cs_abc",
            publishable_key="pk_test",
            elements_session_id="es_test",
            billing=_make_fake_billing(),
        )

    with pytest.raises(ConfirmInvalidResponseError) as exc_info:
        asyncio.run(_run())

    # Assert `exc.http_status` khớp status quan sát được — bằng chứng
    # `confirm()` đã propagate đúng thông tin HTTP để `IdealFlowHandler`
    # log realtime + map JobResult (R3.5).
    expected_status = _EXPECTED_HTTP_STATUS[scenario]
    assert exc_info.value.http_status == expected_status, (
        f"scenario={scenario!r}: ConfirmInvalidResponseError.http_status "
        f"kỳ vọng = {expected_status} nhưng nhận "
        f"{exc_info.value.http_status!r} — `confirm()` không propagate "
        "đúng HTTP status của response (R3.5)."
    )


if __name__ == "__main__":  # pragma: no cover
    # Cho phép chạy file trực tiếp để smoke test cục bộ — production dùng
    # `pytest` từ backend/.
    pytest.main([__file__, "-v"])
