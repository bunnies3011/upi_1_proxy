"""Unit test cho `polling.py` (task 24, spec telegram-pull-job-mode).

Cover:
- `CallbackDedupeCache`: round-trip `seen()`/`add()` cơ bản; LRU evict
  phần tử cũ nhất khi vượt `maxsize=500`.
- `CallbackPollingTask`: dispatch đúng `on_message` vs `on_callback_query`
  theo field có mặt trong Update; update không có field nào bị bỏ qua
  nhưng offset vẫn tăng; HTTP 409 → `run()` return sạch + `conflict_detected
  = True`; dedupe chặn `callback_query` trùng id không gọi lại handler.
- `PollingSupervisor`: spawn khi `enabled=True` + token non-empty; cancel
  khi `enabled=False`; không respawn sau 409 cho tới khi token đổi.

Dùng `FakeTelegramBotClient`/`FakeSettings` duck-typed tối giản — 2 class
này chỉ implement đúng những method mà `polling.py` gọi tới
(`get_updates`/`reopen_polling_session`, `get`/`set`), không route qua
HTTP layer thật (khác với `test_telegram_client_pull_mode.py` — module
đó test chính `TelegramBotClient`, còn ở đây `polling.py` chỉ cần 1 client
duck-typed).

**Validates: Requirements 18.6, 18.7, 18.8**
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.notifiers.telegram.client import TelegramApiError
from app.notifiers.telegram.polling import (
    CallbackDedupeCache,
    CallbackPollingTask,
    PollingSupervisor,
)

# Khoảng chờ ngắn để 1 vòng loop polling kịp chạy xong batch đầu trước khi
# test cancel — nhỏ hơn NHIỀU so với `_TRANSIENT_ERROR_BACKOFF_SECONDS`
# (1.0s) nên không lo dính backoff của lỗi transient trong lúc test.
_SETTLE_SECONDS = 0.05


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeSettings:
    """Stub `SettingsRepository` — snapshot in-memory mutable, đổi được
    giữa các lần đọc (dùng cho test `PollingSupervisor` cần simulate
    setting thay đổi qua nhiều tick).
    """

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.values: dict[str, Any] = dict(values) if values else {}
        self.set_calls: list[tuple[str, Any]] = []

    async def get(self, key: str) -> Any:
        return self.values.get(key)

    async def set(self, key: str, value: Any) -> None:
        self.values[key] = value
        self.set_calls.append((key, value))


class FakeTelegramBotClient:
    """Duck-typed fake cho `TelegramBotClient` — chỉ implement 3 method
    mà `polling.py` gọi tới: `get_updates`, `close_polling_session`,
    `reopen_polling_session`.

    `responses`: list cấu hình trả về CHO TỪNG LẦN GỌI `get_updates` theo
    thứ tự (item là `list[dict]` batch update, hoặc 1 `Exception`
    instance để raise). Sau khi hết list, mọi lần gọi tiếp theo trả `[]`
    (batch rỗng vô hạn) — cho phép vòng `while True` trong
    `CallbackPollingTask.run()` tiếp tục chạy "harmlessly" tới khi test
    tự cancel.
    """

    def __init__(self, responses: list[Any] | None = None) -> None:
        self._responses = list(responses) if responses is not None else []
        self._call_index = 0
        self.calls: list[tuple[str, int]] = []
        self.reopen_calls = 0
        self.close_calls = 0

    async def get_updates(
        self, token: str, offset: int, *, timeout: int = 25
    ) -> list[dict]:
        self.calls.append((token, offset))
        # QUAN TRỌNG: phải có 1 điểm `await` thực sự yield control về event
        # loop (`asyncio.sleep(0)`) — nếu không, vòng lặp `while True` của
        # `CallbackPollingTask.run()` chạy busy-loop đồng bộ (coroutine giả
        # không có I/O thật không tự nhường control), khiến task test
        # KHÔNG BAO GIỜ có cơ hội chạy `asyncio.sleep(_SETTLE_SECONDS)` để
        # cancel — treo vô hạn.
        await asyncio.sleep(0)
        if self._call_index < len(self._responses):
            item = self._responses[self._call_index]
            self._call_index += 1
        else:
            item = []
        if isinstance(item, Exception):
            raise item
        return item

    async def close_polling_session(self) -> None:
        self.close_calls += 1

    async def reopen_polling_session(self) -> None:
        self.reopen_calls += 1


async def _noop_on_message(_message: dict) -> None:
    pass


async def _noop_on_callback_query(_callback_query: dict) -> None:
    pass


async def _run_briefly_then_cancel(task_instance: CallbackPollingTask) -> None:
    """Chạy `task_instance.run()` như background task, chờ 1 khoảng ngắn
    để batch đầu được xử lý, rồi cancel — cần thiết vì `run()` là vòng
    lặp vô hạn, chỉ 409/CancelledError mới làm nó thoát tự nhiên.
    """
    task = asyncio.create_task(task_instance.run())
    await asyncio.sleep(_SETTLE_SECONDS)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# CallbackDedupeCache
# ---------------------------------------------------------------------------


def test_dedupe_cache_seen_add_round_trip() -> None:
    cache = CallbackDedupeCache()

    assert cache.seen("cb-1") is False

    cache.add("cb-1")

    assert cache.seen("cb-1") is True
    assert cache.seen("cb-2") is False


def test_dedupe_cache_evicts_oldest_when_exceeding_maxsize() -> None:
    """Thêm 501 phần tử vào cache `maxsize=500` (mặc định) — phần tử ĐẦU
    TIÊN (cũ nhất) phải bị evict, phần tử MỚI NHẤT vẫn còn.
    """
    cache = CallbackDedupeCache()

    for i in range(501):
        cache.add(f"cb-{i}")

    assert cache.seen("cb-0") is False
    assert cache.seen("cb-500") is True


# ---------------------------------------------------------------------------
# CallbackPollingTask — dispatch message vs callback_query
# ---------------------------------------------------------------------------


async def test_dispatch_routes_message_and_callback_query_correctly() -> None:
    settings = FakeSettings({"telegram.polling_offset": 0})
    client = FakeTelegramBotClient(
        responses=[
            [
                {"update_id": 1, "message": {"text": "hi"}},
                {"update_id": 2, "callback_query": {"id": "cb-1", "data": "x"}},
            ],
        ]
    )
    dedupe = CallbackDedupeCache()
    messages: list[dict] = []
    callbacks: list[dict] = []

    async def on_message(message: dict) -> None:
        messages.append(message)

    async def on_callback_query(callback_query: dict) -> None:
        callbacks.append(callback_query)

    task_instance = CallbackPollingTask(
        settings, client, dedupe, "tok", on_message, on_callback_query
    )
    await _run_briefly_then_cancel(task_instance)

    assert messages == [{"text": "hi"}]
    assert callbacks == [{"id": "cb-1", "data": "x"}]


async def test_update_without_message_or_callback_query_is_skipped_but_offset_advances() -> None:
    settings = FakeSettings({"telegram.polling_offset": 0})
    client = FakeTelegramBotClient(
        responses=[
            [{"update_id": 5, "edited_message": {"text": "edited"}}],
        ]
    )
    dedupe = CallbackDedupeCache()
    call_counts = {"message": 0, "callback_query": 0}

    async def on_message(_message: dict) -> None:
        call_counts["message"] += 1

    async def on_callback_query(_callback_query: dict) -> None:
        call_counts["callback_query"] += 1

    task_instance = CallbackPollingTask(
        settings, client, dedupe, "tok", on_message, on_callback_query
    )
    await _run_briefly_then_cancel(task_instance)

    assert call_counts == {"message": 0, "callback_query": 0}
    # offset phải tăng lên max(update_id) + 1 = 6, dù update_id=5 bị bỏ qua.
    assert settings.values["telegram.polling_offset"] == 6


async def test_http_409_sets_conflict_flag_and_returns_cleanly() -> None:
    settings = FakeSettings({"telegram.polling_offset": 0})
    client = FakeTelegramBotClient(
        responses=[
            TelegramApiError(status_code=409, error_code=409, description="Conflict"),
        ]
    )
    dedupe = CallbackDedupeCache()

    task_instance = CallbackPollingTask(
        settings, client, dedupe, "tok", _noop_on_message, _noop_on_callback_query
    )

    # 409 phải làm run() return sạch KHÔNG cần cancel từ ngoài.
    await task_instance.run()

    assert task_instance.conflict_detected is True


async def test_dedupe_blocks_duplicate_callback_query_id() -> None:
    settings = FakeSettings({"telegram.polling_offset": 0})
    client = FakeTelegramBotClient(
        responses=[
            [{"update_id": 1, "callback_query": {"id": "cb-dup"}}],
            [{"update_id": 2, "callback_query": {"id": "cb-dup"}}],
        ]
    )
    dedupe = CallbackDedupeCache()
    callbacks: list[dict] = []

    async def on_callback_query(callback_query: dict) -> None:
        callbacks.append(callback_query)

    task_instance = CallbackPollingTask(
        settings, client, dedupe, "tok", _noop_on_message, on_callback_query
    )
    await _run_briefly_then_cancel(task_instance)

    assert len(callbacks) == 1


# ---------------------------------------------------------------------------
# PollingSupervisor
# ---------------------------------------------------------------------------


async def test_reconcile_spawns_task_when_enabled_and_token_present() -> None:
    settings = FakeSettings(
        {
            "telegram.polling_enabled": True,
            "telegram.bot_token": "abc",
            "telegram.polling_offset": 0,
        }
    )
    client = FakeTelegramBotClient()
    dedupe = CallbackDedupeCache()
    supervisor = PollingSupervisor(
        settings, client, dedupe, _noop_on_message, _noop_on_callback_query
    )

    await supervisor._reconcile()

    assert supervisor._task is not None
    assert not supervisor._task.done()

    await supervisor.shutdown()


async def test_reconcile_cancels_task_when_disabled() -> None:
    settings = FakeSettings(
        {
            "telegram.polling_enabled": True,
            "telegram.bot_token": "abc",
            "telegram.polling_offset": 0,
        }
    )
    client = FakeTelegramBotClient()
    dedupe = CallbackDedupeCache()
    supervisor = PollingSupervisor(
        settings, client, dedupe, _noop_on_message, _noop_on_callback_query
    )

    await supervisor._reconcile()
    assert supervisor._task is not None

    settings.values["telegram.polling_enabled"] = False
    await supervisor._reconcile()

    assert supervisor._task is None


async def test_no_respawn_after_conflict_until_token_changes() -> None:
    settings = FakeSettings(
        {
            "telegram.polling_enabled": True,
            "telegram.bot_token": "abc",
            "telegram.polling_offset": 0,
        }
    )
    client = FakeTelegramBotClient(
        responses=[
            TelegramApiError(status_code=409, error_code=409, description="Conflict"),
        ]
    )
    dedupe = CallbackDedupeCache()
    supervisor = PollingSupervisor(
        settings, client, dedupe, _noop_on_message, _noop_on_callback_query
    )

    # Tick 1: spawn task cho token "abc" — task này sẽ gặp 409 ngay lần
    # get_updates đầu và tự kết thúc (conflict_detected=True).
    await supervisor._reconcile()
    assert supervisor._task is not None
    await asyncio.sleep(_SETTLE_SECONDS)
    assert supervisor._task.done()

    # Tick 2: reap task đã done, phát hiện conflict → suppress respawn.
    await supervisor._reconcile()
    assert supervisor._task is None
    assert supervisor._suppressed_pair == ("abc", True)

    # Tick 3: (token, enabled) vẫn giống pair bị suppress → KHÔNG spawn lại.
    await supervisor._reconcile()
    assert supervisor._task is None

    # Đổi token → suppression phải được clear, spawn lại bình thường.
    settings.values["telegram.bot_token"] = "xyz"
    await supervisor._reconcile()

    assert supervisor._task is not None
    assert supervisor._suppressed_pair is None

    await supervisor.shutdown()


# ---------------------------------------------------------------------------
# PollingSupervisor — FIX D: reset polling_offset khi ĐỔI SANG BOT KHÁC
# ---------------------------------------------------------------------------


async def test_offset_reset_when_switching_to_different_bot() -> None:
    """Đổi `bot_token` sang bot KHÁC (bot_id trước dấu ':' khác) → offset
    (thường cao, per-bot) phải reset về 0 để bot mới không "như chết".
    """
    settings = FakeSettings(
        {
            "telegram.polling_enabled": True,
            "telegram.bot_token": "111:aaa",
            "telegram.polling_offset": 42,
        }
    )
    client = FakeTelegramBotClient()
    dedupe = CallbackDedupeCache()
    supervisor = PollingSupervisor(
        settings, client, dedupe, _noop_on_message, _noop_on_callback_query
    )

    # Tick 1: spawn ĐẦU cho bot 111 — KHÔNG reset, resume offset đã persist.
    await supervisor._reconcile()
    assert settings.values["telegram.polling_offset"] == 42

    # Đổi sang bot 222 (bot_id khác) → tick kế phải reset offset về 0.
    settings.values["telegram.bot_token"] = "222:bbb"
    await supervisor._reconcile()

    assert settings.values["telegram.polling_offset"] == 0
    assert ("telegram.polling_offset", 0) in settings.set_calls

    await supervisor.shutdown()


async def test_offset_preserved_on_same_bot_token_rotation() -> None:
    """ROTATE token CÙNG bot (cùng bot_id, revoke+reissue auth hash) →
    offset PHẢI giữ nguyên vì `update_id` sequence tiếp tục.
    """
    settings = FakeSettings(
        {
            "telegram.polling_enabled": True,
            "telegram.bot_token": "111:aaa",
            "telegram.polling_offset": 42,
        }
    )
    client = FakeTelegramBotClient()
    dedupe = CallbackDedupeCache()
    supervisor = PollingSupervisor(
        settings, client, dedupe, _noop_on_message, _noop_on_callback_query
    )

    await supervisor._reconcile()

    # Cùng bot_id 111, chỉ đổi phần auth hash → KHÔNG reset offset.
    settings.values["telegram.bot_token"] = "111:ccc"
    await supervisor._reconcile()

    assert settings.values["telegram.polling_offset"] == 42
    assert ("telegram.polling_offset", 0) not in settings.set_calls

    await supervisor.shutdown()
