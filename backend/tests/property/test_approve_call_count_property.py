"""Property test cho `ChatgptClient.approve` — **Property 11: Approve được gọi
tối đa 1 lần cho mỗi lượt thử**.

Kịch bản: sinh 5 nhóm response `approve` giả lập bằng `hypothesis` —

    - `success`             : HTTP 200, body `{"result": "approved"}`.
    - `timeout`             : `httpx.TimeoutException` (transport-level).
    - `http_400`            : HTTP 400, body error.
    - `http_500`            : HTTP 500, body error.
    - `200_missing_result`  : HTTP 200, body `{"result": "not_approved"}`.

Handler `MockTransport` đếm số lần được gọi qua closure `call_count = [0]`.
Sau khi gọi `ChatgptClient.approve(...)`:

    - Nhóm `success` → KHÔNG raise.
    - 4 nhóm còn lại → phải raise `ApproveFailedError` (R4.2 —
      `IdealFlowHandler` KHÔNG BAO GIỜ tự retry, mọi lỗi bọc vào đúng
      exception này).

Assertion cốt lõi: `call_count[0] == 1` LUÔN — bất kể scenario. Đây là bằng
chứng khép kín cho Property 11: 1 lượt thử approve gửi ĐÚNG 1 request HTTP,
không có nhánh nào (thành công / lỗi kết nối / HTTP 4xx-5xx / 200 thiếu
`result=approved`) khiến Backend_Service tự động gọi lại `approve`.

**Validates: Requirements 4.1, 4.2**
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core import http_client as http
from app.payments.ideal.chatgpt_client import ChatgptClient
from app.payments.ideal.errors import ApproveFailedError
from app.payments.ideal.models import SessionBundle
from tests.support.fake_http import FakeAsyncSession, FakeResponse

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

#: Fake session — 3 field bắt buộc của `SessionBundle`. Giá trị chỉ cần
#: non-empty để `approve()` restore cookies + build header Authorization;
#: chi tiết giá trị KHÔNG phải subject của property này (đã được cover ở
#: Property 10 / các test khác).
_FAKE_SESSION = SessionBundle(
    email="a@b.c",
    access_token="tok",
    cookies={},
)

#: 5 nhóm scenario response — đại diện đủ 4 nhánh lỗi + 1 nhánh success theo
#: docstring `ChatgptClient.approve` (R4.1, R4.2). Union này chính là tập
#: quantifier "với mọi response" của Property 11.
_APPROVE_SCENARIOS = (
    "success",
    "timeout",
    "http_400",
    "http_500",
    "200_missing_result",
)


def _make_handler(scenario: str, call_count: list[int]):
    """Tạo handler cho `httpx.MockTransport` theo `scenario`.

    Handler tăng `call_count[0]` mỗi lần được invoke — đây là counter đóng
    vai trò "wire-level" tracking, không có mock nào chèn vào giữa. Vì
    `httpx.MockTransport` chỉ gọi handler khi client thực sự issue 1
    request qua transport → `call_count[0]` phản ánh số request thực tế
    gửi đi.

    Với scenario `timeout`, handler raise `httpx.TimeoutException` — client
    sẽ nhận exception này bên trong `_client.post(...)`, `approve()` map
    thành `ApproveFailedError(http_status=None, ...)`. Số lần call vẫn
    được đếm 1 vì handler đã chạy tới điểm raise.
    """

    def _handler(call):  # noqa: ANN001
        call_count[0] += 1
        if scenario == "success":
            return FakeResponse(200, json_body={"result": "approved"})
        if scenario == "timeout":
            return http.TimeoutException("simulated timeout")
        if scenario == "http_400":
            return FakeResponse(400, json_body={"error": "bad"})
        if scenario == "http_500":
            return FakeResponse(500, json_body={"error": "server"})
        if scenario == "200_missing_result":
            return FakeResponse(200, json_body={"result": "not_approved"})
        # Fail-fast: scenario mới thêm vào `_APPROVE_SCENARIOS` mà quên map
        # ở đây → không được nuốt lỗi.
        raise AssertionError(f"scenario không được hỗ trợ: {scenario!r}")

    return _handler


# ---------------------------------------------------------------------------
# Property 11 — 1 test bao trọn 5 nhánh scenario
# ---------------------------------------------------------------------------


@given(scenario=st.sampled_from(_APPROVE_SCENARIOS))
@settings(max_examples=50, deadline=None)
def test_approve_called_exactly_once_regardless_of_response(scenario: str) -> None:
    """*Với mọi* response `approve` giả lập (thành công / lỗi kết nối /
    HTTP 4xx-5xx / 200 thiếu `result=approved`), số lần request `approve`
    thực tế được gửi trong 1 lượt thử luôn bằng đúng 1.

    KHÔNG có nhánh nào của `ChatgptClient.approve` (thành công hoặc lỗi)
    khiến Backend_Service tự động gọi lại `approve` cho cùng
    `checkout_session_id` — R4.2 cấm retry để tránh approve trùng lặp giao
    dịch.

    **Validates: Requirements 4.1, 4.2**
    """
    # Reset counter mỗi example — closure `call_count` là mutable state duy
    # nhất giữa các example, phải reset để tránh cộng dồn giữa các lần
    # hypothesis re-run.
    call_count: list[int] = [0]
    handler = _make_handler(scenario, call_count)

    async def _scenario_runner() -> None:
        fake_session = FakeAsyncSession(default_handler=handler)
        client = ChatgptClient(
            http_client=fake_session,
            # `approve()` KHÔNG chạm session_cache — inject MagicMock
            # đủ để thoả annotation của constructor mà không cần dựng
            # `AccountSessionCache` thật (đòi hỏi `SettingsRepository`
            # + filesystem cache_dir).
            session_cache=MagicMock(),
            logger=logging.getLogger("test.chatgpt_client.approve_count"),
        )
        await client.approve(
            checkout_session_id="cs_abc",
            processor_entity="openai_ie",
            session=_FAKE_SESSION,
        )

    if scenario == "success":
        # Success path — không được raise.
        asyncio.run(_scenario_runner())
    else:
        # 4 nhóm lỗi — phải raise `ApproveFailedError` (R4.2 bọc tất cả
        # nhóm lỗi vào cùng 1 exception type để caller không lẫn với
        # LoginError / IdealFlowError khác).
        try:
            asyncio.run(_scenario_runner())
        except ApproveFailedError:
            pass
        else:
            pytest.fail(
                f"scenario={scenario!r} phải raise ApproveFailedError "
                "(R4.2 — mọi nhóm lỗi approve đều fail-fast, không nuốt lỗi)"
            )

    # Assertion cốt lõi — bằng chứng khép kín cho Property 11. `call_count`
    # đo số lần handler `MockTransport` được invoke = số request HTTP thực
    # tế client gửi đi trong 1 lượt gọi `approve()`.
    assert call_count[0] == 1, (
        f"scenario={scenario!r}: `approve()` phải gọi endpoint đúng 1 lần "
        f"trong mỗi lượt thử (R4.2 — không retry), nhưng đã ghi nhận "
        f"call_count={call_count[0]}"
    )


if __name__ == "__main__":  # pragma: no cover
    # Cho phép chạy file trực tiếp (`python3 test/...py`) để smoke test
    # nhanh cục bộ — production dùng `pytest`.
    pytest.main([__file__, "-v"])
