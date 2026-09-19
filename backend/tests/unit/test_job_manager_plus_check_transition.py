"""Unit test cho `JobManager.record_plus_check_transition` (task 17, spec
telegram-pull-job-mode).

Cover (Requirement 22.8, 22.9):
- Toàn bộ 6 transition hợp lệ trong `_PLUS_CHECK_TRANSITION_WHITELIST`
  (`armed->checking`, `checking->verified`, `checking->armed`,
  `checking->exhausted_2`, `armed->failed_marked`, `checking->failed_marked`).
- Vài cặp transition KHÔNG hợp lệ (`verified->armed`,
  `exhausted_2->checking`) phải trả `False` và giữ nguyên state cũ.
- `attempts` mismatch (CAS check) bị từ chối, giữ nguyên state/attempts.
- `plus_check_attempts` không thể vượt 2 trong flow bình thường — whitelist
  tự chặn `checking->armed` khi `attempts==2` (chỉ chấp nhận `attempts==1`).
- Race: 2 `asyncio.gather` gọi `record_plus_check_transition(armed->checking)`
  đồng thời cho CÙNG 1 job — chỉ đúng 1 thành công, `plus_check_attempts`
  cuối cùng là 1 (không double-increment).

`record_plus_check_transition` là method generic (dùng chung cho cả
Push_Mode và Pull_Mode) — không phụ thuộc `telegram.mode` hay Pull_Account_Pool,
nên test dùng `_make_manager()` (Push_Mode mặc định) từ `test_job_manager.py`
qua import trực tiếp, tránh duplicate test double.
"""

from __future__ import annotations

import asyncio

from app.core.job_manager import PlusCheckState
from app.core.payment_flow import JobResult, JobStatus

from tests.unit.test_job_manager import FakeHandler, _make_manager


async def _make_job(manager) -> str:
    """Tạo 1 job mới qua `submit_batch`, trả `job_id`. `run_delay` dài để
    job giữ nguyên RUNNING/PENDING suốt test, không tự chuyển terminal
    can thiệp vào việc đọc lại record — dù `plus_check_state`/
    `plus_check_attempts` độc lập với `status` job, giữ delay dài cho an
    toàn/tất định."""
    result = await manager.submit_batch("ideal", [f"acc-{id(object())}@x|p"])
    return result.created_job_ids[0]


def _make_manager_with_handler():
    manager, *_ = _make_manager()
    handler = FakeHandler(run_delay=10.0)
    manager.register_handler("ideal", handler)
    return manager


# ---------------------------------------------------------------------------
# Toàn bộ whitelist hợp lệ
# ---------------------------------------------------------------------------


async def test_armed_to_checking_succeeds_and_increments_attempts() -> None:
    manager = _make_manager_with_handler()
    job_id = await _make_job(manager)

    ok = await manager.record_plus_check_transition(job_id, "armed", "checking", 0)

    assert ok is True
    record = manager.get_job(job_id)
    assert record.plus_check_state == PlusCheckState.CHECKING
    assert record.plus_check_attempts == 1


async def test_checking_to_verified_succeeds_and_keeps_attempts() -> None:
    manager = _make_manager_with_handler()
    job_id = await _make_job(manager)
    await manager.record_plus_check_transition(job_id, "armed", "checking", 0)

    ok = await manager.record_plus_check_transition(job_id, "checking", "verified", 1)

    assert ok is True
    record = manager.get_job(job_id)
    assert record.plus_check_state == PlusCheckState.VERIFIED
    assert record.plus_check_attempts == 1


async def test_checking_to_armed_with_attempts_1_succeeds() -> None:
    manager = _make_manager_with_handler()
    job_id = await _make_job(manager)
    await manager.record_plus_check_transition(job_id, "armed", "checking", 0)

    ok = await manager.record_plus_check_transition(job_id, "checking", "armed", 1)

    assert ok is True
    record = manager.get_job(job_id)
    assert record.plus_check_state == PlusCheckState.ARMED
    assert record.plus_check_attempts == 1


async def test_checking_to_exhausted_2_with_attempts_2_succeeds() -> None:
    manager = _make_manager_with_handler()
    job_id = await _make_job(manager)
    # armed->checking (attempts becomes 1)
    await manager.record_plus_check_transition(job_id, "armed", "checking", 0)
    # checking->armed (back to armed, attempts stays 1)
    await manager.record_plus_check_transition(job_id, "checking", "armed", 1)
    # armed->checking again (attempts becomes 2)
    await manager.record_plus_check_transition(job_id, "armed", "checking", 0)

    ok = await manager.record_plus_check_transition(
        job_id, "checking", "exhausted_2", 2
    )

    assert ok is True
    record = manager.get_job(job_id)
    assert record.plus_check_state == PlusCheckState.EXHAUSTED_2
    assert record.plus_check_attempts == 2


async def test_armed_to_failed_marked_succeeds() -> None:
    manager = _make_manager_with_handler()
    job_id = await _make_job(manager)

    ok = await manager.record_plus_check_transition(job_id, "armed", "failed_marked", 0)

    assert ok is True
    record = manager.get_job(job_id)
    assert record.plus_check_state == PlusCheckState.FAILED_MARKED


