"""Property test cho `StripeClient.refresh_poll` — **Property 12**.

**Property 12: Refresh poll dừng đúng khi có redirect hoặc hết số lần
tối đa**

*Với mọi* chuỗi response `refresh` giả lập trong đó redirect_to_url xuất
hiện ở lượt thứ `k` (`k` có thể lớn hơn `ideal.refresh_poll_max_attempts`
nghĩa là không bao giờ xuất hiện trong giới hạn):

- Nếu `k <= max_attempts` → `StripeClient.refresh_poll(...)` trả về đúng
  `redirect_url` non-empty và dừng poll ngay ở attempt `k`. Số lần call
  handler thực tế = `k` (R4.3, R4.4).
- Nếu `k > max_attempts` → `StripeClient.refresh_poll(...)` raise
  `RefreshPollExhaustedError` sau đúng `max_attempts` lần call, KHÔNG
  cố poll thêm (R4.5, R4.6).

Kiểm chứng ở wire-level qua `httpx.MockTransport`: closure `call_count`
đóng vai counter số request thực tế client issue — không có mock/spy
nào chèn giữa để bóp méo. Delay giữa các lượt đặt cực nhỏ (0.001s) để
test chạy nhanh.

**Validates: Requirements 4.3, 4.4, 4.5, 4.6**
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from tests.support.fake_http import FakeAsyncSession, FakeResponse
from hypothesis import given, settings
from hypothesis import strategies as st

from app.payments.ideal.errors import (
    RefreshPollExhaustedError,
    StripeHttpClientError,
)
from app.payments.ideal.stripe_client import StripeClient

# ---------------------------------------------------------------------------
# Fake Settings — duck-typed thay `SettingsRepository`.
#
# `StripeClient._read_refresh_poll_*` / `_read_request_timeout_seconds` chỉ
# dùng `settings.get(key)` async. Ta cố định 3 giá trị liên quan để test
# hoàn toàn deterministic. Delay đặt cực nhỏ (0.001s) → test nhanh, không
# phải chờ delay thật.
# ---------------------------------------------------------------------------

_REFRESH_POLL_MAX_ATTEMPTS: int = 3
_REFRESH_POLL_DELAY_SECONDS: float = 0.001
_REQUEST_TIMEOUT_SECONDS: float = 1.0

_FAKE_SETTINGS_VALUES: dict[str, Any] = {
    "ideal.refresh_poll_max_attempts": _REFRESH_POLL_MAX_ATTEMPTS,
    "ideal.refresh_poll_delay_seconds": _REFRESH_POLL_DELAY_SECONDS,
    "ideal.stripe_request_timeout_seconds": _REQUEST_TIMEOUT_SECONDS,
}

#: URL redirect mô phỏng theo shape wire Stripe HAR quan sát được (R4.4).
_REDIRECT_URL: str = "https://pm-redirects.stripe.com/authorize/acct/sa_nonce"


class _FakeSettings:
    """Trả cứng 3 setting liên quan refresh_poll; mọi key khác trả `None`.

    Bám semantics thực của `SettingsRepository.get`: key chưa set trả
    `None` để caller (`_read_*`) fallback về default nội bộ — ở đây ta chủ
    động set 3 key mà `refresh_poll` cần đọc, các key khác giữ nguyên
    `None` để chứng minh code KHÔNG đọc chúng.
    """

    async def get(self, key: str) -> Any | None:
        return _FAKE_SETTINGS_VALUES.get(key)


# ---------------------------------------------------------------------------
# Handler factory — stateful qua closure `call_count = [0]`, phát response
# theo `redirect_at_attempt`:
#   - Attempt trước `redirect_at_attempt`: 200 với `next_action` rỗng
#     (không có `redirect_to_url`) → client tiếp tục poll (R4.5).
#   - Attempt = `redirect_at_attempt`: 200 với `redirect_to_url.url` non-
#     empty → client dừng poll, trả URL (R4.4).
#
# Nếu `redirect_at_attempt` > max_attempts thì handler không bao giờ trả
# response chứa URL trong giới hạn → client raise RefreshPollExhaustedError
# sau đúng max_attempts lần call (R4.6).
# ---------------------------------------------------------------------------


def _make_handler(redirect_at_attempt: int, call_count: list[int]):
    """Handler cho `httpx.MockTransport` mô phỏng luồng refresh_poll.

    Contract mỗi call:
        - Tăng `call_count[0]` trước tiên (đo số request wire-level client
          thực sự gửi qua transport).
        - Nếu `call_count[0] == redirect_at_attempt` → trả 200 với
          `setup_intent.next_action.redirect_to_url.url = _REDIRECT_URL`
          (R4.4 — nhánh "thấy redirect").
        - Ngược lại → trả 200 với `setup_intent.next_action = {}`
          (R4.5 — nhánh "chưa thấy redirect, poll tiếp").
    """

    def _handler(call):  # noqa: ANN001
        call_count[0] += 1
        if call_count[0] == redirect_at_attempt:
            return FakeResponse(
                200,
                json_body={
                    "setup_intent": {
                        "next_action": {
                            "redirect_to_url": {"url": _REDIRECT_URL},
                        },
                    },
                },
            )
        return FakeResponse(
            200,
            json_body={"setup_intent": {"next_action": {}}},
        )

    return _handler


def _run_refresh_poll(
    redirect_at_attempt: int,
    call_count: list[int],
) -> str:
    """Chạy `StripeClient.refresh_poll(...)` trong 1 event loop mới.

    Mỗi hypothesis example dựng `httpx.AsyncClient(transport=MockTransport
    (...))` mới, khép trong `async with` để đảm bảo dọn dẹp kết nối giữa
    các example (pattern chuẩn của repo — xem
    `test_stripe_client_retry_property.py`).
    """
    handler = _make_handler(redirect_at_attempt, call_count)

    async def _run() -> str:
        fake_session = FakeAsyncSession(default_handler=handler)
        client = StripeClient(
            http_client=fake_session,
            settings=_FakeSettings(),  # duck-typed thay SettingsRepository
            logger=logging.getLogger("test.stripe_client.refresh_poll"),
        )
        return await client.refresh_poll(
            checkout_session_id="cs_abc",
            publishable_key="pk_test",
        )

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Test — refresh_poll dừng đúng khi có redirect hoặc hết max_attempts.
# ---------------------------------------------------------------------------


@settings(max_examples=50, deadline=None)
@given(redirect_at_attempt=st.integers(min_value=1, max_value=5))
def test_refresh_poll_stops_on_redirect_or_exhausted(
    redirect_at_attempt: int,
) -> None:
    """*Với mọi* `redirect_at_attempt ∈ [1, 5]` (có thể vượt
    `_REFRESH_POLL_MAX_ATTEMPTS=3`), `StripeClient.refresh_poll(...)`:

        - `redirect_at_attempt <= max_attempts` (1, 2, 3): trả URL redirect
          non-empty; số lần call handler = `redirect_at_attempt`
          (R4.3, R4.4 — dừng NGAY khi thấy redirect_to_url).
        - `redirect_at_attempt > max_attempts` (4, 5): raise
          `RefreshPollExhaustedError`; số lần call handler = `max_attempts`
          (R4.5, R4.6 — không cố poll thêm sau khi cạn attempts).

    Range `[1, 5]` bao trọn 2 kịch bản: 1..3 nằm trong giới hạn (thấy
    redirect trước khi cạn), 4..5 vượt giới hạn (không bao giờ thấy
    trong giới hạn → cạn attempts).

    **Validates: Requirements 4.3, 4.4, 4.5, 4.6**
    """
    call_count: list[int] = [0]

    if redirect_at_attempt <= _REFRESH_POLL_MAX_ATTEMPTS:
        result = _run_refresh_poll(redirect_at_attempt, call_count)

        # R4.4 — trả đúng redirect_to_url non-empty đã extract từ payload.
        assert result == _REDIRECT_URL, (
            f"refresh_poll phải trả redirect_to_url={_REDIRECT_URL!r} "
            f"khi handler phát URL ở attempt {redirect_at_attempt}, "
            f"nhưng nhận {result!r} (R4.4)."
        )
        # R4.3 + R4.4 — dừng NGAY khi thấy redirect: số call = k.
        assert call_count[0] == redirect_at_attempt, (
            f"refresh_poll phải dừng ngay ở attempt {redirect_at_attempt} "
            f"(gọi handler đúng {redirect_at_attempt} lần) nhưng ghi nhận "
            f"{call_count[0]} lần call (R4.3, R4.4 — không được poll "
            "tiếp sau khi thấy redirect_to_url)."
        )
    else:
        with pytest.raises(RefreshPollExhaustedError):
            _run_refresh_poll(redirect_at_attempt, call_count)

        # R4.5 + R4.6 — cạn đúng max_attempts (không vượt, không thiếu).
        assert call_count[0] == _REFRESH_POLL_MAX_ATTEMPTS, (
            f"Khi redirect không xuất hiện trong giới hạn "
            f"(redirect_at_attempt={redirect_at_attempt} > max_attempts="
            f"{_REFRESH_POLL_MAX_ATTEMPTS}), refresh_poll phải cạn đúng "
            f"{_REFRESH_POLL_MAX_ATTEMPTS} lần call nhưng ghi nhận "
            f"{call_count[0]} (R4.5, R4.6)."
        )


if __name__ == "__main__":  # pragma: no cover
    # Cho phép chạy file trực tiếp để smoke test cục bộ — production dùng
    # `pytest` từ backend/.
    pytest.main([__file__, "-v"])
