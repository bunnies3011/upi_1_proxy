"""Smoke test cho 3 fix RAM/bottleneck vừa apply:

1. `_JobRecord.logs` dùng deque(maxlen=500) — vượt cap → pop cũ nhất.
2. `JobManager._cleanup_old_terminal_jobs` xoá job terminal quá TTL và
   job vượt trần cứng theo tuổi cũ nhất.
3. `StripeClient._read_*` cache lần đọc đầu — gọi N lần chỉ query
   `SettingsRepository.get` 1 lần.

Script này thay test tự động, in [PASS]/[FAIL] từng case với chi tiết,
flush realtime để dễ debug nếu treo. KHÔNG dùng pytest — chạy thuần
`python3 test/smoke_job_manager_cleanup_and_logs.py`, exit code = số
case FAIL.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# Ensure `backend/` root on sys.path để import `app.*` như module runtime.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))


def log_step(idx: int, total: int, name: str) -> None:
    print(f"[{idx}/{total}] {name}", flush=True)


def log_result(passed: bool, case_id: str, desc: str, detail: str) -> None:
    tag = "[PASS]" if passed else "[FAIL]"
    print(f"{tag} {case_id} — {desc} :: {detail}", flush=True)


async def case_logs_bounded_deque() -> bool:
    """TC-01: deque(maxlen=500) trong `_JobRecord.logs` giữ đúng 500 entry."""
    from app.core.job_manager import _JobRecord, _MAX_LOG_ENTRIES_PER_JOB
    from app.core.payment_flow import Job, JobStatus, SimpleCancellationToken

    record = _JobRecord(
        job=Job(
            job_id="jobA",
            payment_method="ideal",
            account_line="a|b|c",
            created_at=time.time(),
            cancellation_token=SimpleCancellationToken(),
        ),
        status=JobStatus.RUNNING,
        updated_at=time.time(),
    )

    for i in range(_MAX_LOG_ENTRIES_PER_JOB + 200):
        record.logs.append({"ts": float(i), "message": f"m{i}"})

    ok = (
        len(record.logs) == _MAX_LOG_ENTRIES_PER_JOB
        and record.logs[0]["message"] == f"m{200}"  # cũ nhất bị pop
        and record.logs[-1]["message"] == f"m{_MAX_LOG_ENTRIES_PER_JOB + 199}"
    )
    log_result(
        ok,
        "TC-01",
        "logs deque cap 500 pop-oldest",
        f"len={len(record.logs)} first={record.logs[0]['message']!r} "
        f"last={record.logs[-1]['message']!r}",
    )
    return ok


async def case_cleanup_ttl_and_overflow() -> bool:
    """TC-02: `_cleanup_old_terminal_jobs` xoá job terminal cũ + overflow."""
    from app.core import job_manager as jm_mod
    from app.core.job_manager import JobManager, _JobRecord
    from app.core.payment_flow import Job, JobStatus, SimpleCancellationToken

    # Stub deps của JobManager — không cần logic thật, chỉ cần signature.
    class _StubSettings:
        async def list(self) -> dict:
            return {}

        async def get(self, key: str):  # noqa: ANN001
            return None

    class _StubProxyPool:
        pass

    class _StubSse:
        async def broadcast_job_status(self, *a, **kw) -> None:  # noqa: ANN001
            pass

        async def broadcast_job_log(self, *a, **kw) -> None:  # noqa: ANN001
            pass

    jm = JobManager(
        settings=_StubSettings(),
        proxy_pool=_StubProxyPool(),
        sse=_StubSse(),
        job_repo=None,
    )

    now = time.time()
    ttl = jm_mod._JOB_RETENTION_SECONDS
    max_count = jm_mod._JOB_RETENTION_MAX_COUNT

    # Job PENDING/RUNNING không được đụng — dù updated_at cũ cỡ nào.
    def _mk(job_id: str, status: JobStatus, updated_at: float) -> _JobRecord:
        rec = _JobRecord(
            job=Job(
                job_id=job_id,
                payment_method="ideal",
                account_line="x",
                created_at=updated_at,
                cancellation_token=SimpleCancellationToken(),
            ),
            status=status,
            updated_at=updated_at,
        )
        jm._jobs[job_id] = rec
        return rec

    _mk("keep-pending-old", JobStatus.PENDING, now - ttl - 100.0)
    _mk("keep-running-old", JobStatus.RUNNING, now - ttl - 100.0)
    _mk("keep-terminal-fresh", JobStatus.QR_READY, now - 10.0)
    _mk("drop-terminal-old-1", JobStatus.QR_READY, now - ttl - 1.0)
    _mk("drop-terminal-old-2", JobStatus.ERROR, now - ttl - 500.0)
    _mk("drop-terminal-old-3", JobStatus.STOPPED, now - ttl - 999.0)

    await jm._cleanup_old_terminal_jobs()

    kept = set(jm._jobs.keys())
    expected_kept = {"keep-pending-old", "keep-running-old", "keep-terminal-fresh"}
    ok_ttl = kept == expected_kept
    log_result(
        ok_ttl,
        "TC-02a",
        "TTL: xoá terminal cũ, giữ pending/running/fresh",
        f"kept={sorted(kept)} expected={sorted(expected_kept)}",
    )

    # Kiểm overflow: tạo (max_count + N) job terminal FRESH (không cũ) →
    # cleanup xoá theo updated_at ASC, giữ đúng max_count job mới nhất.
    # Dùng N nhỏ (100) và tạm hạ trần để test chạy nhanh.
    jm._jobs.clear()

    original_max_count = jm_mod._JOB_RETENTION_MAX_COUNT
    jm_mod._JOB_RETENTION_MAX_COUNT = 50  # tạm hạ để test nhanh
    try:
        for i in range(50 + 20):  # dư 20 → xoá 20 cũ nhất
            _mk(f"t{i:03d}", JobStatus.QR_READY, now - 10.0 - (100 - i))  # ASC updated_at

        await jm._cleanup_old_terminal_jobs()

        kept_ids = sorted(jm._jobs.keys())
        # Giữ 50 phần tử có updated_at LỚN NHẤT (mới nhất) → là t020..t069.
        expected = [f"t{i:03d}" for i in range(20, 70)]
        ok_overflow = kept_ids == expected
        log_result(
            ok_overflow,
            "TC-02b",
            "overflow: xoá terminal cũ nhất tới trần cứng",
            f"kept_count={len(kept_ids)} first_kept={kept_ids[0] if kept_ids else None} "
            f"last_kept={kept_ids[-1] if kept_ids else None}",
        )
    finally:
        jm_mod._JOB_RETENTION_MAX_COUNT = original_max_count

    return ok_ttl and ok_overflow


async def case_stripe_config_cache() -> bool:
    """TC-03: `_read_*` chỉ query SettingsRepository.get() 1 lần rồi cache."""
    from app.payments.ideal.stripe_client import StripeClient

    class _CountingSettings:
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        async def get(self, key: str):  # noqa: ANN001
            self.calls[key] = self.calls.get(key, 0) + 1
            return None  # ép fallback default

    class _StubLogger:
        def info(self, *a, **kw) -> None:  # noqa: ANN001
            pass

    class _StubHttp:
        pass

    settings = _CountingSettings()
    client = StripeClient(
        http_client=_StubHttp(),
        settings=settings,
        logger=_StubLogger(),
    )

    # Gọi mỗi method 5 lần — mong đợi mỗi key chỉ query 1 lần.
    for _ in range(5):
        await client._read_max_retry_attempts()
        await client._read_retry_backoff_seconds()
        await client._read_request_timeout_seconds()
        await client._read_refresh_poll_max_attempts()
        await client._read_refresh_poll_delay_seconds()

    ok = all(count == 1 for count in settings.calls.values()) and len(settings.calls) == 5
    log_result(
        ok,
        "TC-03",
        "StripeClient config cache: mỗi key query DB đúng 1 lần cho 5 lần gọi",
        f"call_counts={settings.calls}",
    )
    return ok


async def main() -> int:
    total = 3
    log_step(1, total, "TC-01 _JobRecord.logs deque maxlen")
    ok1 = await case_logs_bounded_deque()

    log_step(2, total, "TC-02 JobManager cleanup TTL + overflow")
    ok2 = await case_cleanup_ttl_and_overflow()

    log_step(3, total, "TC-03 StripeClient config cache per-instance")
    ok3 = await case_stripe_config_cache()

    fails = [name for name, ok in [("TC-01", ok1), ("TC-02", ok2), ("TC-03", ok3)] if not ok]
    if fails:
        print(f"\nFAILED: {', '.join(fails)}", flush=True)
        return len(fails)
    print("\nALL PASS", flush=True)
    return 0


if __name__ == "__main__":
    # Set env cần thiết trước khi import app.* (nếu module top-level đọc env).
    os.environ.setdefault("IDEAL_QR_TOOL_BIND_HOST", "127.0.0.1")
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
