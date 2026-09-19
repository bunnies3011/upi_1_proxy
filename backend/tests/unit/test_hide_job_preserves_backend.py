"""Task 35.7: Unit test ẩn job khỏi UI KHÔNG xoá log/dữ liệu backend.

Requirement 8.7: "WHEN user yêu cầu ẩn 1 IdealJob đã kết thúc (`qr_ready`,
`error`, hoặc `stopped`) khỏi danh sách hiển thị, THE Frontend_App SHALL
không hiển thị job đó nữa trên Single_Screen_UI, nhưng THE Backend_Service
SHALL KHÔNG xoá log/dữ liệu của job đó (giữ nguyên cho mục đích audit,
nhất quán với Requirement 14)."

Kiến trúc quan sát trong code hiện tại:
- Backend KHÔNG có endpoint / API "hide" — hide là hành động client-side
  thuần (Frontend giữ `hiddenJobIds` set trong local state / localStorage).
- Backend KHÔNG có field "hidden" trong `_JobRecord` — không có concept ẩn
  ở tầng backend.

Do đó bằng chứng của R8.7 ở tầng backend là **INVARIANT**: dù frontend đã
"hide" job hay chưa, backend inspection (`job_manager.get_job(job_id)`)
LUÔN trả record đầy đủ log/artifact/error, không bị xoá / không bị filter.
Test này verify:

1. Sau khi có log entries → `record.logs` non-empty.
2. Frontend "hide" là thao tác không đi qua backend — không có API để test
   ở đây gọi. Thay vào đó, test dùng inspection API trực tiếp (`get_job`,
   `list_jobs`) LẶP LẠI nhiều lần → dữ liệu vẫn đầy đủ (invariant).
3. Ngay cả khi job ở trạng thái terminal (qr_ready/error/stopped), record
   PHẢI vẫn còn trong `_jobs` map và `list_jobs()` (đại diện `GET
   /api/jobs/{id}` sẽ trả đủ dữ liệu).

_Requirements: 8.7_
"""

from __future__ import annotations

import time
from typing import Any

