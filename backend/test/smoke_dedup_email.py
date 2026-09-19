#!/usr/bin/env python3
"""Smoke test: 1 email = 1 job trong JobManager.submit_batch.

Kịch bản:
1. Submit lần 1 với `a@x|pw1` → tạo job mới, capture job_id.
2. Submit lần 2 với cùng email `a@x|pw2` khi job ở trạng thái ERROR
   (giả lập bằng cách set trực tiếp status) → phải REUSE cùng job_id,
   status reset về PENDING, account_line update.
3. Submit lần 3 khi job đang RUNNING → phải SKIP với reason job_already_active.
4. Submit lần 4 với email khác `b@x|pw` → tạo job mới, khác job_id.

Chạy: ./.venv/bin/python3 test/smoke_dedup_email.py từ backend/.
In [PASS]/[FAIL] mỗi test case ngay khi xong (không gom cuối).
"""
from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.job_manager import JobManager  # noqa: E402
from app.core.payment_flow import (  # noqa: E402
    AccountLineError,
    Job,
    JobResult,
    JobStatus,
    ParsedAccount,
)
from app.core.proxy_pool import ProxyLease  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes — settings/proxy/sse rỗng để cô lập test dedup logic.
# ---------------------------------------------------------------------------


class _FakeSettings:
    async def list(self) -> dict:
        return {}


class _FakeProxy:
    async def acquire(self, job_id: str):
        return None

    def release(self, lease):  # noqa: ARG002
        return None


class _FakeSse:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def broadcast_job_status(self, job_id: str, status: str, **extra) -> None:
        self.events.append(("status", job_id, {"status": status, **extra}))

    async def broadcast_job_log(self, *a, **kw) -> None:  # noqa: ARG002
        return None


class _FakeParsed(ParsedAccount):
    def __init__(self, raw_line: str, email: str) -> None:
        # ParsedAccount là frozen dataclass; kế thừa cần dùng __init__ đơn giản.
        object.__setattr__(self, "raw_line", raw_line)
        object.__setattr__(self, "email", email)


class _FakeHandler:
    """Parse `email|password` → _FakeParsed. `run` giữ trạng thái RUNNING đủ lâu
    để test case #3 (submit khi đang running) chạy được, nhưng finish nhanh
    ở test case #2 (đã ERROR nên submit_batch reset)."""

    def __init__(self) -> None:
        self.run_should_hang = False
        self.run_started = asyncio.Event()

    def parse_account_line(self, line: str):
        stripped = line.strip()
        if "|" not in stripped:
            return AccountLineError(line=line, reason="missing_delimiter")
        email = stripped.split("|", 1)[0].strip().lower()
        if "@" not in email:
            return AccountLineError(line=line, reason="invalid_email")
        return _FakeParsed(stripped, email)

    def get_account_dedup_key(self, parsed: ParsedAccount) -> str | None:
        return getattr(parsed, "email", None)

    def get_max_concurrent_key(self) -> str:
        return "ideal.max_concurrent"

    async def run(self, job: Job, proxy_lease):  # noqa: ARG002
        self.run_started.set()
        if self.run_should_hang:
            # Hang cho tới khi cancel → giữ trạng thái RUNNING để case #3
            # có thể quan sát behavior "job_already_active".
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return JobResult(status=JobStatus.STOPPED)
        return JobResult(status=JobStatus.QR_READY, artifact_path="/tmp/fake.png")