async def test_checking_to_failed_marked_succeeds() -> None:
    manager = _make_manager_with_handler()
    job_id = await _make_job(manager)
    await manager.record_plus_check_transition(job_id, "armed", "checking", 0)

    ok = await manager.record_plus_check_transition(
        job_id, "checking", "failed_marked", 1
    )

    assert ok is True
    record = manager.get_job(job_id)
    assert record.plus_check_state == PlusCheckState.FAILED_MARKED


# ---------------------------------------------------------------------------
# Transition KHÔNG hợp lệ — trả False, giữ nguyên state cũ
# ---------------------------------------------------------------------------


async def test_verified_to_armed_rejected_keeps_verified_state() -> None:
    manager = _make_manager_with_handler()
    job_id = await _make_job(manager)
    await manager.record_plus_check_transition(job_id, "armed", "checking", 0)
    await manager.record_plus_check_transition(job_id, "checking", "verified", 1)

    ok = await manager.record_plus_check_transition(job_id, "verified", "armed", 1)

    assert ok is False
    record = manager.get_job(job_id)
    assert record.plus_check_state == PlusCheckState.VERIFIED
    assert record.plus_check_attempts == 1


async def test_exhausted_2_to_checking_rejected_keeps_exhausted_2_state() -> None:
    manager = _make_manager_with_handler()
    job_id = await _make_job(manager)
    await manager.record_plus_check_transition(job_id, "armed", "checking", 0)
    await manager.record_plus_check_transition(job_id, "checking", "armed", 1)
    await manager.record_plus_check_transition(job_id, "armed", "checking", 0)
    await manager.record_plus_check_transition(job_id, "checking", "exhausted_2", 2)

    ok = await manager.record_plus_check_transition(job_id, "exhausted_2", "checking", 2)

    assert ok is False
    record = manager.get_job(job_id)
    assert record.plus_check_state == PlusCheckState.EXHAUSTED_2
    assert record.plus_check_attempts == 2


# ---------------------------------------------------------------------------
# attempts mismatch (CAS check) — trả False, giữ nguyên state/attempts
# ---------------------------------------------------------------------------


async def test_checking_to_armed_wrong_attempts_rejected() -> None:
    manager = _make_manager_with_handler()
    job_id = await _make_job(manager)
    await manager.record_plus_check_transition(job_id, "armed", "checking", 0)

    ok = await manager.record_plus_check_transition(job_id, "checking", "armed", 2)

    assert ok is False
    record = manager.get_job(job_id)
    assert record.plus_check_state == PlusCheckState.CHECKING
    assert record.plus_check_attempts == 1


async def test_checking_to_exhausted_2_wrong_attempts_rejected() -> None:
    manager = _make_manager_with_handler()
    job_id = await _make_job(manager)
    await manager.record_plus_check_transition(job_id, "armed", "checking", 0)

    ok = await manager.record_plus_check_transition(job_id, "checking", "exhausted_2", 1)

    assert ok is False
    record = manager.get_job(job_id)
    assert record.plus_check_state == PlusCheckState.CHECKING
    assert record.plus_check_attempts == 1


# ---------------------------------------------------------------------------
# attempts không vượt 2 — whitelist tự chặn checking->armed khi attempts==2
# ---------------------------------------------------------------------------


async def test_attempts_never_exceeds_2_via_normal_flow() -> None:
    """Chu trình `armed->checking->armed` lặp lại tăng `attempts` mỗi vòng
    `armed->checking`. Sau khi `attempts` đạt 2, `checking->armed` (gate
    ở `attempts==1`) PHẢI bị từ chối — chỉ `checking->exhausted_2` (gate
    ở `attempts==2`) mới hợp lệ tiếp theo, đảm bảo `attempts` không thể
    vượt 2 qua flow whitelist bình thường."""
    manager = _make_manager_with_handler()
    job_id = await _make_job(manager)

    await manager.record_plus_check_transition(job_id, "armed", "checking", 0)
    await manager.record_plus_check_transition(job_id, "checking", "armed", 1)
    await manager.record_plus_check_transition(job_id, "armed", "checking", 0)

    record = manager.get_job(job_id)
    assert record.plus_check_attempts == 2

    ok = await manager.record_plus_check_transition(job_id, "checking", "armed", 2)

    assert ok is False
    record = manager.get_job(job_id)
    assert record.plus_check_state == PlusCheckState.CHECKING
    assert record.plus_check_attempts == 2


# ---------------------------------------------------------------------------
# Race condition (R22.9, Property 9) — lock per-job
# ---------------------------------------------------------------------------


async def test_concurrent_armed_to_checking_only_one_wins() -> None:
    """2 callback chồng lấp cùng gọi `armed->checking` cho CÙNG job — lock
    per-job serialize, callback thứ 2 thấy state đã là `checking` (không
    còn khớp `from_state="armed"`) → bị từ chối. `plus_check_attempts`
    cuối cùng phải là 1, KHÔNG bị double-increment."""
    manager = _make_manager_with_handler()
    job_id = await _make_job(manager)

    results = await asyncio.gather(
        manager.record_plus_check_transition(job_id, "armed", "checking", 0),
        manager.record_plus_check_transition(job_id, "armed", "checking", 0),
    )

    successes = [r for r in results if r is True]
    failures = [r for r in results if r is False]
    assert len(successes) == 1
    assert len(failures) == 1

    record = manager.get_job(job_id)
    assert record.plus_check_state == PlusCheckState.CHECKING
    assert record.plus_check_attempts == 1
