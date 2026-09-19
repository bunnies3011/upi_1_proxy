"""Unit test cho `commands.py` (task 26, spec telegram-pull-job-mode).

Cover:
- `_COMMAND_PATTERN`: khớp `/start`, `/start@MyBot`; KHÔNG khớp
  `/start abc`, `/reset extra`.
- `handle_message`: message không có `from` → không reply.
- `/start`: bị chặn khi chat không nằm trong `allowed_chat_ids`; được
  reply kèm nút `pull_job:claim` khi `allowed_chat_ids` rỗng.
- `/stats`: rỗng → đúng message; có data → đúng format + đúng thứ tự
  (không tự sort lại).
- `/reset`: đúng 2 nút với `requester_id` = telegram_user_id người gửi.

Dùng `FakeTelegramBotClient`/`FakeSettings`/`FakeJobManager` duck-typed
tối giản, cùng pattern với `test_telegram_polling.py` (task 24) — chỉ
implement đúng method mà `commands.py` gọi tới.

**Validates: Requirements 16.3, 17.1, 19.4, 19.5, 19.6**
"""

from __future__ import annotations

from typing import Any

from app.notifiers.telegram.commands import _COMMAND_PATTERN, handle_message

_KEY_ALLOWED_CHAT_IDS = "telegram.pull_mode.allowed_chat_ids"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeTelegramBotClient:
    """Duck-typed fake cho `TelegramBotClient` — chỉ implement
    `send_message`, ghi lại mọi lần gọi để assert.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, dict | None]] = []

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


class FakeSettings:
    """Stub `SettingsRepository` — trả giá trị cấu hình sẵn từ dict."""

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.values: dict[str, Any] = dict(values) if values else {}

    async def get(self, key: str) -> Any:
        return self.values.get(key)


class FakeJobManager:
    """Stub `JobManager` — chỉ implement `get_worker_stats_ranking`."""

    def __init__(self, ranking: list[dict] | None = None) -> None:
        self._ranking = ranking if ranking is not None else []

    async def get_worker_stats_ranking(self) -> list[dict]:
        return self._ranking


def _make_message(
    *, text: str, chat_id: int = 123, from_user: dict | None = None
) -> dict:
    message: dict = {"chat": {"id": chat_id}, "text": text}
    if from_user is not None:
        message["from"] = from_user
    return message


# ---------------------------------------------------------------------------
# _COMMAND_PATTERN
# ---------------------------------------------------------------------------


def test_command_pattern_matches_bare_command() -> None:
    assert _COMMAND_PATTERN.match("/start") is not None
    assert _COMMAND_PATTERN.match("/stats") is not None
    assert _COMMAND_PATTERN.match("/reset") is not None
    assert _COMMAND_PATTERN.match("/chotky") is not None
    # Exact command set in the regex alternation.
    assert _COMMAND_PATTERN.pattern == r"^/(start|stats|reset|chotky)(@\w+)?$"


def test_command_pattern_matches_with_bot_username_suffix() -> None:
    assert _COMMAND_PATTERN.match("/start@MyBot") is not None


def test_command_pattern_does_not_match_with_extra_argument() -> None:
    assert _COMMAND_PATTERN.match("/start abc") is None
    assert _COMMAND_PATTERN.match("/reset extra") is None


# ---------------------------------------------------------------------------
# handle_message — message không có `from`
# ---------------------------------------------------------------------------


async def test_message_without_from_is_ignored() -> None:
    client = FakeTelegramBotClient()
    message = _make_message(text="/start", from_user=None)

    await handle_message(
        message,
        client=client,
        bot_token="tok",
        job_manager=FakeJobManager(),
        settings=FakeSettings(),
    )

    assert client.calls == []


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------


async def test_start_blocked_when_chat_not_in_allowed_chat_ids() -> None:
    client = FakeTelegramBotClient()
    settings = FakeSettings({_KEY_ALLOWED_CHAT_IDS: ["999"]})
    message = _make_message(
        text="/start", chat_id=123, from_user={"id": 456}
    )

    await handle_message(
        message,
        client=client,
        bot_token="tok",
        job_manager=FakeJobManager(),
        settings=settings,
    )

    assert client.calls == []


async def test_start_allowed_when_allowed_chat_ids_empty_sends_claim_button() -> None:
    client = FakeTelegramBotClient()
    settings = FakeSettings({_KEY_ALLOWED_CHAT_IDS: []})
    message = _make_message(
        text="/start", chat_id=123, from_user={"id": 456}
    )

    await handle_message(
        message,
        client=client,
        bot_token="tok",
        job_manager=FakeJobManager(),
        settings=settings,
    )

    assert len(client.calls) == 1
    _token, chat_id, _text, reply_markup = client.calls[0]
    assert chat_id == "123"
    assert reply_markup is not None
    buttons = reply_markup["inline_keyboard"][0]
    assert any(button["callback_data"] == "pull_job:claim" for button in buttons)


# ---------------------------------------------------------------------------
# /stats
# ---------------------------------------------------------------------------


async def test_stats_empty_ranking_sends_no_data_message() -> None:
    client = FakeTelegramBotClient()
    message = _make_message(text="/stats", chat_id=123, from_user={"id": 456})

    await handle_message(
        message,
        client=client,
        bot_token="tok",
        job_manager=FakeJobManager(ranking=[]),
        settings=FakeSettings(),
    )

    assert len(client.calls) == 1
    _token, _chat_id, text, _reply_markup = client.calls[0]
    assert text == "Chưa có dữ liệu thống kê"


async def test_stats_with_data_formats_correctly_and_preserves_order() -> None:
    client = FakeTelegramBotClient()
    message = _make_message(text="/stats", chat_id=123, from_user={"id": 456})
    ranking = [
        {
            "telegram_user_id": "111",
            "username": "alice",
            "first_name": "Alice",
            "success_count": 5,
            "fail_count": 1,
        },
        {
            "telegram_user_id": "222",
            "username": None,
            "first_name": "Bob",
            "success_count": 3,
            "fail_count": 0,
        },
        {
            "telegram_user_id": "333",
            "username": None,
            "first_name": None,
            "success_count": 1,
            "fail_count": 2,
        },
    ]

    await handle_message(
        message,
        client=client,
        bot_token="tok",
        job_manager=FakeJobManager(ranking=ranking),
        settings=FakeSettings(),
    )

    assert len(client.calls) == 1
    _token, _chat_id, text, _reply_markup = client.calls[0]
    lines = text.split("\n")

    # Dòng đầu là header, sau đó đúng thứ tự input (ranking đã sort sẵn,
    # commands.py KHÔNG tự sort lại).
    assert lines[0] == "📊 <b>Bảng xếp hạng</b>"
    assert lines[1] == "@alice: ✅5 ❌1 (tổng 6)"
    assert lines[2] == "Bob: ✅3 ❌0 (tổng 3)"
    assert lines[3] == "333: ✅1 ❌2 (tổng 3)"


# ---------------------------------------------------------------------------
# /reset
# ---------------------------------------------------------------------------


async def test_reset_sends_confirm_and_cancel_buttons_with_requester_id() -> None:
    client = FakeTelegramBotClient()
    message = _make_message(text="/reset", chat_id=123, from_user={"id": 456})

    await handle_message(
        message,
        client=client,
        bot_token="tok",
        job_manager=FakeJobManager(),
        settings=FakeSettings(),
    )

    assert len(client.calls) == 1
    _token, _chat_id, _text, reply_markup = client.calls[0]
    assert reply_markup is not None
    buttons = reply_markup["inline_keyboard"][0]
    assert len(buttons) == 2
    callback_data_set = {button["callback_data"] for button in buttons}
    assert callback_data_set == {
        "pull_job:reset_confirm:456",
        "pull_job:reset_cancel:456",
    }