from app.core.job_manager import JobManager
from app.core.payment_flow import (
    AccountLineError,
    Job,
    JobResult,
    JobStatus,
    ParsedAccount,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeSettings:
    async def list(self, prefix: str | None = None) -> dict[str, Any]:
        return {}


class _FakeProxyPool:
    async def acquire(
        self,
        job_id: str,  # noqa: ARG002
        *,
        wait_for_release: bool = False,  # noqa: ARG002
        cancellation_token: Any | None = None,  # noqa: ARG002
    ):
        return None

    def release(self, lease) -> None:  # noqa: ARG002
        return None


class _FakeSse:
    def __init__(self) -> None:
        self.status_events: list[dict[str, Any]] = []
        self.log_events: list[dict[str, Any]] = []

    async def broadcast_job_status(self, job_id: str, status: str, **extra) -> None:
        self.status_events.append({"job_id": job_id, "status": status, **extra})

    async def broadcast_job_log(self, job_id: str, message: str, **extra) -> None:
        self.log_events.append({"job_id": job_id, "message": message, **extra})


class _TerminatingHandler:
    """Handler kết thúc ngay với `qr_ready` — dùng để đưa job về terminal state."""

    def get_max_concurrent_key(self) -> str:
        return "ideal.max_concurrent"

    def parse_account_line(self, line: str):
        if not line.strip():
            return AccountLineError(line=line, reason="empty")
        return ParsedAccount(raw_line=line)

    async def run(self, job: Job, proxy_lease):  # noqa: ARG002
        return JobResult(
            status=JobStatus.QR_READY, artifact_path="/tmp/qr-audit-probe.png"
        )


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    """Poll predicate mỗi 5ms cho tới khi True hoặc timeout."""
    import asyncio

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise TimeoutError("predicate did not become true within timeout")


def _make_manager() -> tuple[JobManager, _FakeSse]:
    sse = _FakeSse()
    manager = JobManager(
        settings=_FakeSettings(),  # type: ignore[arg-type]
        proxy_pool=_FakeProxyPool(),  # type: ignore[arg-type]
        sse=sse,  # type: ignore[arg-type]
    )
    return manager, sse


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_backend_has_no_hidden_state_on_job_record() -> None:
    """`_JobRecord` KHÔNG có field `hidden`/`hidden_at`/`is_hidden`.

    Chứng minh backend KHÔNG có concept ẩn — ẩn là view-layer thuần
    (client-side). Nếu tương lai ai đó thêm field hidden vào record, test
    này bắt được ngay: backend không được phép biết đến trạng thái hide.

    Validates: Requirement 8.7.
    """
    manager, _ = _make_manager()
    manager.register_handler("ideal", _TerminatingHandler())  # type: ignore[arg-type]
    result = await manager.submit_batch("ideal", ["audit@x|p"])
    job_id = result.created_job_ids[0]
    record = manager.get_job(job_id)
    assert record is not None

    forbidden_fields = ("hidden", "is_hidden", "hidden_at", "hidden_by_user")
    for name in forbidden_fields:
        assert not hasattr(record, name), (
            f"_JobRecord có field '{name}' — vi phạm R8.7: hide là hành động "
            "view-layer thuần, backend KHÔNG được lưu trạng thái hidden."
        )


async def test_get_job_returns_full_logs_after_terminal_state() -> None:
    """Sau khi job đạt terminal state, `get_job(id)` vẫn trả log đầy đủ.

    Đại diện cho `GET /api/jobs/{id}` (task 23.1 build response từ record).
    Nếu backend từng "xoá log khi hide" thì log sẽ empty — test này bắt.

    Validates: Requirement 8.7.
    """
    manager, sse = _make_manager()
    manager.register_handler("ideal", _TerminatingHandler())  # type: ignore[arg-type]
    result = await manager.submit_batch("ideal", ["audit@x|p"])
    job_id = result.created_job_ids[0]

    # Chèn 1 vài log entries qua API nội bộ để có bằng chứng "log tồn tại".
    await manager._append_log(job_id, "step-1: initiate", detail="probe")  # noqa: SLF001
    await manager._append_log(job_id, "step-2: qr_generated")  # noqa: SLF001

    # Đợi handler chạy xong → job terminal (qr_ready).
    await _wait_until(
        lambda: manager.get_job(job_id) is not None
        and manager.get_job(job_id).status == JobStatus.QR_READY,  # type: ignore[union-attr]
        timeout=2.0,
    )

    record = manager.get_job(job_id)
    assert record is not None
    assert record.status == JobStatus.QR_READY
    assert len(record.logs) >= 2, (
        f"logs bị mất sau khi job terminal — chỉ còn {len(record.logs)} entry. "
        "R8.7: backend PHẢI giữ nguyên log cho mục đích audit."
    )
    # Log content không bị thay đổi
    messages = [entry.get("message") for entry in record.logs]
    assert "step-1: initiate" in messages
    assert "step-2: qr_generated" in messages
    # Artifact cũng phải còn nguyên
    assert record.artifact_path == "/tmp/qr-audit-probe.png"


async def test_repeated_inspection_after_hide_simulation_returns_full_data() -> None:
    """Frontend "hide" không có API backend → mô phỏng bằng cách chỉ đơn
    thuần KHÔNG hiển thị (không gọi API xoá). Backend inspection LẶP LẠI
    vẫn trả record đầy đủ.

    Đây là kiểm chứng invariant "hide là no-op ở backend": không có tương
    tác nào từ Frontend có thể xoá được record khi frontend "hide".

    Validates: Requirement 8.7.
    """
    manager, _ = _make_manager()
    manager.register_handler("ideal", _TerminatingHandler())  # type: ignore[arg-type]
    result = await manager.submit_batch("ideal", ["audit@x|p"])
    job_id = result.created_job_ids[0]

    await manager._append_log(job_id, "audit-log-entry")  # noqa: SLF001

    await _wait_until(
        lambda: manager.get_job(job_id) is not None
        and manager.get_job(job_id).status == JobStatus.QR_READY,  # type: ignore[union-attr]
        timeout=2.0,
    )

    # Frontend "hide" xảy ra ở client — backend không nhận được request nào.
    # Simulate bằng cách chỉ inspect nhiều lần → dữ liệu KHÔNG được giảm.
    snapshots = []
    for _ in range(5):
        record = manager.get_job(job_id)
        assert record is not None
        snapshots.append(
            {
                "status": record.status,
                "log_count": len(record.logs),
                "artifact_path": record.artifact_path,
            }
        )

    # Tất cả snapshot phải giống nhau — record không bị mutate qua các lần
    # inspect (hide=no-op backend).
    first = snapshots[0]
    for i, snap in enumerate(snapshots[1:], start=1):
        assert snap == first, (
            f"Snapshot #{i} khác snapshot #0: {snap} vs {first}. "
            "R8.7: backend inspection PHẢI đồng nhất, không bị mutate qua "
            "chuỗi read-only."
        )

    # `list_jobs()` (đại diện `GET /api/jobs`) vẫn chứa job đó — KHÔNG bị
    # filter ra vì frontend đã hide.
    all_jobs = manager.list_jobs()
    assert any(r.job.job_id == job_id for r in all_jobs), (
        "list_jobs() KHÔNG chứa job đã 'hide' — vi phạm R8.7: backend KHÔNG "
        "được filter hidden vì backend không có concept hidden."
    )


async def test_no_hide_api_on_job_manager() -> None:
    """`JobManager` KHÔNG có method public để "hide" job.

    Nếu tương lai xuất hiện method như `hide()`, `mark_hidden()`,
    `set_hidden_by_user()` → vi phạm R8.7 (backend không được biết đến hide).

    Validates: Requirement 8.7.
    """
    manager, _ = _make_manager()
    forbidden_methods = (
        "hide",
        "hide_job",
        "mark_hidden",
        "set_hidden",
        "set_hidden_by_user",
    )
    for name in forbidden_methods:
        assert not hasattr(manager, name), (
            f"JobManager có method '{name}' — vi phạm R8.7. Backend KHÔNG "
            "được có API 'hide' (hide là view-layer thuần)."
        )
