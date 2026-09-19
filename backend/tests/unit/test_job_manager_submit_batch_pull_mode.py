"""Unit test cho `JobManager.submit_batch`/`load_from_db` — nhánh Pull_Mode
(task 10, spec telegram-pull-job-mode).

Cover (Requirement 4.1, 4.2, 4.5, 4.6):
- `submit_batch` khi `telegram.mode == "pull"`: job mới tạo có
  `pull_assignment_state=UNASSIGNED`/`pull_outcome=PENDING` VÀ KHÔNG được
  đẩy vào `_pending_order` (không bị scheduler nhận).
- `submit_batch` khi `telegram.mode == "push"` (hoặc chưa set): hành vi
  giữ nguyên 100% — job vào `_pending_order` ngay, 2 field mới là `None`.
- Dedup/skip messages không đổi ở cả 2 mode (`job_already_active` khi
  submit trùng account lúc job cũ còn pending).
- `load_from_db`: row PENDING + `pull_assignment_state=unassigned` → job_id
  KHÔNG vào `_pending_order` sau load (còn ở Pull_Account_Pool).
- `load_from_db`: row PENDING + `pull_assignment_state=assigned` (job đang
  chạy dở khi shutdown) → job_id CÓ vào `_pending_order` sau load (giữ
  đúng hành vi resume hiện có).

Tái dùng test double từ `test_job_manager.py` (task 8.3) qua import trực
tiếp — tránh duplicate `FakeSettings`/`FakeProxyPool`/`FakeSse`/`FakeHandler`/
`_make_manager`/`_wait_until`.
"""

from __future__ import annotations

from pathlib import Path

from app.core.db import DbEngine
from app.core.job_manager import (
    JobManager,
    PullAssignmentState,
    PullOutcome,
)
from app.core.job_repo import JobRepository, JobRow
from app.core.payment_flow import JobResult, JobStatus, ParsedAccount

from tests.unit.test_job_manager import (
    FakeHandler,
    FakeProxyPool,
    FakeSettings,
    FakeSse,
    _make_manager,
    _wait_until,
)


class DedupFakeHandler(FakeHandler):
    """`FakeHandler` mở rộng thêm `get_account_dedup_key` — dùng để test
    dedup/skip messages, vì `FakeHandler` gốc không implement method này
    (dedup index chỉ populate khi handler có method optional này)."""

    def get_account_dedup_key(self, parsed: ParsedAccount) -> str:
        # Dùng chính raw_line làm key — đủ để 2 dòng giống nhau bị coi là
        # cùng 1 account trong test.
        return parsed.raw_line


# ---------------------------------------------------------------------------
# submit_batch — Pull_Mode vs Push_Mode
# ---------------------------------------------------------------------------


async def test_submit_batch_pull_mode_sets_unassigned_and_skips_pending_order() -> None:
    manager, *_ = _make_manager(snapshot={"telegram.mode": "pull"})
    handler = FakeHandler(run_result=JobResult(status=JobStatus.QR_READY))
    manager.register_handler("ideal", handler)

    result = await manager.submit_batch("ideal", ["a@x|p"])
    job_id = result.created_job_ids[0]
    record = manager.get_job(job_id)

    assert record is not None
    assert record.pull_assignment_state == PullAssignmentState.UNASSIGNED
    assert record.pull_outcome == PullOutcome.PENDING
    assert job_id not in manager._pending_order  # noqa: SLF001

    # Job không nằm trong _pending_order → scheduler không bao giờ nhận,
    # status vẫn PENDING mãi (không tự chuyển RUNNING/QR_READY).
    import asyncio

    await asyncio.sleep(0.05)
    assert manager.get_job(job_id).status == JobStatus.PENDING  # type: ignore[union-attr]


async def test_submit_batch_push_mode_keeps_existing_behavior() -> None:
    """Chỉ kiểm tra hành vi của `submit_batch` (đẩy vào `_pending_order`,
    2 field mới `None`) — KHÔNG chờ job chạy tới QR_READY, vì thực thi
    scheduler/proxy/auto-retry đầy đủ nằm ngoài phạm vi test này (đã có
    coverage riêng ở `test_job_manager.py`)."""
    manager, *_ = _make_manager(snapshot={"telegram.mode": "push"})
    handler = FakeHandler(run_result=JobResult(status=JobStatus.QR_READY))
    manager.register_handler("ideal", handler)

    result = await manager.submit_batch("ideal", ["a@x|p"])
    job_id = result.created_job_ids[0]
    record = manager.get_job(job_id)

    assert record is not None
    assert record.pull_assignment_state is None
    assert record.pull_outcome is None
    assert job_id in manager._pending_order  # noqa: SLF001


