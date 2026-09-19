"""Unit test cho `TelegramBotClient` mở rộng Pull_Mode (task 21, spec
telegram-pull-job-mode).

Cover:
- `send_message` với `reply_markup=None` (default) → payload KHÔNG có key
  `reply_markup`; với markup truyền vào → payload CÓ đúng markup đó.
- `send_message_with_reply_to` raise `TelegramApiError` khi Telegram trả
  lỗi 400 "message to reply not found".
- `get_updates` parse đúng list `result`.
- `get_updates` propagate `TelegramApiError` với `status_code=409` khi
  Telegram trả Conflict (bot khác đang polling cùng token).
- `answer_callback_query`/`edit_message_caption`/`edit_message_reply_markup`
  build đúng payload.
- `_get_polling_client()` / `_get_sending_client()` trả 2 session KHÁC
  nhau, và mỗi loại là lazy singleton (gọi lại trả cùng object).
- `close_polling_session()`/`reopen_polling_session()` cho phép tạo lại
  polling session mới ở lần `get_updates` kế tiếp.

Dùng `FakeAsyncSession`/`FakeResponse` (`tests/support/fake_http.py`) —
pattern đã dùng cho `StripeClient`/`ChatgptClient` trong repo này — thay
vì tự tạo fake mới, monkey-patch `app.notifiers.telegram.client.http.
create_async_client` để trả về fake session theo thứ tự gọi (sending
trước, polling sau — theo lazy-init order trong `TelegramBotClient`).

**Validates: Requirements 19.1, 19.2, 19.3**
"""

from __future__ import annotations

from typing import Any

import pytest

from app.notifiers.telegram.client import (
    TelegramApiError,
    TelegramBotClient,
    _POLLING_TIMEOUT_SECONDS,
)
from tests.support.fake_http import Call, FakeAsyncSession, FakeResponse

_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
_CHAT_ID = "-1001234567890"


def _make_client_with_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TelegramBotClient, FakeAsyncSession, FakeAsyncSession]:
    """Tạo `TelegramBotClient` + monkeypatch factory để trả 2 fake session
    riêng biệt cho sending/polling.

    Session nào được `create_async_client()` tạo phụ thuộc kwarg
    `timeout=` — sending dùng `_DEFAULT_TIMEOUT_SECONDS` (15s), polling
    dùng `_POLLING_TIMEOUT_SECONDS` (30s). Match theo giá trị này (thay vì
    thứ tự gọi) để test có thể gọi `_get_polling_client()` hoặc
    `_get_sending_client()` theo BẤT KỲ thứ tự nào mà vẫn nhận đúng fake
    session tương ứng.

    Dùng fixture `monkeypatch` (auto-restore sau mỗi test) — patch tại
    nguồn `app.core.http_client.create_async_client` để cả `client.py`
    (dùng `http.create_async_client` qua alias `import ... as http`) đều
    thấy fake.

    Trả `(client, sending_session, polling_session)` — test set route lên
    session tương ứng trước khi gọi method cần fake.
    """
    sending_session = FakeAsyncSession()
    polling_session = FakeAsyncSession()

    def _fake_create_async_client(**kwargs: Any) -> FakeAsyncSession:
        if kwargs.get("timeout") == _POLLING_TIMEOUT_SECONDS:
            return polling_session
        return sending_session

    client = TelegramBotClient()
    monkeypatch.setattr(
        "app.core.http_client.create_async_client", _fake_create_async_client
    )
    return client, sending_session, polling_session


def _telegram_error_body(error_code: int, description: str) -> dict:
    return {"ok": False, "error_code": error_code, "description": description}


# ---------------------------------------------------------------------------
# send_message — reply_markup optional field
# ---------------------------------------------------------------------------


async def test_send_message_without_reply_markup_omits_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """`reply_markup=None` (default) → payload JSON KHÔNG có key `reply_markup`."""
    client, sending, _polling = _make_client_with_sessions(monkeypatch)
    sending.route(
        "POST",
        f"https://api.telegram.org/bot{_TOKEN}/sendMessage",
        FakeResponse(200, json_body={"ok": True, "result": {"message_id": 1}}),
    )

    await client.send_message(_TOKEN, _CHAT_ID, "hello")

    call: Call = sending.calls[0]
    assert "reply_markup" not in call.json_body


