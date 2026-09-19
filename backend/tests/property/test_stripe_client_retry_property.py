"""Property test cho `StripeClient._bounded_retry` — **Property 6**.

**Property 6: Stripe request retry-with-backoff có giới hạn, không retry
lỗi validate/4xx**

*Với mọi* chuỗi kết quả giả lập (`timeout`/`5xx`/`4xx`/`ok`) cho request
`init` (hoặc `elements/sessions` — cùng cơ chế `_bounded_retry`),
`StripeClient` luôn:

- Retry cho `timeout`/`5xx` tối đa `ideal.stripe_max_retry_attempts` lần
  (bao gồm lần đầu).
- KHÔNG retry `4xx` — raise `StripeHttpClientError` ngay lập tức
  (Fail_Fast_Policy, R2.6).
- KHÔNG retry sau khi gặp `ok` (2xx) — trả response ngay ở attempt gặp OK.
- Nếu chuỗi toàn `timeout`/`5xx` cho tới hết attempts → raise
  `StripeRetryExhaustedError` với số lần thử = `max_attempts`.

Sử dụng `httpx.MockTransport` để bắt request tại wire-level. Closure
`call_count = [0]` đóng vai counter số request thực tế client issue qua
transport — không có mock/spy nào chèn giữa để bóp méo con số này.

**Validates: Requirements 2.6**
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from app.core import http_client as http
from tests.support.fake_http import FakeAsyncSession, FakeResponse
from hypothesis import given, settings
from hypothesis import strategies as st

from app.payments.ideal.errors import (
    StripeHttpClientError,
    StripeRetryExhaustedError,
)
from app.payments.ideal.stripe_client import StripeClient

# ---------------------------------------------------------------------------
# Fake Settings — duck-typed thay thế `SettingsRepository`.
#
# `StripeClient._read_*` chỉ dùng duy nhất `settings.get(key)` (async) để
# đọc 3 key retry-related. Không cần DB engine/whitelist/schema đầy đủ.
# Backoff đặt cực nhỏ (0.001s) để test nhanh; max_attempts=3 khớp default
# của Settings whitelist (R11.7).
# ---------------------------------------------------------------------------

_MAX_RETRY_ATTEMPTS: int = 3
_RETRY_BACKOFF_SECONDS: float = 0.001
_REQUEST_TIMEOUT_SECONDS: float = 1.0

_FAKE_SETTINGS_VALUES: dict[str, Any] = {
    "ideal.stripe_max_retry_attempts": _MAX_RETRY_ATTEMPTS,
    "ideal.stripe_retry_backoff_seconds": _RETRY_BACKOFF_SECONDS,
    "ideal.stripe_request_timeout_seconds": _REQUEST_TIMEOUT_SECONDS,
}


class _FakeSettings:
    """Trả cứng 3 setting liên quan retry; mọi key khác trả `None`.

    Bám semantics thực của `SettingsRepository.get`: key chưa set trả
    `None` để caller (`_read_*`) fallback về default nội bộ — ta không giả
    lập trạng thái đó ở đây vì test chỉ cần 3 key retry.
    """

    async def get(self, key: str) -> Any | None:
        return _FAKE_SETTINGS_VALUES.get(key)


# ---------------------------------------------------------------------------
# Handler factory — stateful qua closure `call_count = [0]`, phát response
# theo đúng thứ tự trong `sequence`.
# ---------------------------------------------------------------------------


def _make_ordered_handler(sequence: list[str], call_count: list[int]):
    """Handler cho `httpx.MockTransport` phát kết quả theo `sequence`.

    Contract mỗi call:
        - Tăng `call_count[0]` trước tiên (đo số request wire-level client
          thực sự gửi qua transport).
        - Lấy phần tử kế tiếp trong `sequence` theo index = call_count-1.
        - "timeout" → raise `httpx.TimeoutException`.
        - "5xx" → `httpx.Response(500)` (body error minimal).
        - "4xx" → `httpx.Response(400)` với `error.message` để khớp shape
          Stripe error thông thường (giá trị cụ thể không được assert —
          test này chỉ quan tâm hành vi retry).
        - "ok" → `httpx.Response(200, json=<StripePaymentPageInit shape>)`.

    Nếu client gọi quá số phần tử trong `sequence` → `AssertionError` để
    test fail rõ ràng thay vì hang hoặc "silent overrun".
    """

    def _handler(call):  # noqa: ANN001
        idx = call_count[0]
        call_count[0] += 1
        if idx >= len(sequence):
            raise AssertionError(
                f"Handler bị gọi {call_count[0]} lần nhưng sequence chỉ "
                f"có {len(sequence)} phần tử — client retry vượt giới hạn "
                "hoặc không dừng đúng lúc gặp OK (R2.6)."
            )
        outcome = sequence[idx]
        if outcome == "timeout":
            return http.TimeoutException("simulated timeout")
        if outcome == "5xx":
            return FakeResponse(500, json_body={"error": "server"})
        if outcome == "4xx":
            return FakeResponse(400, json_body={"error": {"message": "bad"}})
        if outcome == "ok":
            return FakeResponse(
                200,
                json_body={"init_checksum": "abc", "config_id": "def"},
            )
        raise AssertionError(f"outcome không hỗ trợ: {outcome!r}")

    return _handler


def _run_init(sequence: list[str], call_count: list[int]):
    """Chạy `StripeClient.init(...)` trong 1 event loop mới.

    Pattern chuẩn của repo khi kết hợp `hypothesis` (sync `@given`) với
    code async: mỗi hypothesis example dựng `httpx.AsyncClient(transport=
    MockTransport(...))` mới, khép trong `async with` để đảm bảo dọn dẹp
    kết nối giữa các example.
    """
    handler = _make_ordered_handler(sequence, call_count)

    async def _run():
        fake_session = FakeAsyncSession(default_handler=handler)
        client = StripeClient(
            http_client=fake_session,
            settings=_FakeSettings(),  # duck-typed thay SettingsRepository
            logger=logging.getLogger("test.stripe_client.retry"),
        )
        return await client.init(
            checkout_session_id="cs_test_abc",
            publishable_key="pk_test_xyz",
        )

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Test 1 — retry cho timeout/5xx cho tới khi gặp OK (không cạn attempts).
# ---------------------------------------------------------------------------


@settings(max_examples=50, deadline=None)
@given(
    sequence=st.lists(
        st.sampled_from(["timeout", "5xx"]),
        min_size=0,
        max_size=2,
    )
)
def test_retry_on_timeout_and_5xx_until_ok(sequence: list[str]) -> None:
    """*Với mọi* chuỗi 0-2 phần tử `timeout`/`5xx` **trước** 1 kết quả OK
    ở cuối, `StripeClient.init(...)`:

        - Trả `StripePaymentPageInit` hợp lệ (không raise).
        - Handler được gọi đúng `len(sequence) + 1` lần — retry đủ để
          vượt qua các lỗi tạm thời, dừng NGAY khi gặp OK (R2.6).

    `max_size=2` được chọn có chủ đích: `ideal.stripe_max_retry_attempts=3`
    → tối đa 2 lần fail trước 1 lần OK là vừa nằm trong giới hạn attempts,
    không rơi vào scenario "cạn retry" (đã được cover ở Test 3).

    **Validates: Requirements 2.6**
    """
    full_sequence = list(sequence) + ["ok"]
    call_count: list[int] = [0]

    result = _run_init(full_sequence, call_count)

    # Assert shape của StripePaymentPageInit — bằng chứng client đã đi
    # đúng nhánh success (`parse_stripe_payment_page_init`) chứ không im
    # lặng nuốt exception nào của tầng retry.
    assert result.init_checksum == "abc"
    assert result.config_id == "def"

    assert call_count[0] == len(full_sequence), (
        f"Số lần call handler phải bằng {len(full_sequence)} "
        f"(sequence={sequence!r} + ['ok']) nhưng ghi nhận {call_count[0]} "
        "— retry không dừng ngay khi gặp OK, hoặc dừng sớm hơn dự kiến "
        "(R2.6)."
    )


# ---------------------------------------------------------------------------
# Test 2 — 4xx KHÔNG retry, raise StripeHttpClientError ngay ở attempt 1.
# ---------------------------------------------------------------------------


@settings(max_examples=50, deadline=None)
@given(status_4xx=st.integers(min_value=400, max_value=499))
def test_no_retry_on_4xx_ever(status_4xx: int) -> None:
    """*Với mọi* HTTP status ∈ [400, 499], `_bounded_retry` raise
    `StripeHttpClientError` **ngay lập tức** ở attempt 1 — số lần call
    handler = 1, không có retry nào được thực hiện.

    R2.6: 4xx = lỗi validate/auth/param sai (client-side). Retry sẽ không
    làm thay đổi kết quả → Fail_Fast_Policy, tránh spam server với cùng
    payload sai.

    **Validates: Requirements 2.6**
    """
    call_count: list[int] = [0]

    def _handler(call):  # noqa: ANN001
        call_count[0] += 1
        return FakeResponse(status_4xx, json_body={"error": {"message": "bad"}})

    async def _run():
        fake_session = FakeAsyncSession(default_handler=_handler)
        client = StripeClient(
            http_client=fake_session,
            settings=_FakeSettings(),
            logger=logging.getLogger("test.stripe_client.retry"),
        )
        await client.init(
            checkout_session_id="cs_test_abc",
            publishable_key="pk_test_xyz",
        )

    with pytest.raises(StripeHttpClientError):
        asyncio.run(_run())

    assert call_count[0] == 1, (
        f"4xx KHÔNG được retry — số lần call phải = 1 nhưng ghi nhận "
        f"{call_count[0]} (status_4xx={status_4xx}, R2.6)."
    )


# ---------------------------------------------------------------------------
# Test 3 — chuỗi toàn 5xx → cạn retry sau max_attempts, raise
# StripeRetryExhaustedError.
# ---------------------------------------------------------------------------


def test_retry_exhausted_raises() -> None:
    """Handler luôn trả HTTP 500 → `_bounded_retry` cạn
    `ideal.stripe_max_retry_attempts=3` và raise
    `StripeRetryExhaustedError`. Số lần call handler = 3 (= max_attempts,
    bao gồm lần đầu).

    Test này KHÔNG cần `hypothesis` vì scenario "toàn 5xx" là 1 điểm duy
    nhất — hành vi deterministic, không có tham số hoá cần thiết.

    **Validates: Requirements 2.6**
    """
    call_count: list[int] = [0]

    def _handler(call):  # noqa: ANN001
        call_count[0] += 1
        return FakeResponse(500, json_body={"error": "server"})

    async def _run():
        fake_session = FakeAsyncSession(default_handler=_handler)
        client = StripeClient(
            http_client=fake_session,
            settings=_FakeSettings(),
            logger=logging.getLogger("test.stripe_client.retry"),
        )
        await client.init(
            checkout_session_id="cs_test_abc",
            publishable_key="pk_test_xyz",
        )

    with pytest.raises(StripeRetryExhaustedError):
        asyncio.run(_run())

    assert call_count[0] == _MAX_RETRY_ATTEMPTS, (
        f"Chuỗi toàn 5xx phải cạn đúng {_MAX_RETRY_ATTEMPTS} attempts "
        f"(= ideal.stripe_max_retry_attempts) nhưng ghi nhận "
        f"{call_count[0]} lần call (R2.6)."
    )


if __name__ == "__main__":  # pragma: no cover
    # Cho phép chạy file trực tiếp để smoke test cục bộ — production dùng
    # `pytest` từ backend/.
    pytest.main([__file__, "-v"])
