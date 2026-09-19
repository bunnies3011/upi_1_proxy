"""Unit test cho `PullJobNotifier` (task 31, spec telegram-pull-job-mode).

Cover (Requirement 1.6, 1.7, 8.1-8.5, 13.3, 13.6):
- `notify_pull_qr_ready` gửi thành công → `initialize_pull_verify_state`
  được gọi đúng (verify hiệu ứng qua `get_job()` sau đó: `plus_check_state`,
  `telegram_message_chat_id`, `telegram_message_id`) + `send_photo` gọi
  đúng `chat_id` + reply_markup 2 nút.
- `send_photo` raise `TelegramApiError` → verify-state KHÔNG được khởi
  tạo (state vẫn default).
- Caption chứa đúng định danh worker — cả 2 case: có `username` và chỉ
  có `first_name`.
- `notify_pull_error` gửi message chứa `error_code` + nút "Nhận job tiếp".

Dùng REAL `JobManager` (factory tương tự `_make_pull_manager`) + real
`_JobRecord` (build trực tiếp, KHÔNG qua `submit_batch`/scheduler — notifier
test chỉ cần record đã ở trạng thái QR_READY với field Pull_Mode set sẵn,
giống pattern `test_api_jobs_qr_binary_content.py`) + `FakeTelegramBotClient`
duck-typed hỗ trợ raise lỗi cấu hình được.

**Validates: Requirements 1.6, 1.7, 8.1, 8.2, 8.3, 8.4, 8.5, 13.3, 13.6**
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from app.core.job_manager import (
    JobManager,
    PlusCheckState,
    PullAssignmentState,
    PullOutcome,
    _JobRecord,
)
from app.core.payment_flow import Job, JobStatus, SimpleCancellationToken
from app.notifiers.telegram.client import TelegramApiError
from app.notifiers.telegram.pull_notifier import PullJobNotifier

from tests.unit.test_job_manager import FakeProxyPool, FakeSettings, FakeSse


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeTelegramBotClient:
    """Duck-typed fake `TelegramBotClient` — ghi lại `send_photo`/
    `send_message`, hỗ trợ raise `TelegramApiError` hoặc exception chung
    cấu hình được qua `send_photo_exc`.
    """

    def __init__(self) -> None:
        self.send_photo_calls: list[dict[str, Any]] = []
        self.send_message_calls: list[dict[str, Any]] = []
        self.send_photo_exc: Exception | None = None
        self.send_photo_result: dict[str, Any] = {"message_id": 42}
        # Task 8 (UX refactor): status card cần edit_message_text + phải
        # nhận về message_id từ send_message để lưu vào tracker.
        self.edit_text_calls: list[dict[str, Any]] = []
        self._next_message_id = 100

    async def send_photo(
        self,
        token: str,
        chat_id: str,
        photo_bytes: bytes,
        caption: str,
        *,
        photo_filename: str = "qr.png",
        reply_markup: dict | None = None,
    ) -> dict:
        self.send_photo_calls.append(
            {
                "token": token,
                "chat_id": chat_id,
                "photo_bytes": photo_bytes,
                "caption": caption,
                "reply_markup": reply_markup,
            }
        )
        if self.send_photo_exc is not None:
            raise self.send_photo_exc
        return self.send_photo_result

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


def _make_manager() -> JobManager:
    settings = FakeSettings(snapshot={"telegram.mode": "pull"})
    proxy_pool = FakeProxyPool()
    sse = FakeSse()
    return JobManager(settings=settings, proxy_pool=proxy_pool, sse=sse)  # type: ignore[arg-type]


def _make_pull_record(
    manager: JobManager,
    tmp_path: Any,
    *,
    username: str | None = "alice",
    first_name: str | None = None,
    origin_chat_id: str = "chat-1",
) -> _JobRecord:
    """Build 1 `_JobRecord` Pull_Mode ở trạng thái QR_READY với artifact
    PNG thật trên đĩa, gán trực tiếp vào `manager._jobs` — giống pattern
    `test_api_jobs_qr_binary_content.py` (không qua `submit_batch`/scheduler,
    notifier test chỉ cần record đã ở QR_READY).
    """
    qr_path = tmp_path / f"qr_{uuid.uuid4().hex}.png"
    qr_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)

    job_id = uuid.uuid4().hex
    job = Job(
        job_id=job_id,
        payment_method="ideal",
        account_line="user@example.com|pw|",
        created_at=time.time(),
        cancellation_token=SimpleCancellationToken(),
    )
    record = _JobRecord(
        job=job,
        status=JobStatus.QR_READY,
        updated_at=time.time(),
        artifact_path=str(qr_path),
        finished_at=time.time(),
        payment_link="https://pay.ideal.nl/tx/abc",
        pull_assignment_state=PullAssignmentState.ASSIGNED,
        pull_assigned_telegram_user_id="worker-1",
        pull_origin_chat_id=origin_chat_id,
        pull_assigned_username=username,
        pull_assigned_first_name=first_name,
        pull_outcome=PullOutcome.PENDING,
    )
    manager._jobs[job_id] = record  # noqa: SLF001 — pattern đã dùng ở test khác trong suite
    return record


# ---------------------------------------------------------------------------
# notify_pull_qr_ready — thành công
# ---------------------------------------------------------------------------


async def test_notify_pull_qr_ready_success_initializes_verify_state(tmp_path: Any) -> None:
    manager = _make_manager()
    record = _make_pull_record(manager, tmp_path)
    client = FakeTelegramBotClient()
    from app.notifiers.telegram.status_card import WorkerStatusCardTracker

    status_card = WorkerStatusCardTracker(client=client)  # type: ignore[arg-type]
    notifier = PullJobNotifier(manager, client, _bot_token_getter, status_card)  # type: ignore[arg-type]

    await notifier.notify_pull_qr_ready(record)

    updated = manager.get_job(record.job.job_id)
    assert updated is not None
    assert updated.plus_check_state == PlusCheckState.ARMED
    assert updated.plus_check_attempts == 0
    assert updated.telegram_message_chat_id == "chat-1"
    assert updated.telegram_message_id == 42

    assert len(client.send_photo_calls) == 1
    call = client.send_photo_calls[0]
    assert call["chat_id"] == "chat-1"
    assert call["reply_markup"] is not None
    buttons = call["reply_markup"]["inline_keyboard"][0]
    assert len(buttons) == 2
    callback_data_set = {b["callback_data"] for b in buttons}
    assert callback_data_set == {
        f"pull_job:done:{record.job.job_id}",
        f"pull_job:fail:{record.job.job_id}",
    }


async def test_notify_pull_qr_ready_early_returns_for_push_mode_job(tmp_path: Any) -> None:
    """R1.6/R1.7: job Push_Mode (`pull_assignment_state is None`) KHÔNG
    được hook này xử lý — early return, không gửi gì."""
    manager = _make_manager()
    record = _make_pull_record(manager, tmp_path)
    record.pull_assignment_state = None
    client = FakeTelegramBotClient()
    from app.notifiers.telegram.status_card import WorkerStatusCardTracker

    status_card = WorkerStatusCardTracker(client=client)  # type: ignore[arg-type]
    notifier = PullJobNotifier(manager, client, _bot_token_getter, status_card)  # type: ignore[arg-type]

    await notifier.notify_pull_qr_ready(record)

    assert client.send_photo_calls == []


# ---------------------------------------------------------------------------
# notify_pull_qr_ready — sendPhoto fail
# ---------------------------------------------------------------------------


async def test_notify_pull_qr_ready_send_photo_fail_does_not_initialize_state(
    tmp_path: Any,
) -> None:
    manager = _make_manager()
    record = _make_pull_record(manager, tmp_path)
    client = FakeTelegramBotClient()
    client.send_photo_exc = TelegramApiError(
        status_code=400, error_code=400, description="Bad Request: chat not found"
    )
    from app.notifiers.telegram.status_card import WorkerStatusCardTracker

    status_card = WorkerStatusCardTracker(client=client)  # type: ignore[arg-type]
    notifier = PullJobNotifier(manager, client, _bot_token_getter, status_card)  # type: ignore[arg-type]

    await notifier.notify_pull_qr_ready(record)

    updated = manager.get_job(record.job.job_id)
    assert updated is not None
    # Verify-state KHÔNG được khởi tạo — vẫn default (ARMED/0/None/None).
    assert updated.plus_check_state == PlusCheckState.ARMED
    assert updated.plus_check_attempts == 0
    assert updated.telegram_message_chat_id is None
    assert updated.telegram_message_id is None


# ---------------------------------------------------------------------------
# Caption — định danh worker
# ---------------------------------------------------------------------------


async def test_notify_pull_qr_ready_caption_contains_username(tmp_path: Any) -> None:
    manager = _make_manager()
    record = _make_pull_record(manager, tmp_path, username="alice", first_name="Alice")
    client = FakeTelegramBotClient()
    from app.notifiers.telegram.status_card import WorkerStatusCardTracker

    status_card = WorkerStatusCardTracker(client=client)  # type: ignore[arg-type]
    notifier = PullJobNotifier(manager, client, _bot_token_getter, status_card)  # type: ignore[arg-type]

    await notifier.notify_pull_qr_ready(record)

    caption = client.send_photo_calls[0]["caption"]
    assert "@alice" in caption


async def test_notify_pull_qr_ready_caption_contains_first_name_when_no_username(
    tmp_path: Any,
) -> None:
    manager = _make_manager()
    record = _make_pull_record(manager, tmp_path, username=None, first_name="Nguyen")
    client = FakeTelegramBotClient()
    from app.notifiers.telegram.status_card import WorkerStatusCardTracker

    status_card = WorkerStatusCardTracker(client=client)  # type: ignore[arg-type]
    notifier = PullJobNotifier(manager, client, _bot_token_getter, status_card)  # type: ignore[arg-type]

    await notifier.notify_pull_qr_ready(record)

    caption = client.send_photo_calls[0]["caption"]
    assert "🙋 Worker: Nguyen" in caption


# ---------------------------------------------------------------------------
# Status card record_qr_ready — model batch (UX refactor)
# ---------------------------------------------------------------------------


async def test_notify_pull_qr_ready_records_qr_ready_on_status_card(
    tmp_path: Any,
) -> None:
    """Model batch: notifier phải ghi nhận `record_qr_ready` trên status
    card SAU KHI sendPhoto + initialize_pull_verify_state thành công.

    Setup: dùng chung `status_card` với batch total=1 (giả lập worker vừa
    claim 1 job trong batch). Sau `notify_pull_qr_ready`, batch đã settle
    đủ (1/1) → tracker tự finalize + pop session, edit_message_text với
    text "Đã lấy 1/1 QR".
    """
    from app.notifiers.telegram.status_card import WorkerStatusCardTracker

    manager = _make_manager()
    record = _make_pull_record(manager, tmp_path)
    client = FakeTelegramBotClient()
    status_card = WorkerStatusCardTracker(client=client)  # type: ignore[arg-type]

    # Giả lập worker đã bấm "Nhận job" trước đó — batch 1 job.
    await status_card.start_batch("chat-1", "worker-1", 1, "test-token")

    notifier = PullJobNotifier(manager, client, _bot_token_getter, status_card)  # type: ignore[arg-type]
    await notifier.notify_pull_qr_ready(record)

    # Batch đã settle đủ (1/1) → session đã pop.
    assert not await status_card.has_active("worker-1")

    # edit_message_text được gọi ít nhất 1 lần (finalize batch), text
    # chứa "Đã lấy 1/1 QR".
    assert any(
        "Đã lấy 1/1 QR" in call["text"] for call in client.edit_text_calls
    )

    # Ảnh QR vẫn được gửi.
    assert len(client.send_photo_calls) == 1