async def test_send_message_with_reply_markup_includes_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """Truyền `reply_markup` → payload JSON CÓ đúng markup đó."""
    client, sending, _polling = _make_client_with_sessions(monkeypatch)
    sending.route(
        "POST",
        f"https://api.telegram.org/bot{_TOKEN}/sendMessage",
        FakeResponse(200, json_body={"ok": True, "result": {"message_id": 1}}),
    )
    markup = {"inline_keyboard": [[{"text": "OK", "callback_data": "ok"}]]}

    await client.send_message(_TOKEN, _CHAT_ID, "hello", reply_markup=markup)

    call: Call = sending.calls[0]
    assert call.json_body["reply_markup"] == markup


# ---------------------------------------------------------------------------
# send_message_with_reply_to — TelegramApiError khi Telegram trả 400
# ---------------------------------------------------------------------------


async def test_send_message_with_reply_to_raises_on_message_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Telegram trả 400 "Bad Request: message to reply not found" →
    raise `TelegramApiError` với status_code/description đúng.
    """
    client, sending, _polling = _make_client_with_sessions(monkeypatch)
    sending.route(
        "POST",
        f"https://api.telegram.org/bot{_TOKEN}/sendMessage",
        FakeResponse(
            400,
            json_body=_telegram_error_body(
                400, "Bad Request: message to reply not found"
            ),
        ),
    )

    with pytest.raises(TelegramApiError) as exc_info:
        await client.send_message_with_reply_to(
            _TOKEN, _CHAT_ID, "hello", reply_to_message_id=999
        )

    err = exc_info.value
    assert err.status_code == 400
    assert err.error_code == 400
    assert err.description == "Bad Request: message to reply not found"


# ---------------------------------------------------------------------------
# get_updates — parse list + 409 Conflict
# ---------------------------------------------------------------------------


async def test_get_updates_parses_result_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """`get_updates` trả đúng list dict từ `result` — dùng polling session."""
    client, _sending, polling = _make_client_with_sessions(monkeypatch)
    updates = [
        {"update_id": 1, "message": {"text": "a"}},
        {"update_id": 2, "message": {"text": "b"}},
    ]
    polling.route(
        "POST",
        f"https://api.telegram.org/bot{_TOKEN}/getUpdates",
        FakeResponse(200, json_body={"ok": True, "result": updates}),
    )

    result = await client.get_updates(_TOKEN, offset=1)

    assert result == updates
    # sending session không được dùng cho get_updates.
    assert len(_sending.calls) == 0


async def test_get_updates_raises_conflict_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """Telegram trả 409 Conflict (instance khác đang polling cùng token)
    → `TelegramApiError` với `status_code=409` để `PollingSupervisor`
    phát hiện.
    """
    client, _sending, polling = _make_client_with_sessions(monkeypatch)
    polling.route(
        "POST",
        f"https://api.telegram.org/bot{_TOKEN}/getUpdates",
        FakeResponse(
            409,
            json_body=_telegram_error_body(409, "Conflict: terminated by other getUpdates request"),
        ),
    )

    with pytest.raises(TelegramApiError) as exc_info:
        await client.get_updates(_TOKEN, offset=1)

    assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# answer_callback_query / edit_message_caption / edit_message_reply_markup
# ---------------------------------------------------------------------------


async def test_answer_callback_query_builds_correct_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    client, sending, _polling = _make_client_with_sessions(monkeypatch)
    sending.route(
        "POST",
        f"https://api.telegram.org/bot{_TOKEN}/answerCallbackQuery",
        FakeResponse(200, json_body={"ok": True, "result": True}),
    )

    await client.answer_callback_query(
        _TOKEN, "cb-123", "Đã xử lý", show_alert=True
    )

    call: Call = sending.calls[0]
    assert call.json_body == {
        "callback_query_id": "cb-123",
        "text": "Đã xử lý",
        "show_alert": True,
    }


async def test_edit_message_caption_builds_correct_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    client, sending, _polling = _make_client_with_sessions(monkeypatch)
    sending.route(
        "POST",
        f"https://api.telegram.org/bot{_TOKEN}/editMessageCaption",
        FakeResponse(200, json_body={"ok": True, "result": {"message_id": 42}}),
    )

    await client.edit_message_caption(_TOKEN, _CHAT_ID, 42, "Caption mới")

    call: Call = sending.calls[0]
    assert call.json_body == {
        "chat_id": _CHAT_ID,
        "message_id": 42,
        "caption": "Caption mới",
        "parse_mode": "HTML",
    }


async def test_edit_message_reply_markup_with_markup_includes_field(monkeypatch: pytest.MonkeyPatch) -> None:
    client, sending, _polling = _make_client_with_sessions(monkeypatch)
    sending.route(
        "POST",
        f"https://api.telegram.org/bot{_TOKEN}/editMessageReplyMarkup",
        FakeResponse(200, json_body={"ok": True, "result": {"message_id": 42}}),
    )
    markup = {"inline_keyboard": [[{"text": "Done", "callback_data": "done"}]]}

    await client.edit_message_reply_markup(_TOKEN, _CHAT_ID, 42, reply_markup=markup)

    call: Call = sending.calls[0]
    assert call.json_body == {
        "chat_id": _CHAT_ID,
        "message_id": 42,
        "reply_markup": markup,
    }


async def test_edit_message_reply_markup_without_markup_omits_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """`reply_markup=None` (default) → payload KHÔNG có key `reply_markup`
    — Telegram hiểu là xoá toàn bộ inline keyboard.
    """
    client, sending, _polling = _make_client_with_sessions(monkeypatch)
    sending.route(
        "POST",
        f"https://api.telegram.org/bot{_TOKEN}/editMessageReplyMarkup",
        FakeResponse(200, json_body={"ok": True, "result": {"message_id": 42}}),
    )

    await client.edit_message_reply_markup(_TOKEN, _CHAT_ID, 42)

    call: Call = sending.calls[0]
    assert "reply_markup" not in call.json_body
    assert call.json_body == {"chat_id": _CHAT_ID, "message_id": 42}


# ---------------------------------------------------------------------------
# 2 session riêng biệt — lazy singleton per session type
# ---------------------------------------------------------------------------


def test_sending_and_polling_clients_are_different_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    client, sending, polling = _make_client_with_sessions(monkeypatch)

    got_sending = client._get_sending_client()
    got_polling = client._get_polling_client()

    assert got_sending is sending
    assert got_polling is polling
    assert got_sending is not got_polling


def test_sending_client_is_lazy_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    client, sending, _polling = _make_client_with_sessions(monkeypatch)

    first = client._get_sending_client()
    second = client._get_sending_client()

    assert first is second is sending


def test_polling_client_is_lazy_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _sending, polling = _make_client_with_sessions(monkeypatch)

    first = client._get_polling_client()
    second = client._get_polling_client()

    assert first is second is polling


# ---------------------------------------------------------------------------
# close_polling_session / reopen_polling_session
# ---------------------------------------------------------------------------


async def test_close_polling_session_sets_client_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _sending, polling = _make_client_with_sessions(monkeypatch)
    # Force lazy-init polling client trước.
    client._get_polling_client()
    assert client._polling_client is polling

    await client.close_polling_session()

    assert client._polling_client is None
    assert polling.closed is True


async def test_reopen_polling_session_allows_fresh_client_on_next_get_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sau `reopen_polling_session()`, lần gọi `get_updates()` kế tiếp lazy
    tạo session polling MỚI (không phải session cũ đã đóng).
    """
    client, _sending, old_polling = _make_client_with_sessions(monkeypatch)
    client._get_polling_client()
    assert client._polling_client is old_polling

    await client.reopen_polling_session()
    assert client._polling_client is None
    assert old_polling.closed is True

    # Monkeypatch factory để trả 1 session polling MỚI cho lần gọi kế tiếp.
    new_polling = FakeAsyncSession()
    new_polling.route(
        "POST",
        f"https://api.telegram.org/bot{_TOKEN}/getUpdates",
        FakeResponse(200, json_body={"ok": True, "result": []}),
    )
    monkeypatch.setattr(
        "app.core.http_client.create_async_client", lambda **kw: new_polling
    )

    result = await client.get_updates(_TOKEN, offset=1)

    assert result == []
    assert client._polling_client is new_polling
    assert client._polling_client is not old_polling
