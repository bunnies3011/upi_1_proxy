"""Property test cho `core/job_manager.py::JobManager.submit_batch` (Property 23).

**Property 23: Xử lý batch dòng account bảo toàn tổng số và thứ tự**

Sinh danh sách 0-50 dòng account trộn hợp lệ/không hợp lệ ngẫu nhiên bằng
`hypothesis`, submit qua `JobManager.submit_batch(...)` và assert 3 bất biến:

1. Bảo toàn tổng số dòng:
   `len(created_job_ids) + len(skipped) == len(input_lines)`
2. Thứ tự `created_job_ids` khớp đúng thứ tự các dòng hợp lệ trong `input_lines`
   — verify qua `manager.get_job(job_id).job.account_line`, không dựa vào job_id
   (uuid không có thông tin về vị trí input).
3. Thứ tự `skipped[i]["line"]` khớp đúng thứ tự các dòng không hợp lệ trong
   `input_lines`.

**Validates: Requirements 8.1, 8.2**

Note upper-bound: spec cho phép tới 500 dòng, test giới hạn `max_size=50` để
hypothesis không mất quá nhiều thời gian; ordering bug (nếu có) đã lộ ra ở
kích thước nhỏ. `max_examples=20` được đặt sẵn qua profile 'fast' ở
`tests/conftest.py`, không override lại ở file này.
"""

from __future__ import annotations

import asyncio
from typing import Any

from hypothesis import given, strategies as st

from app.core.job_manager import JobManager
from app.core.payment_flow import (
    AccountLineError,
    Job,
    JobResult,
    JobStatus,
    ParsedAccount,
)


# ---------------------------------------------------------------------------
# Test doubles — minimal, cô lập JobManager khỏi Settings/Proxy/SSE thật
# ---------------------------------------------------------------------------


class _FakeSettings:
    """Stub tối giản cho `SettingsRepository.list()`."""

    async def list(self, prefix: str | None = None) -> dict[str, Any]:
        return {}


class _FakeProxyPool:
    """Direct_Mode (acquire → None) — đủ cho property test này vì FakeHandler
    không quan tâm proxy_lease."""

    async def acquire(self, job_id: str):
        return None

    def release(self, lease) -> None:
        return None


class _FakeSse:
    """Nuốt mọi event — property test không assert về SSE."""

    async def broadcast_job_status(self, job_id: str, status: str, **extra) -> None:
        return None

    async def broadcast_job_log(self, job_id: str, message: str, **extra) -> None:
        return None


class _FakeHandler:
    """Handler implement `PaymentFlowHandler` với logic parse deterministic:

    - Dòng bắt đầu bằng ``"OK|"`` → `ParsedAccount(raw_line=line)` (hợp lệ).
    - Ngược lại → `AccountLineError(line=line, reason="not_ok")` (không hợp lệ).

    `run` no-op trả `JobResult(status=QR_READY)` ngay — Property 23 chỉ quan
    tâm giá trị `BatchSubmitResult` do `submit_batch` trả về, không quan tâm
    chi tiết execution của job.
    """

    def get_max_concurrent_key(self) -> str:
        return "ideal.max_concurrent"

    def parse_account_line(self, line: str):
        if line.startswith("OK|"):
            return ParsedAccount(raw_line=line)
        return AccountLineError(line=line, reason="not_ok")

    async def run(self, job: Job, proxy_lease) -> JobResult:
        return JobResult(status=JobStatus.QR_READY)


async def _cleanup_manager(manager: JobManager) -> None:
    """Cancel scheduler + handler task còn lại để `asyncio.run` không warn về
    pending task khi event loop đóng.

    `submit_batch` với ít nhất 1 dòng hợp lệ sẽ spawn scheduler task chạy
    `while True` — nếu không cancel, task đó vẫn pending khi test thoát.
    """
    scheduler_task = manager._scheduler_task  # noqa: SLF001
    if scheduler_task is not None and not scheduler_task.done():
        scheduler_task.cancel()
        try:
            await scheduler_task
        except (asyncio.CancelledError, Exception):
            pass

    for record in manager.list_jobs():
        handler_task = record.handler_task
        if handler_task is not None and not handler_task.done():
            handler_task.cancel()
            try:
                await handler_task
            except (asyncio.CancelledError, Exception):
                pass


# ---------------------------------------------------------------------------
# Strategy — sinh dòng: hoặc "OK|<int>" (hợp lệ, unique-ish content), hoặc
# text bất kỳ không bắt đầu bằng "OK|" (không hợp lệ). Nội dung khác nhau
# giữa các dòng hợp lệ giúp phát hiện bug ordering thay vì chỉ đếm số.
# ---------------------------------------------------------------------------


_valid_line = st.integers(min_value=0, max_value=99_999).map(lambda i: f"OK|{i}")
_invalid_line = st.text(max_size=20).filter(lambda s: not s.startswith("OK|"))
_line_strategy = st.one_of(_valid_line, _invalid_line)


@given(lines=st.lists(_line_strategy, min_size=0, max_size=50))
def test_submit_batch_preserves_count_and_order(lines: list[str]) -> None:
    async def _main() -> None:
        manager = JobManager(
            settings=_FakeSettings(),  # type: ignore[arg-type]
            proxy_pool=_FakeProxyPool(),  # type: ignore[arg-type]
            sse=_FakeSse(),  # type: ignore[arg-type]
        )
        manager.register_handler("ideal", _FakeHandler())
        try:
            result = await manager.submit_batch("ideal", lines)

            # Phân tách theo cùng luật của _FakeHandler.parse_account_line.
            expected_valid = [line for line in lines if line.startswith("OK|")]
            expected_invalid = [line for line in lines if not line.startswith("OK|")]

            # Bất biến 1: bảo toàn tổng số.
            assert len(result.created_job_ids) + len(result.skipped) == len(lines), (
                f"count mismatch: created={len(result.created_job_ids)} + "
                f"skipped={len(result.skipped)} != input={len(lines)}"
            )
            # Số job hợp lệ / bỏ qua khớp phân loại kỳ vọng.
            assert len(result.created_job_ids) == len(expected_valid)
            assert len(result.skipped) == len(expected_invalid)

            # Bất biến 2: thứ tự created_job_ids khớp thứ tự dòng hợp lệ trong input.
            # Verify qua account_line lưu trong Job (job_id là uuid nên không mang
            # thông tin vị trí — phải tra lại record để so sánh).
            job_account_lines_in_order = [
                manager.get_job(jid).job.account_line  # type: ignore[union-attr]
                for jid in result.created_job_ids
            ]
            assert job_account_lines_in_order == expected_valid, (
                "created_job_ids không theo đúng thứ tự các dòng hợp lệ"
            )

            # Bất biến 3: thứ tự skipped khớp thứ tự dòng không hợp lệ trong input.
            skipped_lines_in_order = [row["line"] for row in result.skipped]
            assert skipped_lines_in_order == expected_invalid, (
                "skipped không theo đúng thứ tự các dòng không hợp lệ"
            )
        finally:
            await _cleanup_manager(manager)

    asyncio.run(_main())
