"""Property test cho `core/job_manager.py` — concurrency invariant (task 8.5).

**Property 24: Job_Manager giữ đúng bất biến concurrency và tự bổ sung job
khi có slot trống**

Với mọi chuỗi sự kiện ngẫu nhiên (tạo job mới → job running kết thúc) áp
dụng lên `JobManager` với `ideal.max_concurrent = M` cố định:

1. Tại MỌI thời điểm quan sát được, số IdealJob ở trạng thái `RUNNING`
   luôn `<= M` (không bao giờ vượt ngân sách concurrency).
2. Khi có slot trống (`running < M`) và còn job `pending`, `JobManager`
   luôn nhấc job pending kế tiếp lên chạy trong bounded time — không bỏ
   quên job nào. Assertion này được kiểm gián tiếp qua kết luận cuối: mọi
   job hợp lệ trong batch cuối cùng ĐỀU đạt `QR_READY` trong khoảng thời
   gian polling.

**Validates: Requirements 8.4, 8.5**

Test double (`FakeSettings`, `FakeProxyPool`, `FakeSse`, `FakeHandler`)
được copy nguyên convention từ `tests/unit/test_job_manager.py` (task 8.3)
để cô lập `JobManager` — không mock deep, không import chéo giữa 2 test
module.

Profile `hypothesis` "fast" (`max_examples=20`) được load global trong
`tests/conftest.py`; giá trị `handler_delay`/`total_jobs`/`m` giữ nhỏ để
tổng thời gian ~vài trăm ms/example × 20 example ≤ 10s.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from hypothesis import given, settings as hyp_settings, strategies as st

from app.core.errors import ProxyExhaustedError
from app.core.job_manager import JobManager
from app.core.payment_flow import (
    AccountLineError,
    Job,
    JobResult,
    JobStatus,
    ParsedAccount,
)

# ---------------------------------------------------------------------------
# Test doubles — copy nguyên convention từ tests/unit/test_job_manager.py
# ---------------------------------------------------------------------------


class FakeSettings:
    """Minimal stub cho `SettingsRepository.list()`."""

    def __init__(self, snapshot: dict[str, Any] | None = None) -> None:
        self._snapshot = snapshot or {}

    async def list(self, prefix: str | None = None) -> dict[str, Any]:
        return dict(self._snapshot)


@dataclass
class FakeLease:
    proxy_id: str
    materialized_url: str
    leased_at: float


class FakeProxyPool:
    """Direct_Mode mặc định (`acquire` trả `None`) — property 24 không
    phụ thuộc tương tác với proxy, giữ đơn giản nhất."""

    def __init__(self) -> None:
        self.acquired_for: list[str] = []
        self.released: list[str] = []
        self.mode: str = "direct"

    async def acquire(
        self,
        job_id: str,
        *,
        wait_for_release: bool = False,  # noqa: ARG002
        cancellation_token: Any | None = None,  # noqa: ARG002
    ):
        self.acquired_for.append(job_id)
        if self.mode == "direct":
            return None
        if self.mode == "lease":
            return FakeLease(
                proxy_id="proxy-1",
                materialized_url="http://proxy-1",
                leased_at=time.time(),
            )
        if self.mode == "exhausted":
            raise ProxyExhaustedError(total_proxies=1, dead_count=1, leased_out_count=0)
        raise RuntimeError(f"unknown mode {self.mode}")

    def release(self, lease) -> None:
        if lease is not None:
            self.released.append(lease.proxy_id)


class FakeSse:
    def __init__(self) -> None:
        self.status_events: list[dict[str, Any]] = []
        self.log_events: list[dict[str, Any]] = []

    async def broadcast_job_status(self, job_id: str, status: str, **extra) -> None:
        self.status_events.append({"job_id": job_id, "status": status, **extra})

    async def broadcast_job_log(self, job_id: str, message: str, **extra) -> None:
        self.log_events.append({"job_id": job_id, "message": message, **extra})


class FakeHandler:
    """Handler có `run_delay` cấu hình được — mỗi job kết thúc sau
    `run_delay` giây kể từ khi `handler.run` bắt đầu. Đủ để tạo cửa sổ
    thời gian quan sát nhiều job cùng running và đo max concurrent."""

    def __init__(
        self,
        parse_valid: bool = True,
        run_result: JobResult | None = None,
        run_exc: Exception | None = None,
        run_delay: float = 0.0,
        respect_cancel: bool = True,
    ) -> None:
        self.parse_valid = parse_valid
        self.run_result = run_result or JobResult(status=JobStatus.QR_READY, artifact_path="/tmp/qr.png")
        self.run_exc = run_exc
        self.run_delay = run_delay
        self.respect_cancel = respect_cancel
        self.run_called_with: list[tuple[Job, Any]] = []

    def get_max_concurrent_key(self) -> str:
        return "ideal.max_concurrent"

    def parse_account_line(self, line: str):
        if not line.strip():
            return AccountLineError(line=line, reason="empty")
        if not self.parse_valid:
            return AccountLineError(line=line, reason="fake reject")
        return ParsedAccount(raw_line=line)

    async def run(self, job: Job, proxy_lease) -> JobResult:
        self.run_called_with.append((job, proxy_lease))
        if self.run_delay > 0:
            end = asyncio.get_event_loop().time() + self.run_delay
            while asyncio.get_event_loop().time() < end:
                if self.respect_cancel and job.cancellation_token.is_cancelled():
                    return JobResult(status=JobStatus.STOPPED)
                await asyncio.sleep(0.005)
        if self.run_exc is not None:
            raise self.run_exc
        return self.run_result


def _make_manager() -> tuple[JobManager, FakeSettings, FakeProxyPool, FakeSse]:
    settings_stub = FakeSettings()
    proxy_pool = FakeProxyPool()
    sse = FakeSse()
    manager = JobManager(settings=settings_stub, proxy_pool=proxy_pool, sse=sse)  # type: ignore[arg-type]
    return manager, settings_stub, proxy_pool, sse


async def _cleanup_manager(manager: JobManager) -> None:
    """Dọn scheduler task + handler task còn treo sau khi test xong để
    tránh warning "task destroyed while pending" khi event loop của
    `asyncio.run()` đóng lại."""
    # Cancel scheduler trước để không tiếp tục spawn handler mới.
    scheduler_task = manager._scheduler_task  # noqa: SLF001
    if scheduler_task is not None:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except (asyncio.CancelledError, Exception):
            pass
    # Dọn handler task còn chạy (nếu có).
    for record in manager.list_jobs():
        task = record.handler_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


# ---------------------------------------------------------------------------
# Property 24
# ---------------------------------------------------------------------------


@given(
    m=st.integers(min_value=1, max_value=4),
    total_jobs=st.integers(min_value=0, max_value=8),
    handler_delay=st.floats(
        min_value=0.005,
        max_value=0.05,
        allow_nan=False,
        allow_infinity=False,
    ),
)
@hyp_settings(deadline=None)
def test_running_count_never_exceeds_max_concurrent_and_no_job_left_behind(
    m: int, total_jobs: int, handler_delay: float
) -> None:
    """Property 24 (Requirement 8.4, 8.5):

    - Trong suốt cửa sổ quan sát (poll `list_jobs()` mỗi 5ms), số job có
      `status == RUNNING` KHÔNG BAO GIỜ vượt `m`.
    - Kết thúc cửa sổ, mọi job trong batch đã đạt `QR_READY` — chứng minh
      không có job pending bị bỏ quên khi có slot trống (Job_Manager tự
      bổ sung job).
    """

    async def _scenario() -> None:
        manager, _, _, _ = _make_manager()
        handler = FakeHandler(run_delay=handler_delay)
        manager.register_handler("ideal", handler)
        await manager.update_max_concurrent(m)

        # Sinh `total_jobs` dòng account hợp lệ (unique cho dễ debug khi
        # counter-example được shrink).
        lines = [f"acc{index:02d}@x|p" for index in range(total_jobs)]
        result = await manager.submit_batch("ideal", lines)
        assert len(result.created_job_ids) == total_jobs
        assert result.skipped == []

        # Cửa sổ quan sát: `handler_delay * total_jobs * 2` (theo task 8.5)
        # đủ để tất cả job chạy xong với biên an toàn 2x; cộng thêm 0.2s
        # floor để tránh race ở example rất nhỏ (`total_jobs<=1`, delay
        # rất bé) khi scheduler cần vài ms để boot.
        poll_seconds = handler_delay * total_jobs * 2 + 0.2
        deadline = asyncio.get_event_loop().time() + poll_seconds

        max_running_observed = 0
        while asyncio.get_event_loop().time() < deadline:
            running_now = sum(
                1
                for record in manager.list_jobs()
                if record.status == JobStatus.RUNNING
            )
            if running_now > max_running_observed:
                max_running_observed = running_now
            # Bất biến 1: không bao giờ vượt `m`.
            assert running_now <= m, (
                f"Vi phạm bất biến concurrency: running={running_now} > m={m}"
                f" (total_jobs={total_jobs}, handler_delay={handler_delay})"
            )
            # Nếu đã có kết quả cuối cho tất cả job — thoát sớm để tiết
            # kiệm thời gian test.
            if all(
                record.status == JobStatus.QR_READY
                for record in manager.list_jobs()
            ) and len(manager.list_jobs()) == total_jobs:
                break
            await asyncio.sleep(0.005)

        # Bất biến 2: không job nào bị bỏ quên — tất cả đều đạt QR_READY
        # trong cửa sổ quan sát (nếu có job nào còn PENDING/RUNNING nghĩa
        # là bị bỏ quên hoặc scheduler không tự bổ sung job).
        final_records = manager.list_jobs()
        assert len(final_records) == total_jobs
        assert all(
            record.status == JobStatus.QR_READY for record in final_records
        ), (
            "Có job không đạt QR_READY trong cửa sổ quan sát → nghi ngờ "
            "scheduler bỏ quên job khi có slot trống. "
            f"Statuses: {[r.status for r in final_records]}"
        )

        # Sanity check bổ sung: max concurrent quan sát được không âm và
        # tuân thủ ngân sách (đã assert trong loop, ghi lại cho debug).
        assert 0 <= max_running_observed <= m

        await _cleanup_manager(manager)

    asyncio.run(_scenario())
