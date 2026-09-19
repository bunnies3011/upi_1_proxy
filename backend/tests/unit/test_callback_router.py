"""Unit test cho `callback_router.py` (task 31, spec telegram-pull-job-mode).

Cover (Requirement 18.1, 18.2, 18.3):
- `data` tiền tố `plus_check:` → chỉ answer (stub), KHÔNG gọi
  `pull_coordinator`.
- `data` tiền tố `pull_job:` → `pull_coordinator.handle_callback` được
  gọi với ĐÚNG dict `callback_query` nguyên vẹn.
- `data` không khớp tiền tố nào → fallback answer rỗng, KHÔNG gọi
  coordinator nào.

Dùng fake/spy `PullJobCoordinator` (KHÔNG cần real — router chỉ dispatch,
không đọc/ghi `JobManager` trực tiếp) + `FakeTelegramBotClient` duck-typed
ghi lại `answer_callback_query`.

**Validates: Requirements 18.1, 18.2, 18.3**
"""

from __future__ import annotations

from typing import Any

from app.notifiers.telegram.callback_router import handle_callback_query


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeTelegramBotClient:
    """Duck-typed fake — ghi lại mọi lời gọi `answer_callback_query`."""

    def __init__(self) -> None:
        self.answer_calls: list[dict[str, Any]] = []

    async def answer_callback_query(
        self,
        token: str,
        callback_query_id: str,
        text: str = "",
        *,
        show_alert: bool = False,
    ) -> dict:
        self.answer_calls.append(
            {
                "token": token,
                "callback_query_id": callback_query_id,
                "text": text,
                "show_alert": show_alert,
            }
        )
        return {}


class SpyPullCoordinator:
    """Spy — chỉ ghi lại `callback_query` dict truyền vào
    `handle_callback`, KHÔNG thực hiện logic gì (router không cần biết
    nội dung xử lý, chỉ cần dispatch đúng)."""

    def __init__(self) -> None:
        self.handle_callback_calls: list[dict] = []

    async def handle_callback(self, callback_query: dict) -> None:
        self.handle_callback_calls.append(callback_query)


def _cb(data: str, callback_id: str = "cb-1") -> dict:
    return {"id": callback_id, "data": data, "from": {"id": "user-1"}}


# ---------------------------------------------------------------------------
# plus_check: prefix — stub
# ---------------------------------------------------------------------------


async def test_plus_check_prefix_answers_and_does_not_call_pull_coordinator() -> None:
    client = FakeTelegramBotClient()
    pull_coordinator = SpyPullCoordinator()
    callback_query = _cb("plus_check:confirm:job-1")

    await handle_callback_query(
        callback_query,
        client=client,  # type: ignore[arg-type]
        bot_token="tok",
        job_manager=object(),  # type: ignore[arg-type]
        pull_coordinator=pull_coordinator,  # type: ignore[arg-type]
    )

    assert len(client.answer_calls) == 1
    assert client.answer_calls[0]["callback_query_id"] == "cb-1"
    assert pull_coordinator.handle_callback_calls == []


# ---------------------------------------------------------------------------
# pull_job: prefix — dispatch tới PullJobCoordinator
# ---------------------------------------------------------------------------


async def test_pull_job_prefix_dispatches_to_pull_coordinator_with_exact_dict() -> None:
    client = FakeTelegramBotClient()
    pull_coordinator = SpyPullCoordinator()
    callback_query = _cb("pull_job:claim")

    await handle_callback_query(
        callback_query,
        client=client,  # type: ignore[arg-type]
        bot_token="tok",
        job_manager=object(),  # type: ignore[arg-type]
        pull_coordinator=pull_coordinator,  # type: ignore[arg-type]
    )

    assert pull_coordinator.handle_callback_calls == [callback_query]
    # Router KHÔNG tự answer cho nhánh này — coordinator tự đảm nhiệm.
    assert client.answer_calls == []


# ---------------------------------------------------------------------------
# Unknown prefix — fallback
# ---------------------------------------------------------------------------


async def test_unknown_prefix_falls_back_to_empty_answer_no_coordinator_call() -> None:
    client = FakeTelegramBotClient()
    pull_coordinator = SpyPullCoordinator()
    callback_query = _cb("something_else:foo")

    await handle_callback_query(
        callback_query,
        client=client,  # type: ignore[arg-type]
        bot_token="tok",
        job_manager=object(),  # type: ignore[arg-type]
        pull_coordinator=pull_coordinator,  # type: ignore[arg-type]
    )

    assert len(client.answer_calls) == 1
    assert client.answer_calls[0]["text"] == ""
    assert client.answer_calls[0]["show_alert"] is False
    assert pull_coordinator.handle_callback_calls == []
