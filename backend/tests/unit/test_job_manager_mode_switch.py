"""Unit test cho `JobManager.set_operating_mode` (task 19, spec
telegram-pull-job-mode).

Cover (Requirement 3.1, 3.2, 3.5, 3.6):
- Đổi mode khi KHÔNG có job dở dang (assigned/pending) → thành công ngay,
  `settings.set("telegram.mode", ...)` được gọi đúng giá trị mới.
- Có job dở dang + `force=False` → raise `ModeSwitchBlockedError` kèm đúng
  `job_ids`, mode KHÔNG đổi.
- `force=True` → tất cả job dở dang chuyển `pull_outcome = FAIL`, mode đổi
  thành công, mode-switch-notify hook được gọi đúng số lần (1 lần/job).
- Hook raise exception → log nhưng mode vẫn đổi (best-effort, R3.6).
- `resolve_pull_outcome` raise giữa vòng lặp force-fail (data-integrity
  failure) → mode KHÔNG đổi, exception propagate (R3.5).

Tái dùng test double từ `test_job_manager_pull_mode.py` (task 15) qua
import trực tiếp (`_PullModeFakeProxyPool`, `FakeHandler`, `FakeSse`) —
chỉ mở rộng `_PullModeFakeSettings` thêm `.set()` (chưa có ở bản gốc, vì
`set_operating_mode` là method DUY NHẤT trong toàn bộ JobManager gọi
`self._settings.set(...)` — không test nào trước đây cần nó).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.errors import JobAlreadyResolvedError, ModeSwitchBlockedError
from app.core.job_manager import JobManager, PullOutcome
from app.core.payment_flow import JobResult, JobStatus

from tests.unit.test_job_manager import FakeHandler, FakeSse
from tests.unit.test_job_manager_pull_mode import (
    _PullModeFakeProxyPool,
    _PullModeFakeSettings,
    _seed_accounts,
)


# ---------------------------------------------------------------------------
# Test double mở rộng — riêng cho file này
# ---------------------------------------------------------------------------


class _ModeSwitchFakeSettings(_PullModeFakeSettings):
    """`_PullModeFakeSettings` (task 15) đã có `.get()` + `.list()` nhưng
    chưa có `.set()` — `set_operating_mode` là method DUY NHẤT gọi
    `self._settings.set("telegram.mode", new_mode)` (bước cuối). Ghi lại
    mọi lời gọi vào `set_calls` để test assert mode có thực sự đổi hay
    KHÔNG (VD khi bị `ModeSwitchBlockedError` chặn hoặc khi
    `resolve_pull_outcome` raise giữa vòng lặp force-fail)."""

    def __init__(self, snapshot: dict[str, Any] | None = None) -> None:
        super().__init__(snapshot=snapshot)
        self.set_calls: list[tuple[str, Any]] = []

    async def set(self, key: str, value: Any) -> None:
        self.set_calls.append((key, value))
        self._snapshot[key] = value


def _make_mode_switch_manager(
    max_concurrent_per_user: int = 10,
) -> tuple[JobManager, _ModeSwitchFakeSettings, _PullModeFakeProxyPool, FakeSse]:
    settings = _ModeSwitchFakeSettings(
        snapshot={
            "telegram.mode": "pull",
            "telegram.pull_mode.max_concurrent_jobs_per_user": max_concurrent_per_user,
        }
    )
    proxy_pool = _PullModeFakeProxyPool()
    sse = FakeSse()
    manager = JobManager(settings=settings, proxy_pool=proxy_pool, sse=sse)  # type: ignore[arg-type]
    return manager, settings, proxy_pool, sse


# ---------------------------------------------------------------------------
# Không có job dở dang → thành công ngay (R3.3)
# ---------------------------------------------------------------------------


async def test_set_operating_mode_succeeds_immediately_when_no_pending_jobs() -> None:
    manager, settings, *_ = _make_mode_switch_manager()

    await manager.set_operating_mode("push")

    assert ("telegram.mode", "push") in settings.set_calls


# ---------------------------------------------------------------------------
# Có job dở dang + force=False → ModeSwitchBlockedError (R3.1)
# ---------------------------------------------------------------------------


async def test_set_operating_mode_blocked_without_force_lists_correct_job_ids() -> None:
    manager, settings, *_ = _make_mode_switch_manager()
    handler = FakeHandler(run_result=JobResult(status=JobStatus.QR_READY))
    manager.register_handler("ideal", handler)
    await _seed_accounts(manager, 1)
    job_id = await manager.claim_pull_account("worker-1", "chat-1", None, None)
    assert job_id is not None

    with pytest.raises(ModeSwitchBlockedError) as exc_info:
        await manager.set_operating_mode("push", force=False)

    assert exc_info.value.job_ids == [job_id]
    assert settings.set_calls == []


# ---------------------------------------------------------------------------
# force=True → force-fail tất cả job dở dang + hook gọi đúng số lần (R3.2)
# ---------------------------------------------------------------------------


async def test_set_operating_mode_force_true_fails_pending_jobs_and_switches_mode() -> None:
    manager, settings, *_ = _make_mode_switch_manager()
    handler = FakeHandler(run_result=JobResult(status=JobStatus.QR_READY))
    manager.register_handler("ideal", handler)
    await _seed_accounts(manager, 2)
    job_id_1 = await manager.claim_pull_account("worker-1", "chat-1", None, None)
    job_id_2 = await manager.claim_pull_account("worker-2", "chat-2", None, None)
    assert job_id_1 is not None
    assert job_id_2 is not None

    hook_calls: list[str] = []

    async def notify_hook(record: Any) -> None:
        hook_calls.append(record.job.job_id)

    manager.register_mode_switch_notify_hook(notify_hook)

    await manager.set_operating_mode("push", force=True)

    assert manager.get_job(job_id_1).pull_outcome == PullOutcome.FAIL  # type: ignore[union-attr]
    assert manager.get_job(job_id_2).pull_outcome == PullOutcome.FAIL  # type: ignore[union-attr]
    assert ("telegram.mode", "push") in settings.set_calls
    assert sorted(hook_calls) == sorted([job_id_1, job_id_2])


# ---------------------------------------------------------------------------
# Hook raise exception → log nhưng mode vẫn đổi (R3.6)
# ---------------------------------------------------------------------------


async def test_set_operating_mode_hook_exception_does_not_block_mode_switch() -> None:
    manager, settings, *_ = _make_mode_switch_manager()
    handler = FakeHandler(run_result=JobResult(status=JobStatus.QR_READY))
    manager.register_handler("ideal", handler)
    await _seed_accounts(manager, 1)
    job_id = await manager.claim_pull_account("worker-1", "chat-1", None, None)
    assert job_id is not None

    async def failing_hook(record: Any) -> None:  # noqa: ARG001
        raise RuntimeError("boom")

    manager.register_mode_switch_notify_hook(failing_hook)

    # KHÔNG raise ra ngoài — best-effort swallow tại boundary.
    await manager.set_operating_mode("push", force=True)

    assert manager.get_job(job_id).pull_outcome == PullOutcome.FAIL  # type: ignore[union-attr]
    assert ("telegram.mode", "push") in settings.set_calls


# ---------------------------------------------------------------------------
# resolve_pull_outcome raise giữa vòng lặp force-fail → mode KHÔNG đổi (R3.5)
# ---------------------------------------------------------------------------


async def test_set_operating_mode_propagates_resolve_error_and_does_not_switch_mode() -> None:
    """Mô phỏng lỗi data-integrity: `resolve_pull_outcome` raise
    `JobAlreadyResolvedError` giữa vòng lặp force-fail (VD race hiếm 1
    đường khác đã resolve job này trước khi `set_operating_mode` chạm
    tới). Vì `set_operating_mode`'s listing step lọc theo
    `pull_outcome == PENDING` NGAY TẠI THỜI ĐIỂM gọi, không thể tạo ra
    kịch bản "job vẫn nằm trong list nhưng đã terminal khi tới lượt xử
    lý" chỉ bằng data thuần trong 1 coroutine tuần tự — inject trực tiếp
    bằng cách monkeypatch `manager.resolve_pull_outcome` để raise ngay
    lần gọi đầu, mô phỏng đúng điểm raise thực tế mà `set_operating_mode`
    KHÔNG catch (bước (a) trong vòng lặp, R3.5)."""
    manager, settings, *_ = _make_mode_switch_manager()
    handler = FakeHandler(run_result=JobResult(status=JobStatus.QR_READY))
    manager.register_handler("ideal", handler)
    await _seed_accounts(manager, 1)
    job_id = await manager.claim_pull_account("worker-1", "chat-1", None, None)
    assert job_id is not None

    async def raising_resolve(_job_id: str, _outcome: PullOutcome) -> None:
        raise JobAlreadyResolvedError(job_id)

    manager.resolve_pull_outcome = raising_resolve  # type: ignore[assignment,method-assign]

    with pytest.raises(JobAlreadyResolvedError):
        await manager.set_operating_mode("push", force=True)

    assert settings.set_calls == []
