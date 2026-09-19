"""Property test cho `core/job_manager.py::JobManager` (Property 41).

**Property 41: Job mới không bị ảnh hưởng retroactive bởi thay đổi Settings**

Semantics R11.2: `JobManager.submit_batch()` phải "chốt" snapshot Settings
tại đúng thời điểm submit — mọi thay đổi Settings SAU đó không được lan
ngược vào các job đã tạo ("no retroactive"). Ngược lại, batch submit sau
khi thay đổi phải nhận đúng giá trị mới.

Sinh xen kẽ 2 batch quanh 1 lần đổi 1 Settings key:

1. `FakeSettings._snapshot = {key: v1}`.
2. `submit_batch(...)` batch 1 → get `record_1`.
3. Đổi `FakeSettings._snapshot = {key: v2}` (giả lập user save Settings mới
   qua UI/endpoint write-through).
4. `submit_batch(...)` batch 2 → get `record_2`.
5. Assert `record_1.settings_snapshot[key] == v1` (không retroactive)
   và `record_2.settings_snapshot[key] == v2` (đọc đúng giá trị hiện hành).

`FakeHandler` chạy `run_delay` khá lâu để đảm bảo record vẫn hiện diện trong
`JobManager._jobs` khi test đọc `settings_snapshot` — nếu handler kịp finish
thành `QR_READY`, record vẫn không mất, nhưng để bám đúng mô tả task và
tránh mọi trường hợp đua cạnh với scheduler ta giữ handler running.

Reuse `FakeSettings` / `FakeHandler` / `FakeProxyPool` / `FakeSse` pattern từ
`backend/tests/unit/test_job_manager.py` (không import trực tiếp để giữ
property test độc lập).

**Validates: Requirements 11.2**
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from hypothesis import given, settings, strategies as st

from app.core.job_manager import JobManager
from app.core.payment_flow import (
    AccountLineError,
    Job,
    JobResult,
    JobStatus,
    ParsedAccount,
)


# ---------------------------------------------------------------------------
# Test doubles — cùng pattern với backend/tests/unit/test_job_manager.py
# ---------------------------------------------------------------------------


class FakeSettings:
    """Stub minimal của `SettingsRepository.list()` với snapshot in-memory
    mutable — test đổi giá trị bằng cách gán lại `_snapshot` trực tiếp."""

    def __init__(self, snapshot: dict[str, Any] | None = None) -> None:
        self._snapshot: dict[str, Any] = snapshot or {}

    async def list(self, prefix: str | None = None) -> dict[str, Any]:
        # Trả copy để caller không mutate ngược state của FakeSettings —
        # cùng semantic với `SettingsRepository.list()` thật (trả dict mới).
        return dict(self._snapshot)


class FakeProxyPool:
    """Direct_Mode luôn — `acquire()` trả None, `release()` no-op. Đủ cho
    property này vì không quan tâm proxy state."""

    async def acquire(self, job_id: str):
        return None

    def release(self, lease) -> None:
        return None


class FakeSse:
    """No-op SSE broadcaster — không tạo network I/O, không capture events
    (test này không assert SSE)."""

    async def broadcast_job_status(self, job_id: str, status: str, **extra: Any) -> None:
        return None

    async def broadcast_job_log(self, job_id: str, message: str, **extra: Any) -> None:
        return None


class FakeHandler:
    """PaymentFlowHandler minimal với `run_delay` để giữ job running đủ
    lâu — không quan trọng kết quả (test không assert QR)."""

    def __init__(self, run_delay: float = 2.0) -> None:
        self.run_delay = run_delay

    def get_max_concurrent_key(self) -> str:
        return "ideal.max_concurrent"

    def parse_account_line(self, line: str):
        if not line.strip():
            return AccountLineError(line=line, reason="empty")
        return ParsedAccount(raw_line=line)

    async def run(self, job: Job, proxy_lease) -> JobResult:
        # Polling loop tôn trọng cancellation_token → sạch khi cleanup cancel
        # handler_task.
        end = asyncio.get_event_loop().time() + self.run_delay
        while asyncio.get_event_loop().time() < end:
            if job.cancellation_token.is_cancelled():
                return JobResult(status=JobStatus.STOPPED)
            await asyncio.sleep(0.005)
        return JobResult(status=JobStatus.QR_READY)


# ---------------------------------------------------------------------------
# Cleanup helper — cancel scheduler + handler_task để `asyncio.run` không bị
# kẹt task nền zombie giữa các hypothesis example (mỗi example là 1 event
# loop mới, tạo mới JobManager).
# ---------------------------------------------------------------------------


async def _shutdown_manager(manager: JobManager) -> None:
    """Cancel toàn bộ background task JobManager đã spawn (scheduler +
    handler_task đang chạy). Bắt buộc trước khi coroutine `_scenario` kết
    thúc để tránh warning "Task was destroyed but it is pending"."""
    scheduler_task = manager._scheduler_task  # noqa: SLF001
    if scheduler_task is not None and not scheduler_task.done():
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task

    live_handler_tasks = [
        record.handler_task
        for record in manager.list_jobs()
        if record.handler_task is not None and not record.handler_task.done()
    ]
    for task in live_handler_tasks:
        task.cancel()
    if live_handler_tasks:
        await asyncio.gather(*live_handler_tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# Property 41
# ---------------------------------------------------------------------------


# Sinh 2 giá trị v1/v2 độc lập trong range whitelist hợp lý cho cả 2 key
# (`ideal.max_concurrent` [1..64], `session_cache.ttl_hours` [1..720]). Giới
# hạn [1..100] theo mô tả task giữ test nhanh nhưng vẫn phủ nhiều giá trị.
_value_strategy = st.integers(min_value=1, max_value=100)

# 2 key thuộc namespace khác nhau (core `ideal.*` và `session_cache.*`) — bảo
# đảm property độc lập với namespace cụ thể, chỉ phụ thuộc semantic snapshot.
_key_strategy = st.sampled_from(["ideal.max_concurrent", "session_cache.ttl_hours"])


@given(v1=_value_strategy, v2=_value_strategy, key=_key_strategy)
@settings(max_examples=30, deadline=None)
def test_settings_snapshot_frozen_at_submit_time_no_retroactive(
    v1: int, v2: int, key: str
) -> None:
    """R11.2: mỗi batch `submit_batch` chốt snapshot Settings tại thời điểm
    submit; thay đổi Settings sau đó KHÔNG lan ngược vào job đã tạo.

    Với mọi (v1, v2, key):
    - `record_1.settings_snapshot[key] == v1` (giá trị TRƯỚC khi đổi).
    - `record_2.settings_snapshot[key] == v2` (giá trị SAU khi đổi).
    """

    async def _scenario() -> None:
        fake_settings = FakeSettings(snapshot={key: v1})
        manager = JobManager(
            settings=fake_settings,  # type: ignore[arg-type]
            proxy_pool=FakeProxyPool(),  # type: ignore[arg-type]
            sse=FakeSse(),  # type: ignore[arg-type]
        )
        handler = FakeHandler(run_delay=2.0)
        manager.register_handler("ideal", handler)

        try:
            # Batch 1 — submit khi settings ở giá trị v1.
            result_1 = await manager.submit_batch("ideal", ["acc-batch1@x|p"])
            assert len(result_1.created_job_ids) == 1, (
                "submit_batch phải tạo đúng 1 job cho 1 dòng account hợp lệ"
            )
            record_1 = manager.get_job(result_1.created_job_ids[0])
            assert record_1 is not None

            # Thay đổi Settings SAU khi submit batch 1 — mô phỏng user save
            # Settings mới qua UI/endpoint write-through.
            fake_settings._snapshot = {key: v2}  # noqa: SLF001

            # Batch 2 — submit khi settings ở giá trị v2.
            result_2 = await manager.submit_batch("ideal", ["acc-batch2@x|p"])
            assert len(result_2.created_job_ids) == 1
            record_2 = manager.get_job(result_2.created_job_ids[0])
            assert record_2 is not None

            # Property 41 — no retroactive.
            assert record_1.settings_snapshot.get(key) == v1, (
                "Job batch 1 bị ảnh hưởng retroactive bởi thay đổi Settings: "
                f"key={key!r}, expected {v1}, "
                f"got {record_1.settings_snapshot.get(key)!r}"
            )
            # Property 41 — batch sau nhận giá trị mới.
            assert record_2.settings_snapshot.get(key) == v2, (
                "Job batch 2 KHÔNG nhận snapshot Settings tại thời điểm submit: "
                f"key={key!r}, expected {v2}, "
                f"got {record_2.settings_snapshot.get(key)!r}"
            )
        finally:
            await _shutdown_manager(manager)

    asyncio.run(_scenario())
