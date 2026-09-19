"""Unit tests for `/chotky` command (admin gate + confirm, not worker-stats)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.notifiers.telegram.commands import (
    _CHOTKY_CONFIRM_PROMPT,
    _CHOTKY_REFUSE_TEXT,
    _CHOTKY_UNAVAILABLE_TEXT,
    _COMMAND_PATTERN,
    format_chotky_summary,
    handle_message,
)
from app.notifiers.telegram.pull_mode import PullJobCoordinator

_KEY_ADMIN_USER_IDS = "telegram.pull_mode.admin_user_ids"


class FakeTelegramBotClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, dict | None]] = []
        self.answer_calls: list[tuple[str, str, str, bool]] = []
        self.edit_calls: list[tuple[str, str, int]] = []

    async def send_message(
        self,
        token: str,
        chat_id: str,
        text: str,
        *,
        reply_markup: dict | None = None,
    ) -> dict:
        self.calls.append((token, chat_id, text, reply_markup))
        return {}

    async def answer_callback_query(
        self,
        token: str,
        callback_query_id: str,
        *,
        text: str = "",
        show_alert: bool = False,
    ) -> dict:
        self.answer_calls.append((token, callback_query_id, text, show_alert))
        return {}

    async def edit_message_reply_markup(
        self,
        token: str,
        chat_id: str,
        message_id: int,
        *,
        reply_markup: dict | None = None,
    ) -> dict:
        self.edit_calls.append((token, chat_id, message_id))
        return {}


class FakeSettings:
    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.values: dict[str, Any] = dict(values) if values else {}

    async def get(self, key: str) -> Any:
        return self.values.get(key)


class FakeJobManager:
    def __init__(self) -> None:
        self.reset_all_worker_stats = AsyncMock()

    async def get_worker_stats_ranking(self) -> list[dict]:
        return []


def _make_message(
    *, text: str, chat_id: int = 123, from_user: dict | None = None
) -> dict:
    message: dict = {"chat": {"id": chat_id}, "text": text}
    if from_user is not None:
        message["from"] = from_user
    return message


async def _bot_token_getter() -> str:
    return "tok"


def _make_coordinator(
    *,
    client: FakeTelegramBotClient,
    hook: Any = None,
) -> PullJobCoordinator:
    from app.notifiers.telegram.status_card import WorkerStatusCardTracker

    status_card = WorkerStatusCardTracker(client=client)  # type: ignore[arg-type]
    return PullJobCoordinator(
        job_manager=FakeJobManager(),  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        settings=FakeSettings(),  # type: ignore[arg-type]
        bot_token_getter=_bot_token_getter,
        status_card=status_card,
        reset_batch_tally_hook=hook,
    )


# ---------------------------------------------------------------------------
# Regex + dispatch isolation from /reset
# ---------------------------------------------------------------------------


def test_command_pattern_matches_chotky() -> None:
    assert _COMMAND_PATTERN.match("/chotky") is not None
    assert _COMMAND_PATTERN.match("/chotky@MyBot") is not None
    assert _COMMAND_PATTERN.match("/chotky extra") is None


@pytest.mark.asyncio
async def test_chotky_never_invokes_worker_stats_reset() -> None:
    """Explicit chotky branch must not fall through to `_handle_reset`."""
    client = FakeTelegramBotClient()
    job_manager = FakeJobManager()
    settings = FakeSettings({_KEY_ADMIN_USER_IDS: ["456"]})
    hook = AsyncMock(
        return_value={
            "closed": [],
            "skipped": [],
            "plus_total": 0,
            "expired_total": 0,
        }
    )

    await handle_message(
        _make_message(text="/chotky", from_user={"id": 456}),
        client=client,
        bot_token="tok",
        job_manager=job_manager,  # type: ignore[arg-type]
        settings=settings,  # type: ignore[arg-type]
        reset_batch_tally_hook=hook,
    )

    job_manager.reset_all_worker_stats.assert_not_awaited()
    # Confirm step only — hook not yet called.
    hook.assert_not_awaited()
    assert len(client.calls) == 1
    _tok, _chat, text, reply_markup = client.calls[0]
    assert text == _CHOTKY_CONFIRM_PROMPT
    assert reply_markup is not None
    callbacks = {
        btn["callback_data"] for btn in reply_markup["inline_keyboard"][0]
    }
    assert callbacks == {
        "pull_job:chotky_confirm:456",
        "pull_job:chotky_cancel:456",
    }
    # Must NOT be worker-stats reset buttons.
    assert "pull_job:reset_confirm:456" not in callbacks


# ---------------------------------------------------------------------------
# Admin gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chotky_empty_admin_list_refuses_everyone() -> None:
    client = FakeTelegramBotClient()
    hook = AsyncMock()

    await handle_message(
        _make_message(text="/chotky", from_user={"id": 999}),
        client=client,
        bot_token="tok",
        job_manager=FakeJobManager(),  # type: ignore[arg-type]
        settings=FakeSettings({_KEY_ADMIN_USER_IDS: []}),  # type: ignore[arg-type]
        reset_batch_tally_hook=hook,
    )

    hook.assert_not_awaited()
    assert len(client.calls) == 1
    assert client.calls[0][2] == _CHOTKY_REFUSE_TEXT


@pytest.mark.asyncio
async def test_chotky_unset_admin_list_refuses() -> None:
    client = FakeTelegramBotClient()
    hook = AsyncMock()

    await handle_message(
        _make_message(text="/chotky", from_user={"id": 1}),
        client=client,
        bot_token="tok",
        job_manager=FakeJobManager(),  # type: ignore[arg-type]
        settings=FakeSettings(),  # type: ignore[arg-type]
        reset_batch_tally_hook=hook,
    )

    hook.assert_not_awaited()
    assert client.calls[0][2] == _CHOTKY_REFUSE_TEXT


@pytest.mark.asyncio
async def test_chotky_non_admin_refused() -> None:
    client = FakeTelegramBotClient()
    hook = AsyncMock()

    await handle_message(
        _make_message(text="/chotky", from_user={"id": 111}),
        client=client,
        bot_token="tok",
        job_manager=FakeJobManager(),  # type: ignore[arg-type]
        settings=FakeSettings({_KEY_ADMIN_USER_IDS: ["456", "789"]}),  # type: ignore[arg-type]
        reset_batch_tally_hook=hook,
    )

    hook.assert_not_awaited()
    assert client.calls[0][2] == _CHOTKY_REFUSE_TEXT


@pytest.mark.asyncio
async def test_chotky_admin_passes_to_confirm_step_hook_not_called() -> None:
    client = FakeTelegramBotClient()
    hook = AsyncMock()

    await handle_message(
        _make_message(text="/chotky", from_user={"id": 456}),
        client=client,
        bot_token="tok",
        job_manager=FakeJobManager(),  # type: ignore[arg-type]
        settings=FakeSettings({_KEY_ADMIN_USER_IDS: ["456"]}),  # type: ignore[arg-type]
        reset_batch_tally_hook=hook,
    )

    hook.assert_not_awaited()
    assert client.calls[0][2] == _CHOTKY_CONFIRM_PROMPT


@pytest.mark.asyncio
async def test_chotky_none_hook_unavailable() -> None:
    client = FakeTelegramBotClient()

    await handle_message(
        _make_message(text="/chotky", from_user={"id": 456}),
        client=client,
        bot_token="tok",
        job_manager=FakeJobManager(),  # type: ignore[arg-type]
        settings=FakeSettings({_KEY_ADMIN_USER_IDS: ["456"]}),  # type: ignore[arg-type]
        reset_batch_tally_hook=None,
    )

    assert len(client.calls) == 1
    assert client.calls[0][2] == _CHOTKY_UNAVAILABLE_TEXT


@pytest.mark.asyncio
async def test_stats_and_non_command_unaffected() -> None:
    client = FakeTelegramBotClient()
    settings = FakeSettings({_KEY_ADMIN_USER_IDS: ["456"]})
    hook = AsyncMock()

    await handle_message(
        _make_message(text="hello", from_user={"id": 456}),
        client=client,
        bot_token="tok",
        job_manager=FakeJobManager(),  # type: ignore[arg-type]
        settings=settings,  # type: ignore[arg-type]
        reset_batch_tally_hook=hook,
    )
    assert client.calls == []
    hook.assert_not_awaited()

    await handle_message(
        _make_message(text="/stats", from_user={"id": 456}),
        client=client,
        bot_token="tok",
        job_manager=FakeJobManager(),  # type: ignore[arg-type]
        settings=settings,  # type: ignore[arg-type]
        reset_batch_tally_hook=hook,
    )
    assert len(client.calls) == 1
    assert "Chưa có dữ liệu thống kê" in client.calls[0][2]
    hook.assert_not_awaited()


# ---------------------------------------------------------------------------
# Confirm callback — hook runs only after admin gate + confirm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chotky_confirm_awaits_hook_and_replies_summary() -> None:
    client = FakeTelegramBotClient()
    summary = {
        "closed": ["-1001"],
        "skipped": ["-1002"],
        "plus_total": 5,
        "expired_total": 1,
    }
    hook = AsyncMock(return_value=summary)
    coordinator = _make_coordinator(client=client, hook=hook)

    await coordinator.handle_callback(
        {
            "id": "cb1",
            "data": "pull_job:chotky_confirm:456",
            "message": {"chat": {"id": 123}, "message_id": 9},
            "from": {"id": 456},
        }
    )

    hook.assert_awaited_once()
    assert any(
        format_chotky_summary(summary) == text for _t, _c, text, _m in client.calls
    )


@pytest.mark.asyncio
async def test_chotky_confirm_wrong_requester_does_not_call_hook() -> None:
    client = FakeTelegramBotClient()
    hook = AsyncMock()
    coordinator = _make_coordinator(client=client, hook=hook)

    await coordinator.handle_callback(
        {
            "id": "cb1",
            "data": "pull_job:chotky_confirm:456",
            "message": {"chat": {"id": 123}, "message_id": 9},
            "from": {"id": 999},
        }
    )

    hook.assert_not_awaited()


@pytest.mark.asyncio
async def test_chotky_cancel_does_not_call_hook() -> None:
    client = FakeTelegramBotClient()
    hook = AsyncMock()
    coordinator = _make_coordinator(client=client, hook=hook)

    await coordinator.handle_callback(
        {
            "id": "cb1",
            "data": "pull_job:chotky_cancel:456",
            "message": {"chat": {"id": 123}, "message_id": 9},
            "from": {"id": 456},
        }
    )

    hook.assert_not_awaited()
    assert any("Đã hủy chốt kỳ" in text for _t, _c, text, _m in client.calls)


def test_format_chotky_summary_includes_skipped() -> None:
    text = format_chotky_summary(
        {
            "closed": ["a"],
            "skipped": ["b", "c"],
            "plus_total": 3,
            "expired_total": 1,
        }
    )
    assert "Plus: 3" in text
    assert "Hết hạn: 1" in text
    assert "b, c" in text
