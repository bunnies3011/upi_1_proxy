"""After QR_READY, JobManager polls check_plan until plus or 5 min timeout."""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.job_manager import JobManager
from app.core.payment_flow import (
    AccountLineError,
    Job,
    JobResult,
    JobStatus,
    ParsedAccount,
)


class _FakeSettings:
    def __init__(self, snapshot: dict[str, Any] | None = None) -> None:
        self._snapshot = snapshot or {"ideal.max_concurrent": 2}

    async def list(self, prefix: str | None = None) -> dict[str, Any]:
        return dict(self._snapshot)

    async def get(self, key: str) -> Any | None:
        return self._snapshot.get(key)


class _FakeProxyPool:
    def __init__(self) -> None:
        from app.core.proxy_health import ProbeConfig

        self.probe_config = ProbeConfig(
            enabled=False, fallback_direct_on_exhausted=False
        )

    async def acquire(self, job_id: str, **kwargs):
        return None

    def release(self, lease) -> None:
        return None


class _FakeSse:
    async def broadcast_job_status(self, job_id: str, status: str, **extra) -> None:
        return None

    async def broadcast_job_log(self, job_id: str, message: str, **extra) -> None:
        return None


class _HandlerPollPlan:
    """First N checks return free, then plus — exercises poll loop."""

    def __init__(self, free_then_plus_after: int = 2) -> None:
        self.free_then_plus_after = free_then_plus_after
        self.check_plan_calls: list[str] = []

    def get_max_concurrent_key(self) -> str:
        return "ideal.max_concurrent"

    def parse_account_line(self, line: str):
        if not line.strip():
            return AccountLineError(line=line, reason="empty")
        return ParsedAccount(raw_line=line)

    async def run(self, job: Job, proxy_lease) -> JobResult:
        return JobResult(status=JobStatus.QR_READY, artifact_path="/tmp/qr.png")

    async def check_plan_status(self, job: Job) -> dict[str, Any]:
        self.check_plan_calls.append(job.job_id)
        if len(self.check_plan_calls) < self.free_then_plus_after:
            return {"plan": "free", "email": "a@b.c"}
        return {"plan": "plus", "email": "a@b.c"}


async def test_qr_ready_polls_until_plus() -> None:
    handler = _HandlerPollPlan(free_then_plus_after=3)
    manager = JobManager(
        settings=_FakeSettings(),  # type: ignore[arg-type]
        proxy_pool=_FakeProxyPool(),  # type: ignore[arg-type]
        sse=_FakeSse(),  # type: ignore[arg-type]
    )
    # Fast poll for unit test (still exercise multi-attempt loop).
    manager._AUTO_CHECK_PLAN_TIMEOUT_SECONDS = 5.0
    manager._AUTO_CHECK_PLAN_INTERVAL_SECONDS = 0.05
    manager.register_handler("ideal", handler)  # type: ignore[arg-type]

    result = await manager.submit_batch("ideal", ["user@example.com|pw"])
    job_id = result.created_job_ids[0]

    deadline = asyncio.get_event_loop().time() + 3.0
    while asyncio.get_event_loop().time() < deadline:
        record = manager.get_job(job_id)
        if (
            record is not None
            and record.status == JobStatus.QR_READY
            and record.plan == "plus"
            and len(handler.check_plan_calls) >= 3
        ):
            break
        await asyncio.sleep(0.01)
    else:
        record = manager.get_job(job_id)
        raise AssertionError(
            f"poll did not reach plus: plan={getattr(record, 'plan', None)} "
            f"calls={len(handler.check_plan_calls)}"
        )

    assert record is not None
    assert record.plan == "plus"
    assert len(handler.check_plan_calls) >= 3
    assert all(c == job_id for c in handler.check_plan_calls)


async def test_qr_ready_poll_stops_on_timeout() -> None:
    handler = _HandlerPollPlan(free_then_plus_after=999)
    manager = JobManager(
        settings=_FakeSettings(),  # type: ignore[arg-type]
        proxy_pool=_FakeProxyPool(),  # type: ignore[arg-type]
        sse=_FakeSse(),  # type: ignore[arg-type]
    )
    manager._AUTO_CHECK_PLAN_TIMEOUT_SECONDS = 0.15
    manager._AUTO_CHECK_PLAN_INTERVAL_SECONDS = 0.05
    manager.register_handler("ideal", handler)  # type: ignore[arg-type]

    result = await manager.submit_batch("ideal", ["user2@example.com|pw"])
    job_id = result.created_job_ids[0]

    # Wait past timeout + a little slack.
    await asyncio.sleep(0.4)

    record = manager.get_job(job_id)
    assert record is not None
    assert record.status == JobStatus.QR_READY
    # Never reached plus — last successful persist is free.
    assert record.plan == "free"
    assert len(handler.check_plan_calls) >= 2
    # Poll task finished (timeout path).
    tasks = getattr(manager, "_auto_check_plan_tasks", {})
    assert job_id not in tasks or tasks[job_id].done()