async def test_submit_batch_mode_unset_keeps_existing_behavior() -> None:
    """`telegram.mode` chưa set trong snapshot (key vắng mặt) → coi như
    Push_Mode, giữ 100% hành vi cũ."""
    manager, *_ = _make_manager(snapshot={})
    handler = FakeHandler(run_result=JobResult(status=JobStatus.QR_READY))
    manager.register_handler("ideal", handler)

    result = await manager.submit_batch("ideal", ["a@x|p"])
    job_id = result.created_job_ids[0]
    record = manager.get_job(job_id)

    assert record is not None
    assert record.pull_assignment_state is None
    assert record.pull_outcome is None
    assert job_id in manager._pending_order  # noqa: SLF001


# ---------------------------------------------------------------------------
# Dedup / skip messages — không đổi ở cả 2 mode
# ---------------------------------------------------------------------------


async def test_submit_batch_dedup_skip_unchanged_in_pull_mode() -> None:
    manager, *_ = _make_manager(snapshot={"telegram.mode": "pull"})
    handler = DedupFakeHandler(run_result=JobResult(status=JobStatus.QR_READY))
    manager.register_handler("ideal", handler)

    result = await manager.submit_batch("ideal", ["same@x|p", "same@x|p"])

    assert len(result.created_job_ids) == 1
    assert len(result.skipped) == 1
    assert result.skipped[0]["reason"] == "job_already_active"


async def test_submit_batch_dedup_skip_unchanged_in_push_mode() -> None:
    manager, *_ = _make_manager(snapshot={"telegram.mode": "push"})
    handler = DedupFakeHandler(run_delay=1.0)  # giữ job pending/running để dedup active
    manager.register_handler("ideal", handler)

    result = await manager.submit_batch("ideal", ["same@x|p", "same@x|p"])

    assert len(result.created_job_ids) == 1
    assert len(result.skipped) == 1
    assert result.skipped[0]["reason"] == "job_already_active"


# ---------------------------------------------------------------------------
# load_from_db — Pull_Mode
# ---------------------------------------------------------------------------


def _pull_job_row(job_id: str, pull_assignment_state: str) -> JobRow:
    return JobRow(
        job_id=job_id,
        payment_method="ideal",
        account_line="acc@x|p",
        status="pending",
        order_num=1,
        created_at=1000.0,
        updated_at=1000.0,
        dedup_key=None,
        artifact_path=None,
        error_code=None,
        error_message=None,
        payment_link=None,
        settings_snapshot={},
        pull_assignment_state=pull_assignment_state,
        pull_outcome="pending",
    )


async def test_load_from_db_unassigned_pull_job_not_in_pending_order(
    sqlite_path: Path,
) -> None:
    engine = DbEngine(sqlite_path)
    await engine.init_schema()
    repo = JobRepository(engine)
    try:
        row = _pull_job_row("job-unassigned", "unassigned")
        await repo.upsert(row)

        settings = FakeSettings()
        proxy_pool = FakeProxyPool()
        sse = FakeSse()
        manager = JobManager(
            settings=settings,  # type: ignore[arg-type]
            proxy_pool=proxy_pool,  # type: ignore[arg-type]
            sse=sse,  # type: ignore[arg-type]
            job_repo=repo,
        )

        await manager.load_from_db()

        assert "job-unassigned" not in manager._pending_order  # noqa: SLF001
        record = manager.get_job("job-unassigned")
        assert record is not None
        assert record.pull_assignment_state == PullAssignmentState.UNASSIGNED
    finally:
        await engine.close()


async def test_load_from_db_assigned_pull_job_resumes_in_pending_order(
    sqlite_path: Path,
) -> None:
    """Job `pending` + `pull_assignment_state=assigned` (đang chạy dở khi
    shutdown) PHẢI vào `_pending_order` sau load — giữ đúng hành vi resume
    hiện có (Requirement 4.6)."""
    engine = DbEngine(sqlite_path)
    await engine.init_schema()
    repo = JobRepository(engine)
    try:
        row = _pull_job_row("job-assigned", "assigned")
        await repo.upsert(row)

        settings = FakeSettings()
        proxy_pool = FakeProxyPool()
        sse = FakeSse()
        manager = JobManager(
            settings=settings,  # type: ignore[arg-type]
            proxy_pool=proxy_pool,  # type: ignore[arg-type]
            sse=sse,  # type: ignore[arg-type]
            job_repo=repo,
        )

        await manager.load_from_db()

        assert "job-assigned" in manager._pending_order  # noqa: SLF001
        record = manager.get_job("job-assigned")
        assert record is not None
        assert record.pull_assignment_state == PullAssignmentState.ASSIGNED
    finally:
        await engine.close()