async def _main() -> int:
    settings = _FakeSettings()
    proxy = _FakeProxy()
    sse = _FakeSse()

    manager = JobManager(settings=settings, proxy_pool=proxy, sse=sse)
    handler = _FakeHandler()
    manager.register_handler("ideal", handler)

    failed = 0

    # -------------------------------------------------------------------
    # TC-01: submit lần 1 với email `a@x|pw1` → tạo job mới.
    # -------------------------------------------------------------------
    tc = "TC-01"
    try:
        result = await manager.submit_batch("ideal", ["a@x|pw1"])
        assert len(result.created_job_ids) == 1, "phải tạo 1 job mới"
        assert len(result.skipped) == 0
        job1_id = result.created_job_ids[0]
        rec1 = manager.get_job(job1_id)
        assert rec1 is not None
        assert rec1.status == JobStatus.PENDING
        assert rec1.dedup_key == "ideal:a@x"
        print(f"[PASS] {tc} — tạo job mới cho email `a@x`, id={job1_id[:8]}…", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {tc} — {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        failed += 1
        return failed

    # Force terminal state ERROR để test case reuse (không đợi scheduler chạy).
    rec1.status = JobStatus.ERROR
    rec1.error_code = "test_forced_error"
    # Xóa khỏi pending queue (scheduler có thể đã pick job này, không sao,
    # chỉ cần state là ERROR để reuse logic kick in).
    try:
        manager._pending_order.remove(job1_id)  # type: ignore[attr-defined]
    except ValueError:
        pass

    # -------------------------------------------------------------------
    # TC-02: submit lần 2 cùng email `a@x|pw2` → REUSE job1_id, reset về PENDING,
    #        account_line update thành `a@x|pw2`.
    # -------------------------------------------------------------------
    tc = "TC-02"
    try:
        result = await manager.submit_batch("ideal", ["a@x|pw2"])
        assert len(result.created_job_ids) == 1, "phải trả job_id cũ (reused)"
        assert result.created_job_ids[0] == job1_id, "job_id phải giữ nguyên"
        assert len(result.skipped) == 0

        rec = manager.get_job(job1_id)
        assert rec is not None
        assert rec.status == JobStatus.PENDING, f"phải reset PENDING, got {rec.status}"
        assert rec.job.account_line == "a@x|pw2", "account_line phải update"
        assert rec.error_code is None, "error_code phải clear"
        assert rec.error_message is None
        assert rec.dedup_key == "ideal:a@x"
        print(f"[PASS] {tc} — REUSE cùng job_id, reset PENDING, update account_line", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {tc} — {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        failed += 1

    # -------------------------------------------------------------------
    # TC-03: submit khi job đang RUNNING → SKIP với reason `job_already_active`.
    # -------------------------------------------------------------------
    tc = "TC-03"
    try:
        rec = manager.get_job(job1_id)
        assert rec is not None
        # Force RUNNING trực tiếp.
        rec.status = JobStatus.RUNNING
        try:
            manager._pending_order.remove(job1_id)  # type: ignore[attr-defined]
        except ValueError:
            pass

        result = await manager.submit_batch("ideal", ["a@x|pw3"])
        assert len(result.created_job_ids) == 0, "không tạo job mới khi đã running"
        assert len(result.skipped) == 1
        assert result.skipped[0]["reason"] == "job_already_active"
        assert result.skipped[0]["line"] == "a@x|pw3"

        rec = manager.get_job(job1_id)
        assert rec is not None
        assert rec.status == JobStatus.RUNNING, "job đang running không bị reset"
        assert rec.job.account_line == "a@x|pw2", "account_line không đổi"
        print(f"[PASS] {tc} — SKIP với job_already_active, không reset running job", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {tc} — {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        failed += 1

    # -------------------------------------------------------------------
    # TC-04: submit email khác `b@x|pw` → tạo job MỚI, khác job_id.
    # -------------------------------------------------------------------
    tc = "TC-04"
    try:
        result = await manager.submit_batch("ideal", ["b@x|pw"])
        assert len(result.created_job_ids) == 1
        job2_id = result.created_job_ids[0]
        assert job2_id != job1_id, "email khác phải tạo job_id khác"
        rec2 = manager.get_job(job2_id)
        assert rec2 is not None
        assert rec2.dedup_key == "ideal:b@x"
        print(f"[PASS] {tc} — email khác `b@x` tạo job mới, id={job2_id[:8]}…", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {tc} — {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        failed += 1

    # -------------------------------------------------------------------
    # TC-05: batch [a@x|pw, a@x|pw] (2 dòng cùng email trong 1 batch) khi
    # job cũ đã terminal (STOPPED) → dòng đầu reset job, dòng sau skip
    # (job đã pending từ dòng đầu).
    # -------------------------------------------------------------------
    tc = "TC-05"
    try:
        # Reset job1 về STOPPED để test.
        rec = manager.get_job(job1_id)
        assert rec is not None
        rec.status = JobStatus.STOPPED
        try:
            manager._pending_order.remove(job1_id)  # type: ignore[attr-defined]
        except ValueError:
            pass

        result = await manager.submit_batch("ideal", ["a@x|pwA", "a@x|pwB"])
        assert len(result.created_job_ids) == 1, "chỉ 1 lần reset"
        assert result.created_job_ids[0] == job1_id
        assert len(result.skipped) == 1
        assert result.skipped[0]["reason"] == "job_already_active"
        assert result.skipped[0]["line"] == "a@x|pwB"

        rec = manager.get_job(job1_id)
        assert rec is not None
        assert rec.job.account_line == "a@x|pwA", "chỉ dòng đầu update account_line"
        print(f"[PASS] {tc} — batch 2 dòng cùng email: reset 1 lần + skip lần 2", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {tc} — {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        failed += 1

    # -------------------------------------------------------------------
    # TC-06: delete job → dedup_index cũng clear → email đó tạo job mới được.
    # -------------------------------------------------------------------
    tc = "TC-06"
    try:
        deleted = await manager.delete_job(job1_id)
        assert deleted is True

        # Submit lại email `a@x` → phải tạo job mới (job1_id đã bị xóa).
        result = await manager.submit_batch("ideal", ["a@x|pwNew"])
        assert len(result.created_job_ids) == 1
        new_id = result.created_job_ids[0]
        assert new_id != job1_id
        print(f"[PASS] {tc} — sau delete, email `a@x` tạo job mới, id={new_id[:8]}…", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {tc} — {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        failed += 1

    print(
        f"\n== Done: {6 - failed}/6 PASS ==",
        flush=True,
    )
    return failed


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main())
