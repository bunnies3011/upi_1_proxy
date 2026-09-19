"""Unit test cho `PullJobCoordinator` (task 31, spec telegram-pull-job-mode).

Cover (Requirement 5.2-5.7, 9.1-9.4, 10.1-10.8, 11.1-11.5, 12.1-12.7,
17.2-17.6, 22.11):
- `_handle_claim`: chặn theo `allowed_chat_ids` (im lặng), sai mode, claim
  thành công, pool rỗng, vượt limit.
- `_handle_done`: job không tồn tại, sai ownership, đã terminal (bấm lại),
  full success (`plan=="plus"` lần đầu), retry (`plan!=plus` lần 1), hết
  lượt (`plan!=plus` lần 2), exception trong `check_plan_status` (cả 2
  nhánh recovery: attempts==1 và attempts==2).
- `_handle_fail`: bình thường từ armed, override khi đang checking, đã
  terminal rồi bấm lại (không áp dụng lại, call count không đổi).
- `_handle_reset_confirm`/`_handle_reset_cancel`: đúng người (có/không
  reset), sai người (từ chối, không đổi state), bấm lại sau khi đã xử lý.
- Race THẬT cho R22.11: `_handle_done` (delay qua `asyncio.sleep` trong
  `check_plan_status`) chạy đồng thời với `_handle_fail` cho CÙNG job qua
  `asyncio.gather` — chỉ đúng 1 trong 2 kết quả được ghi vào `pull_outcome`.

Dùng REAL `JobManager` (factory cục bộ tương tự `_make_pull_manager` ở
`test_job_manager_pull_mode.py`) + `FakeTelegramBotClient` duck-typed +
`FakeCheckPlanHandler` (mở rộng `FakeHandler` với `check_plan_status`
cấu hình được: canned value, callable, exception, hoặc delay).

**Validates: Requirements 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 9.1, 9.2, 9.3, 9.4,
10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 11.1, 11.2, 11.3, 11.4, 11.5,
12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 12.7, 17.2, 17.3, 17.4, 17.5, 17.6, 22.11**
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from app.core.job_manager import JobManager, PlusCheckState, PullOutcome
from app.core.payment_flow import JobResult, JobStatus
from app.notifiers.telegram.pull_mode import PullJobCoordinator

from tests.unit.test_job_manager import FakeHandler, FakeSse
from tests.unit.test_job_manager_pull_mode import (
    FakeJobRepo as _BaseFakeJobRepo,
    _PullModeFakeProxyPool,
    _PullModeFakeSettings,
    _seed_accounts,
)


class FakeJobRepo(_BaseFakeJobRepo):
    """`FakeJobRepo` (từ `test_job_manager_pull_mode.py`) chỉ implement
    `upsert_worker_stat_delta`/`upsert` — `_handle_reset_confirm` gọi
    thêm `job_manager.reset_all_worker_stats()` → `job_repo.reset_worker_stats()`.
    Thêm no-op tracked để test reset xác nhận được gọi."""

    def __init__(self) -> None:
        super().__init__()
        self.reset_called = 0

    async def reset_worker_stats(self) -> None:
        self.reset_called += 1


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeCheckPlanHandler(FakeHandler):
    """`FakeHandler` (từ `test_job_manager.py`) mở rộng thêm
    `check_plan_status` cấu hình được — dùng cho `_handle_done`/race
    R22.11. Ưu tiên `check_plan_fn` (callable async) > `check_plan_exc`
    (raise) > `check_plan_result` (trả canned dict). `check_plan_delay`
    (giây) luôn `await asyncio.sleep()` TRƯỚC khi quyết định theo 3 nhánh
    trên — dùng để dựng race thật (R22.11).
    """

    def __init__(
        self,
        *args: Any,
        check_plan_result: dict | None = None,
        check_plan_exc: Exception | None = None,
        check_plan_delay: float = 0.0,
        check_plan_fn: Callable[[Any], Awaitable[dict | None]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.check_plan_result = check_plan_result
        self.check_plan_exc = check_plan_exc
        self.check_plan_delay = check_plan_delay
        self.check_plan_fn = check_plan_fn
        self.check_plan_calls: list[str] = []

    async def check_plan_status(self, job: Any) -> dict | None:
        self.check_plan_calls.append(job.job_id)
        if self.check_plan_delay > 0:
            await asyncio.sleep(self.check_plan_delay)
        if self.check_plan_fn is not None:
            return await self.check_plan_fn(job)
        if self.check_plan_exc is not None:
            raise self.check_plan_exc
        return self.check_plan_result


class FakeTelegramBotClient:
    """Duck-typed fake `TelegramBotClient` — ghi lại mọi lời gọi
    `answer_callback_query`/`edit_message_reply_markup`/`send_message`,
    hỗ trợ raise lỗi cấu hình được (KHÔNG dùng trong test coordinator vì
    mọi call này đều best-effort swallow bên trong `pull_mode.py`, nhưng
    giữ hook sẵn cho robustness nếu cần mở rộng).
    """

    def __init__(self) -> None:
        self.answer_calls: list[dict[str, Any]] = []
        self.edit_markup_calls: list[dict[str, Any]] = []
        self.send_message_calls: list[dict[str, Any]] = []
        # Task 8 (UX refactor): status card gọi edit_message_text để cập
        # nhật in-place; send_message trả message_id để tracker lưu lại
        # cho lần edit sau. Fake trả sẵn 1 counter tăng dần.
        self.edit_text_calls: list[dict[str, Any]] = []
        self._next_message_id = 1000

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

    async def edit_message_reply_markup(
        self,
        token: str,
        chat_id: str,
        message_id: int,
        *,
        reply_markup: dict | None = None,
    ) -> dict:
        self.edit_markup_calls.append(
            {
                "token": token,
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": reply_markup,
            }
        )
        return {}

    async def send_message(
        self,
        token: str,
        chat_id: str,
        text: str,
        *,
        reply_markup: dict | None = None,
    ) -> dict:
        message_id = self._next_message_id
        self._next_message_id += 1
        self.send_message_calls.append(
            {
                "token": token,
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup,
                "message_id": message_id,
            }
        )
        return {"message_id": message_id}

    async def edit_message_text(
        self,
        token: str,
        chat_id: str,
        message_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
    ) -> dict:
        self.edit_text_calls.append(
            {
                "token": token,
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )
        return {"message_id": message_id}


async def _bot_token_getter() -> str:
    return "test-token"


def _make_manager_and_settings(
    *,
    mode: str = "pull",
    max_concurrent: int = 10,
    allowed_chat_ids: list[str] | None = None,
) -> tuple[JobManager, _PullModeFakeSettings]:
    settings = _PullModeFakeSettings(
        snapshot={
            "telegram.mode": mode,
            "telegram.pull_mode.max_concurrent_jobs_per_user": max_concurrent,
            "telegram.pull_mode.allowed_chat_ids": allowed_chat_ids or [],
        }
    )
    proxy_pool = _PullModeFakeProxyPool()
    sse = FakeSse()
    manager = JobManager(settings=settings, proxy_pool=proxy_pool, sse=sse)  # type: ignore[arg-type]
    return manager, settings


def _make_coordinator(
    manager: JobManager, settings: _PullModeFakeSettings
) -> tuple[PullJobCoordinator, FakeTelegramBotClient]:
    from app.notifiers.telegram.status_card import WorkerStatusCardTracker

    client = FakeTelegramBotClient()
    status_card = WorkerStatusCardTracker(client=client)  # type: ignore[arg-type]
    coordinator = PullJobCoordinator(
        manager, client, settings, _bot_token_getter, status_card  # type: ignore[arg-type]
    )
    return coordinator, client


def _claim_cb(
    *,
    callback_id: str = "cb-claim",
    chat_id: str = "chat-1",
    user_id: str = "worker-1",
    username: str | None = None,
    first_name: str | None = None,
) -> dict:
    return {
        "id": callback_id,
        "data": "pull_job:claim",
        "message": {"chat": {"id": chat_id}, "message_id": 1},
        "from": {"id": user_id, "username": username, "first_name": first_name},
    }


def _done_cb(job_id: str, *, callback_id: str = "cb-done", user_id: str = "worker-1") -> dict:
    return {
        "id": callback_id,
        "data": f"pull_job:done:{job_id}",
        "from": {"id": user_id},
    }


def _fail_cb(job_id: str, *, callback_id: str = "cb-fail", user_id: str = "worker-1") -> dict:
    return {
        "id": callback_id,
        "data": f"pull_job:fail:{job_id}",
        "from": {"id": user_id},
    }


def _reset_cb(
    requester_id: str,
    *,
    action: str = "confirm",
    callback_id: str = "cb-reset",
    user_id: str | None = None,
    chat_id: str = "chat-1",
    message_id: int = 1,
) -> dict:
    return {
        "id": callback_id,
        "data": f"pull_job:reset_{action}:{requester_id}",
        "from": {"id": user_id if user_id is not None else requester_id},
        "message": {"chat": {"id": chat_id}, "message_id": message_id},
    }


async def _setup(
    *,
    mode: str = "pull",
    max_concurrent: int = 10,
    allowed_chat_ids: list[str] | None = None,
    check_plan_result: dict | None = None,
    check_plan_exc: Exception | None = None,
    check_plan_delay: float = 0.0,
    check_plan_fn: Callable[[Any], Awaitable[dict | None]] | None = None,
    n_accounts: int = 0,
) -> tuple[JobManager, _PullModeFakeSettings, PullJobCoordinator, FakeTelegramBotClient, FakeCheckPlanHandler]:
    manager, settings = _make_manager_and_settings(
        mode=mode, max_concurrent=max_concurrent, allowed_chat_ids=allowed_chat_ids
    )
    handler = FakeCheckPlanHandler(
        run_result=JobResult(status=JobStatus.QR_READY),
        check_plan_result=check_plan_result,
        check_plan_exc=check_plan_exc,
        check_plan_delay=check_plan_delay,
        check_plan_fn=check_plan_fn,
    )
    manager.register_handler("ideal", handler)
    if n_accounts:
        await _seed_accounts(manager, n_accounts)
    coordinator, client = _make_coordinator(manager, settings)
    return manager, settings, coordinator, client, handler


# ---------------------------------------------------------------------------
# _handle_claim — Requirement 5
# ---------------------------------------------------------------------------


async def test_handle_claim_blocked_by_allowed_chat_ids_is_silent() -> None:
    manager, settings, coordinator, client, _handler = await _setup(
        allowed_chat_ids=["other-chat"], n_accounts=1
    )

    await coordinator.handle_callback(_claim_cb(chat_id="chat-1"))

    assert len(client.answer_calls) == 1
    call = client.answer_calls[0]
    assert call["text"] == ""
    assert call["show_alert"] is False
    # Account KHÔNG bị claim.
    accounts = [r for r in manager.list_jobs()]
    assert all(r.pull_assignment_state.value == "unassigned" for r in accounts)


async def test_handle_claim_wrong_mode_rejects() -> None:
    # `_seed_accounts` gọi `submit_batch` đọc LIVE `telegram.mode` — seed
    # ở mode "pull" TRƯỚC rồi đổi settings sang "push" để account vẫn nằm
    # trong Pull_Account_Pool (UNASSIGNED) khi coordinator check mode.
    manager, settings, coordinator, client, _handler = await _setup(
        mode="pull", n_accounts=1
    )
    settings._snapshot["telegram.mode"] = "push"  # noqa: SLF001

    await coordinator.handle_callback(_claim_cb())

    assert len(client.answer_calls) == 1
    call = client.answer_calls[0]
    assert call["text"] == "Tool hiện không ở chế độ nhận job"
    assert call["show_alert"] is True
    accounts = manager.list_jobs()
    assert all(r.pull_assignment_state.value == "unassigned" for r in accounts)


async def test_handle_claim_success() -> None:
    manager, settings, coordinator, client, _handler = await _setup(
        n_accounts=1, max_concurrent=1
    )

    await coordinator.handle_callback(
        _claim_cb(user_id="worker-1", username="alice", first_name="Alice")
    )

    call = client.answer_calls[0]
    assert call["text"] == "Đã nhận 1 job, đang xử lý..."
    assert call["show_alert"] is False
    record = next(iter(manager.list_jobs()))
    assert record.pull_assignment_state.value == "assigned"
    assert record.pull_assigned_telegram_user_id == "worker-1"


async def test_handle_claim_pool_empty_message() -> None:
    manager, settings, coordinator, client, _handler = await _setup(n_accounts=0)

    await coordinator.handle_callback(_claim_cb())

    call = client.answer_calls[0]
    assert call["text"] == "Hiện không có job nào khả dụng, thử lại sau."
    assert call["show_alert"] is True


async def test_handle_claim_holding_batch_rejects_new_claim() -> None:
    """Model batch: worker còn job batch trước chưa xử lý hết (active >
    0) → claim mới bị từ chối với status "holding", KHÔNG đụng session
    card cũ."""
    manager, settings, coordinator, client, _handler = await _setup(
        max_concurrent=1, n_accounts=2
    )

    await coordinator.handle_callback(_claim_cb(callback_id="cb-1"))
    client.answer_calls.clear()
    await coordinator.handle_callback(_claim_cb(callback_id="cb-2"))

    call = client.answer_calls[0]
    assert call["text"] == (
        "Bạn còn job chưa xử lý hết, hãy hoàn tất trước khi nhận batch mới."
    )
    assert call["show_alert"] is True


# ---------------------------------------------------------------------------
# _handle_done — Requirement 9, 10, 11
# ---------------------------------------------------------------------------


async def test_handle_done_job_not_found() -> None:
    _manager, _settings, coordinator, client, _handler = await _setup()

    await coordinator.handle_callback(_done_cb("nonexistent-job"))

    call = client.answer_calls[0]
    assert call["text"] == "Job không còn tồn tại"
    assert call["show_alert"] is True


async def test_handle_done_ownership_mismatch() -> None:
    manager, _settings, coordinator, client, _handler = await _setup(n_accounts=1)
    job_id = await manager.claim_pull_account("worker-1", "chat-1", None, None)
    assert job_id is not None

    await coordinator.handle_callback(_done_cb(job_id, user_id="worker-2"))

    call = client.answer_calls[0]
    assert call["text"] == "Đây không phải job của bạn"
    assert call["show_alert"] is True
    record = manager.get_job(job_id)
    assert record is not None
    assert record.pull_outcome == PullOutcome.PENDING


async def test_handle_done_already_terminal_after_fail_reports_result_exists() -> None:
    manager, _settings, coordinator, client, _handler = await _setup(n_accounts=1)
    job_id = await manager.claim_pull_account("worker-1", "chat-1", None, None)
    assert job_id is not None

    await coordinator.handle_callback(_fail_cb(job_id, user_id="worker-1"))
    client.answer_calls.clear()

    await coordinator.handle_callback(_done_cb(job_id, user_id="worker-1"))

    call = client.answer_calls[0]
    assert call["text"] == "Job này đã có kết quả"
    assert call["show_alert"] is True
    record = manager.get_job(job_id)
    assert record is not None
    assert record.pull_outcome == PullOutcome.FAIL


async def test_result_message_claim_again_button_only_on_last_job_in_batch() -> None:
    """Model batch (N=2): resolve QR ĐẦU tiên trong batch — worker vẫn còn
    1 job ASSIGNED+PENDING khác → message kết quả KHÔNG kèm nút "Nhận job
    tiếp" (tránh worker bấm claim mới khi còn QR chưa xử lý, bị
    `claim_pull_batch` chặn với status "holding" — spam/rối UX). Resolve
    QR CUỐI (worker hết active) → message kết quả CÓ kèm nút.
    """
    manager, _settings, coordinator, client, _handler = await _setup(
        check_plan_result={"plan": "plus"}, n_accounts=2, max_concurrent=2
    )
    job_a = await manager.claim_pull_account("worker-1", "chat-1", None, None)
    job_b = await manager.claim_pull_account("worker-1", "chat-1", None, None)
    assert job_a is not None and job_b is not None
    await manager.initialize_pull_verify_state(job_a, "chat-1", 101)
    await manager.initialize_pull_verify_state(job_b, "chat-1", 102)

    # Resolve job_a trước — worker vẫn còn job_b PENDING.
    await coordinator.handle_callback(_done_cb(job_a, user_id="worker-1"))
    first_result = client.send_message_calls[-1]
    assert first_result["reply_markup"] is None

    # Resolve job_b (job cuối) — worker hết active → nút xuất hiện.
    await coordinator.handle_callback(_done_cb(job_b, user_id="worker-1"))
    second_result = client.send_message_calls[-1]
    assert second_result["reply_markup"] is not None
    assert (
        second_result["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        == "pull_job:claim"
    )


async def test_handle_done_full_success_path_plan_plus_first_check() -> None:
    manager, _settings, coordinator, client, handler = await _setup(
        check_plan_result={"plan": "plus"}, n_accounts=1
    )
    job_id = await manager.claim_pull_account("worker-1", "chat-1", None, None)
    assert job_id is not None
    await manager.initialize_pull_verify_state(job_id, "chat-1", 42)

    await coordinator.handle_callback(_done_cb(job_id, user_id="worker-1"))

    record = manager.get_job(job_id)
    assert record is not None
    assert record.plus_check_state == PlusCheckState.VERIFIED
    assert record.plus_check_attempts == 1
    assert record.pull_outcome == PullOutcome.SUCCESS
    assert handler.check_plan_calls == [job_id]
    # Gỡ nút message QR + gửi message xác nhận tới origin_chat_id.
    assert len(client.edit_markup_calls) == 1
    assert client.edit_markup_calls[0]["chat_id"] == "chat-1"
    assert client.edit_markup_calls[0]["message_id"] == 42
    assert len(client.send_message_calls) == 1
    assert "thành công" in client.send_message_calls[0]["text"]
    call = client.answer_calls[-1]
    assert call["text"] == "Đã xác nhận thành công!"
    assert call["show_alert"] is False


async def test_handle_done_retry_path_plan_not_plus_first_attempt_back_to_armed() -> None:
    manager, _settings, coordinator, client, handler = await _setup(
        check_plan_result={"plan": "free"}, n_accounts=1
    )
    job_id = await manager.claim_pull_account("worker-1", "chat-1", None, None)
    assert job_id is not None

    await coordinator.handle_callback(_done_cb(job_id, user_id="worker-1"))

    record = manager.get_job(job_id)
    assert record is not None
    assert record.plus_check_state == PlusCheckState.ARMED
    assert record.plus_check_attempts == 1
    assert record.pull_outcome == PullOutcome.PENDING
    call = client.answer_calls[-1]
    assert call["text"] == "Chưa lên Plus, thử lại"
    assert call["show_alert"] is True


async def test_handle_done_exhausted_path_plan_not_plus_second_attempt_fail() -> None:
    manager, _settings, coordinator, client, handler = await _setup(
        check_plan_result={"plan": "free"}, n_accounts=1
    )
    job_id = await manager.claim_pull_account("worker-1", "chat-1", None, None)
    assert job_id is not None

    await coordinator.handle_callback(_done_cb(job_id, user_id="worker-1", callback_id="cb-1"))
    await coordinator.handle_callback(_done_cb(job_id, user_id="worker-1", callback_id="cb-2"))

    record = manager.get_job(job_id)
    assert record is not None
    assert record.plus_check_state == PlusCheckState.EXHAUSTED_2
    assert record.plus_check_attempts == 2
    assert record.pull_outcome == PullOutcome.FAIL
    assert handler.check_plan_calls == [job_id, job_id]
    call = client.answer_calls[-1]
    assert call["text"] == "Đã ghi nhận thất bại."
    assert call["show_alert"] is False


async def test_handle_done_exception_in_check_plan_status_recovers_to_armed() -> None:
    manager, _settings, coordinator, client, handler = await _setup(
        check_plan_exc=RuntimeError("boom"), n_accounts=1
    )
    job_id = await manager.claim_pull_account("worker-1", "chat-1", None, None)
    assert job_id is not None

    await coordinator.handle_callback(_done_cb(job_id, user_id="worker-1"))

    record = manager.get_job(job_id)
    assert record is not None
    assert record.plus_check_state == PlusCheckState.ARMED
    assert record.plus_check_attempts == 1
    assert record.pull_outcome == PullOutcome.PENDING
    call = client.answer_calls[-1]
    assert "lỗi" in call["text"].lower()
    assert call["show_alert"] is True


async def test_handle_done_exception_on_second_attempt_recovers_to_exhausted_fail() -> None:
    manager, _settings, coordinator, client, handler = await _setup(
        check_plan_result={"plan": "free"}, n_accounts=1
    )
    job_id = await manager.claim_pull_account("worker-1", "chat-1", None, None)
    assert job_id is not None

    # Lần 1: plan != plus -> về armed, attempts=1 (không lỗi).
    await coordinator.handle_callback(_done_cb(job_id, user_id="worker-1", callback_id="cb-1"))
    # Lần 2: đổi handler sang raise exception thay vì trả canned value.
    handler.check_plan_result = None
    handler.check_plan_exc = RuntimeError("boom-2")

    await coordinator.handle_callback(_done_cb(job_id, user_id="worker-1", callback_id="cb-2"))

    record = manager.get_job(job_id)
    assert record is not None
    assert record.plus_check_state == PlusCheckState.EXHAUSTED_2
    assert record.pull_outcome == PullOutcome.FAIL


# ---------------------------------------------------------------------------
# _handle_fail — Requirement 9, 12
# ---------------------------------------------------------------------------


async def test_handle_fail_normal_path_from_armed() -> None:
    manager, _settings, coordinator, client, _handler = await _setup(n_accounts=1)
    job_id = await manager.claim_pull_account("worker-1", "chat-1", None, None)
    assert job_id is not None

    await coordinator.handle_callback(_fail_cb(job_id, user_id="worker-1"))

    record = manager.get_job(job_id)
    assert record is not None
    assert record.plus_check_state == PlusCheckState.FAILED_MARKED
    assert record.pull_outcome == PullOutcome.FAIL
    assert len(client.send_message_calls) == 1
    assert "thất bại" in client.send_message_calls[0]["text"]
    call = client.answer_calls[-1]
    assert call["text"] == "Đã ghi nhận thất bại."
    assert call["show_alert"] is False


async def test_handle_fail_override_while_checking() -> None:
    manager, _settings, coordinator, client, _handler = await _setup(n_accounts=1)
    job_id = await manager.claim_pull_account("worker-1", "chat-1", None, None)
    assert job_id is not None
    # Đưa job vào "checking" trực tiếp qua job_manager (giả lập đang chờ
    # check_plan_status của 1 lượt bấm Hoàn_Thành trước đó).
    transitioned = await manager.record_plus_check_transition(
        job_id, "armed", "checking", 0
    )
    assert transitioned is True

    await coordinator.handle_callback(_fail_cb(job_id, user_id="worker-1"))

    record = manager.get_job(job_id)
    assert record is not None
    assert record.plus_check_state == PlusCheckState.FAILED_MARKED
    assert record.pull_outcome == PullOutcome.FAIL


async def test_handle_fail_clicked_again_after_terminal_no_reapplication() -> None:
    manager, _settings, coordinator, client, _handler = await _setup(n_accounts=1)
    job_id = await manager.claim_pull_account("worker-1", "chat-1", None, None)
    assert job_id is not None

    await coordinator.handle_callback(_fail_cb(job_id, user_id="worker-1", callback_id="cb-1"))
    send_message_count_after_first = len(client.send_message_calls)

    await coordinator.handle_callback(_fail_cb(job_id, user_id="worker-1", callback_id="cb-2"))

    assert len(client.send_message_calls) == send_message_count_after_first
    call = client.answer_calls[-1]
    assert call["text"] == "Job này đã có kết quả"
    assert call["show_alert"] is True


# ---------------------------------------------------------------------------
# _handle_reset_confirm / _handle_reset_cancel — Requirement 17
# ---------------------------------------------------------------------------


async def test_reset_confirm_correct_requester_resets_stats() -> None:
    manager, settings, coordinator, client, _handler = await _setup()
    spy_repo = FakeJobRepo()
    manager._job_repo = spy_repo  # noqa: SLF001 — pattern đã dùng ở test khác trong suite

    await coordinator.handle_callback(_reset_cb("456", action="confirm"))

    assert spy_repo.reset_called == 1
    call = client.answer_calls[-1]
    assert call["show_alert"] is False
    assert len(client.edit_markup_calls) == 1
    assert len(client.send_message_calls) == 1
    assert client.send_message_calls[0]["text"] == "✅ Đã reset toàn bộ thống kê"


async def test_reset_cancel_correct_requester_does_not_reset_stats() -> None:
    manager, settings, coordinator, client, _handler = await _setup()

    calls: list[str] = []
    original_reset = manager.reset_all_worker_stats

    async def _tracked_reset() -> None:
        calls.append("reset")
        await original_reset()

    manager.reset_all_worker_stats = _tracked_reset  # type: ignore[method-assign]

    await coordinator.handle_callback(_reset_cb("456", action="cancel"))

    assert calls == []
    assert len(client.edit_markup_calls) == 1
    assert len(client.send_message_calls) == 1
    assert client.send_message_calls[0]["text"] == "Đã hủy reset"


async def test_reset_confirm_wrong_requester_denied_no_state_change() -> None:
    manager, settings, coordinator, client, _handler = await _setup()

    calls: list[str] = []
    original_reset = manager.reset_all_worker_stats

    async def _tracked_reset() -> None:
        calls.append("reset")
        await original_reset()

    manager.reset_all_worker_stats = _tracked_reset  # type: ignore[method-assign]

    await coordinator.handle_callback(
        _reset_cb("456", action="confirm", user_id="999")
    )

    assert calls == []
    assert client.edit_markup_calls == []
    assert client.send_message_calls == []
    call = client.answer_calls[-1]
    assert call["show_alert"] is True


async def test_reset_cancel_wrong_requester_denied_no_state_change() -> None:
    _manager, _settings, coordinator, client, _handler = await _setup()

    await coordinator.handle_callback(
        _reset_cb("456", action="cancel", user_id="999")
    )

    assert client.edit_markup_calls == []
    assert client.send_message_calls == []
    call = client.answer_calls[-1]
    assert call["show_alert"] is True


async def test_reset_confirm_clicked_again_after_resolved_reports_already_processed() -> None:
    manager, settings, coordinator, client, _handler = await _setup()
    spy_repo = FakeJobRepo()
    manager._job_repo = spy_repo  # noqa: SLF001

    await coordinator.handle_callback(
        _reset_cb("456", action="confirm", callback_id="cb-1")
    )
    send_message_count_after_first = len(client.send_message_calls)

    await coordinator.handle_callback(
        _reset_cb("456", action="confirm", callback_id="cb-2")
    )

    assert len(client.send_message_calls) == send_message_count_after_first
    call = client.answer_calls[-1]
    assert call["text"] == "Yêu cầu reset này đã được xử lý"
    assert call["show_alert"] is True


# ---------------------------------------------------------------------------
# Race THẬT — Requirement 22.11
# ---------------------------------------------------------------------------


async def test_race_done_vs_fail_only_one_outcome_wins_r22_11() -> None:
    """Dựng job đang chờ `check_plan_status` (delay qua `asyncio.sleep`,
    sẽ trả `plan == "plus"`), rồi `asyncio.gather` đồng thời giữa (a)
    `_handle_done` (qua `handle_callback`) và (b) `_handle_fail` cho CÙNG
    job — chỉ đúng 1 trong 2 kết quả (SUCCESS/FAIL) được ghi vào
    `pull_outcome`, KHÔNG có cả `success_count` và `fail_count` cùng tăng.
    """
    manager, settings, coordinator, client, handler = await _setup(
        check_plan_result={"plan": "plus"},
        check_plan_delay=0.05,
        n_accounts=1,
    )
    fake_repo = FakeJobRepo()
    manager._job_repo = fake_repo  # noqa: SLF001

    job_id = await manager.claim_pull_account("worker-1", "chat-1", None, None)
    assert job_id is not None

    await asyncio.gather(
        coordinator.handle_callback(_done_cb(job_id, user_id="worker-1", callback_id="cb-done")),
        coordinator.handle_callback(_fail_cb(job_id, user_id="worker-1", callback_id="cb-fail")),
    )

    record = manager.get_job(job_id)
    assert record is not None
    # Đúng 1 trong 2 outcome — KHÔNG kẹt PENDING, KHÔNG raise exception nào
    # thoát ra khỏi coordinator (nếu có exception, test đã fail ở await trên).
    assert record.pull_outcome in (PullOutcome.SUCCESS, PullOutcome.FAIL)

    success_increments = sum(1 for call in fake_repo.calls if call[1] == 1)
    fail_increments = sum(1 for call in fake_repo.calls if call[2] == 1)
    # Tổng cộng ĐÚNG 1 lần tính công cho job này — không double-count.
    assert success_increments + fail_increments == 1
    assert not (success_increments == 1 and fail_increments == 1)


# ---------------------------------------------------------------------------
# handle_pull_error — model batch (retry account lỗi chuyển hẳn xuống
# `JobManager._maybe_auto_retry`; coordinator CHỈ ghi nhận 1 job đã lỗi
# hẳn vào status card, KHÔNG tự claim job khác).
# ---------------------------------------------------------------------------


async def test_handle_pull_error_records_job_failed_on_status_card() -> None:
    """Job trong batch chuyển ERROR (đã hết lượt auto-retry ở tầng
    JobManager) → coordinator CHỈ gọi `status_card.record_job_failed`,
    KHÔNG tự claim job khác, KHÔNG gửi sendMessage text lỗi kỹ thuật riêng.
    """
    manager, settings, coordinator, client, _handler = await _setup(
        n_accounts=2, max_concurrent=1
    )

    # Worker bấm "Nhận job" — claim batch (giới hạn 1) qua coordinator.
    await coordinator.handle_callback(_claim_cb(user_id="worker-1"))
    first_job_id = None
    for record in manager.list_jobs():
        if record.pull_assigned_telegram_user_id == "worker-1":
            first_job_id = record.job.job_id
            break
    assert first_job_id is not None

    send_message_count_before = len(client.send_message_calls)

    error_record = manager.get_job(first_job_id)
    assert error_record is not None
    await coordinator.handle_pull_error(error_record, "approve_blocked")

    # KHÔNG có job mới nào được claim thêm cho worker-1 — coordinator
    # không tự claim account khác khi lỗi trong model batch.
    assigned_job_ids = [
        r.job.job_id
        for r in manager.list_jobs()
        if r.pull_assigned_telegram_user_id == "worker-1"
    ]
    assert assigned_job_ids == [first_job_id]

    # KHÔNG có sendMessage text lỗi kỹ thuật riêng.
    assert (
        len(client.send_message_calls) == send_message_count_before
    ), "handle_pull_error KHÔNG được gửi sendMessage mới — phải edit status card"

    # Batch total=1, đã settle 1/1 (lỗi) → card finalize "Cả 1 job đều gặp lỗi".
    edit_texts = [call["text"] for call in client.edit_text_calls]
    assert any("đều gặp lỗi hệ thống" in t for t in edit_texts)
    assert not await coordinator._status_card.has_active("worker-1")  # noqa: SLF001


async def test_handle_pull_error_no_active_session_is_noop() -> None:
    """Job ERROR nhưng worker không có phiên đang chạy (VD phiên đã đóng
    qua /reset, hoặc job resume từ DB sau restart) → CHỈ log, KHÔNG gửi
    message mới.
    """
    manager, settings, coordinator, client, _handler = await _setup(n_accounts=2)

    # Claim job qua low-level API (không đi qua coordinator → tracker
    # KHÔNG có session cho worker này).
    job_id = await manager.claim_pull_account(
        "worker-orphan", "chat-1", None, None
    )
    assert job_id is not None
    record = manager.get_job(job_id)
    assert record is not None

    send_before = len(client.send_message_calls)
    edit_before = len(client.edit_text_calls)

    await coordinator.handle_pull_error(record, "network_error")

    assert len(client.send_message_calls) == send_before
    assert len(client.edit_text_calls) == edit_before
    # KHÔNG có job mới nào được assign cho worker-orphan (chỉ 1 job cũ).
    assigned = [
        r for r in manager.list_jobs()
        if r.pull_assigned_telegram_user_id == "worker-orphan"
    ]
    assert len(assigned) == 1
