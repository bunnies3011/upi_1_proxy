"""Unit tests: per-method concurrency limiters + explicit lease health + proxy mask.

Phase 1 Session 1a — core foundations (AC-3..AC-8). Does NOT register upi_direct.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import pytest

from app.core.errors import ProxyExhaustedError
from app.core.job_manager import JobManager
from app.core.payment_flow import (
    AccountLineError,
    Job,
    JobResult,
    JobStatus,
    ParsedAccount,
    ProxyLeaseHealth,
)
from app.core.proxy_format import safe_proxy_id


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeSettings:
    def __init__(self, snapshot: dict[str, Any] | None = None) -> None:
        self._snapshot = snapshot or {}

    async def list(self, prefix: str | None = None) -> dict[str, Any]:
        return dict(self._snapshot)

    async def get(self, key: str) -> Any | None:
        return self._snapshot.get(key)


@dataclass
class FakeLease:
    proxy_id: str
    materialized_url: str
    leased_at: float


class FakeProxyPool:
    def __init__(
        self,
        proxy_id: str = "raw-secret-proxy-id-42",
        *,
        host: str = "proxy.example.test",
    ) -> None:
        self.proxy_id = proxy_id
        self.host = host
        self.acquired_for: list[str] = []
        self.released: list[str] = []
        self.alive_marks: list[str] = []
        self.dead_marks: list[str] = []
        self.mode: str = "direct"
        from app.core.proxy_health import ProbeConfig

        self.probe_config = ProbeConfig(
            enabled=False, fallback_direct_on_exhausted=False
        )

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
                proxy_id=self.proxy_id,
                # Host deliberately ≠ proxy_id so mask_proxy host residual
                # is not confused with the raw identifier under test.
                materialized_url=f"http://user:pass@{self.host}:8080",
                leased_at=time.time(),
            )
        if self.mode == "exhausted":
            raise ProxyExhaustedError(
                total_proxies=1, dead_count=1, leased_out_count=0
            )
        raise RuntimeError(f"unknown mode {self.mode}")

    def release(self, lease) -> None:
        if lease is not None:
            self.released.append(lease.proxy_id)

    def mark_alive(self, proxy_id: str) -> None:
        self.alive_marks.append(proxy_id)

    def mark_dead(self, proxy_id: str) -> None:
        self.dead_marks.append(proxy_id)


class FakeSse:
    def __init__(self) -> None:
        self.status_events: list[dict[str, Any]] = []
        self.log_events: list[dict[str, Any]] = []

    async def broadcast_job_status(self, job_id: str, status: str, **extra) -> None:
        self.status_events.append({"job_id": job_id, "status": status, **extra})

    async def broadcast_job_log(self, job_id: str, message: str, **extra) -> None:
        self.log_events.append({"job_id": job_id, "message": message, **extra})


class MethodHandler:
    """Handler bound to a payment method + settings key."""

    def __init__(
        self,
        method: str,
        *,
        run_result: JobResult | None = None,
        run_exc: Exception | None = None,
        run_delay: float = 0.0,
        respect_cancel: bool = True,
        gate: asyncio.Event | None = None,
        started: asyncio.Event | None = None,
    ) -> None:
        self.method = method
        self.run_result = run_result or JobResult(
            status=JobStatus.QR_READY, artifact_path="/tmp/qr.png"
        )
        self.run_exc = run_exc
        self.run_delay = run_delay
        self.respect_cancel = respect_cancel
        self.gate = gate
        self.started = started
        self.run_count = 0
        self.run_called_with: list[tuple[Job, Any]] = []

    def get_max_concurrent_key(self) -> str:
        return f"{self.method}.max_concurrent"

    def parse_account_line(self, line: str):
        if not line.strip():
            return AccountLineError(line=line, reason="empty")
        return ParsedAccount(raw_line=line)

    async def run(self, job: Job, proxy_lease) -> JobResult:
        self.run_count += 1
        self.run_called_with.append((job, proxy_lease))
        if self.started is not None:
            self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.run_delay > 0:
            end = asyncio.get_event_loop().time() + self.run_delay
            while asyncio.get_event_loop().time() < end:
                if self.respect_cancel and job.cancellation_token.is_cancelled():
                    return JobResult(status=JobStatus.STOPPED)
                await asyncio.sleep(0.005)
        if self.run_exc is not None:
            raise self.run_exc
        return self.run_result


def _make_manager(
    snapshot: dict[str, Any] | None = None,
    proxy_mode: str = "direct",
    proxy_id: str = "raw-secret-proxy-id-42",
) -> tuple[JobManager, FakeSettings, FakeProxyPool, FakeSse]:
    settings = FakeSettings(snapshot=snapshot)
    proxy_pool = FakeProxyPool(proxy_id=proxy_id)
    proxy_pool.mode = proxy_mode
    sse = FakeSse()
    manager = JobManager(settings=settings, proxy_pool=proxy_pool, sse=sse)  # type: ignore[arg-type]
    return manager, settings, proxy_pool, sse


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise TimeoutError("predicate did not become true within timeout")


def _running_for(manager: JobManager, method: str) -> int:
    return sum(
        1
        for r in manager.list_jobs()
        if r.status == JobStatus.RUNNING and r.job.payment_method == method
    )


# ---------------------------------------------------------------------------
# AC-3: head-of-line — saturated method A does not block method B
# ---------------------------------------------------------------------------


async def test_saturated_method_does_not_head_of_line_block_other() -> None:
    """AC-3: method A at capacity; eligible method B still dispatches."""
    manager, *_ = _make_manager()
    gate_a = asyncio.Event()
    started_b = asyncio.Event()
    handler_a = MethodHandler("method_a", gate=gate_a)
    handler_b = MethodHandler("method_b", started=started_b, run_delay=0.01)
    manager.register_handler("method_a", handler_a)
    manager.register_handler("method_b", handler_b)
    await manager.update_method_max_concurrent("method_a", 1)
    await manager.update_method_max_concurrent("method_b", 1)

    # Fill method_a (limit 1) with a long-running job.
    await manager.submit_batch("method_a", ["a1@x|p"])
    await _wait_until(lambda: _running_for(manager, "method_a") == 1)

    # Queue another method_a job (will sit pending — A saturated), then B.
    await manager.submit_batch("method_a", ["a2@x|p"])
    await manager.submit_batch("method_b", ["b1@x|p"])

    # B must start even though A has a pending job ahead in global FIFO.
    await asyncio.wait_for(started_b.wait(), timeout=1.0)
    assert handler_b.run_count >= 1
    assert _running_for(manager, "method_a") == 1

    gate_a.set()
    await _wait_until(
        lambda: all(r.status == JobStatus.QR_READY for r in manager.list_jobs()),
        timeout=2.0,
    )


async def test_two_methods_run_up_to_own_limits_concurrently() -> None:
    """AC-3: distinct max_concurrent values apply per method."""
    manager, *_ = _make_manager()
    gate = asyncio.Event()
    handler_a = MethodHandler("method_a", gate=gate)
    handler_b = MethodHandler("method_b", gate=gate)
    manager.register_handler("method_a", handler_a)
    manager.register_handler("method_b", handler_b)
    await manager.update_method_max_concurrent("method_a", 2)
    await manager.update_method_max_concurrent("method_b", 1)

    await manager.submit_batch("method_a", ["a1@x|p", "a2@x|p", "a3@x|p"])
    await manager.submit_batch("method_b", ["b1@x|p", "b2@x|p"])

    await _wait_until(
        lambda: _running_for(manager, "method_a") == 2
        and _running_for(manager, "method_b") == 1,
        timeout=1.0,
    )
    # Hard caps: never exceed per-method limits while gates hold.
    await asyncio.sleep(0.05)
    assert _running_for(manager, "method_a") == 2
    assert _running_for(manager, "method_b") == 1

    gate.set()
    await _wait_until(
        lambda: all(r.status == JobStatus.QR_READY for r in manager.list_jobs()),
        timeout=2.0,
    )


# ---------------------------------------------------------------------------
# AC-4: update_method / apply_from_settings scoped to one method
# ---------------------------------------------------------------------------


async def test_update_method_max_concurrent_only_affects_target() -> None:
    """AC-4: changing method_a limit leaves method_b limiter untouched."""
    manager, *_ = _make_manager()
    manager.register_handler("method_a", MethodHandler("method_a"))
    manager.register_handler("method_b", MethodHandler("method_b"))
    await manager.update_method_max_concurrent("method_a", 3)
    await manager.update_method_max_concurrent("method_b", 5)

    lim_b_before = manager._method_limiters["method_b"]  # noqa: SLF001
    b_max_before = lim_b_before.configured_max
    b_id_before = id(lim_b_before)

    await manager.update_method_max_concurrent("method_a", 7)

    assert manager._method_limiters["method_a"].configured_max == 7  # noqa: SLF001
    lim_b_after = manager._method_limiters["method_b"]  # noqa: SLF001
    assert lim_b_after.configured_max == b_max_before == 5
    assert id(lim_b_after) == b_id_before


async def test_apply_max_concurrent_from_settings_is_per_method() -> None:
    """AC-4: settings apply writes each method's own key, no last-write-wins."""
    snapshot = {
        "method_a.max_concurrent": 4,
        "method_b.max_concurrent": 2,
    }
    manager, settings, *_ = _make_manager(snapshot=snapshot)
    manager.register_handler("method_a", MethodHandler("method_a"))
    manager.register_handler("method_b", MethodHandler("method_b"))

    await manager.apply_max_concurrent_from_settings()

    assert manager._method_limiters["method_a"].configured_max == 4  # noqa: SLF001
    assert manager._method_limiters["method_b"].configured_max == 2  # noqa: SLF001

    # Changing only method_a setting + maybe_reload must not clobber b.
    settings._snapshot["method_a.max_concurrent"] = 9  # noqa: SLF001
    await manager.maybe_reload_concurrency(["method_a.max_concurrent"])
    assert manager._method_limiters["method_a"].configured_max == 9  # noqa: SLF001
    assert manager._method_limiters["method_b"].configured_max == 2  # noqa: SLF001


# ---------------------------------------------------------------------------
# AC-5: slot released once on every exit path
# ---------------------------------------------------------------------------


async def test_method_slot_released_on_success_exception_cancel_proxy_fail() -> None:
    """AC-5: success / handler exception / user cancel / proxy fail release once."""
    manager, _, proxy_pool, _ = _make_manager(proxy_mode="direct")

    # success
    h_ok = MethodHandler(
        "m",
        run_result=JobResult(status=JobStatus.QR_READY, artifact_path="/t.png"),
    )
    manager.register_handler("m", h_ok)
    await manager.update_method_max_concurrent("m", 1)
    r1 = await manager.submit_batch("m", ["ok@x|p"])
    j1 = r1.created_job_ids[0]
    await _wait_until(lambda: manager.get_job(j1).status == JobStatus.QR_READY)  # type: ignore[union-attr]
    assert manager._method_limiters["m"].in_use == 0  # noqa: SLF001

    # handler exception
    h_exc = MethodHandler("m2", run_exc=RuntimeError("boom"))
    manager.register_handler("m2", h_exc)
    await manager.update_method_max_concurrent("m2", 1)
    r2 = await manager.submit_batch("m2", ["ex@x|p"])
    j2 = r2.created_job_ids[0]
    await _wait_until(lambda: manager.get_job(j2).status == JobStatus.ERROR)  # type: ignore[union-attr]
    assert manager._method_limiters["m2"].in_use == 0  # noqa: SLF001

    # user cancel while running
    h_slow = MethodHandler("m3", run_delay=5.0, respect_cancel=True)
    manager.register_handler("m3", h_slow)
    await manager.update_method_max_concurrent("m3", 1)
    r3 = await manager.submit_batch("m3", ["slow@x|p"])
    j3 = r3.created_job_ids[0]
    await _wait_until(lambda: manager.get_job(j3).status == JobStatus.RUNNING)  # type: ignore[union-attr]
    await manager.stop(j3)
    await _wait_until(lambda: manager.get_job(j3).status == JobStatus.STOPPED)  # type: ignore[union-attr]
    assert manager._method_limiters["m3"].in_use == 0  # noqa: SLF001

    # proxy acquisition failure
    proxy_pool.mode = "exhausted"
    h_px = MethodHandler("m4")
    manager.register_handler("m4", h_px)
    await manager.update_method_max_concurrent("m4", 1)
    r4 = await manager.submit_batch("m4", ["px@x|p"])
    j4 = r4.created_job_ids[0]
    await _wait_until(
        lambda: manager.get_job(j4).status == JobStatus.ERROR  # type: ignore[union-attr]
    )
    assert manager.get_job(j4).error_code == "proxy_exhausted"  # type: ignore[union-attr]
    assert manager._method_limiters["m4"].in_use == 0  # noqa: SLF001

    # After releases, methods can dispatch again (no leak / no stuck 0-capacity).
    # Use a fresh quick handler on m3 (old one is still 5s delay) and restore
    # proxy pool for m4.
    proxy_pool.mode = "direct"
    h_slow.run_delay = 0.0
    h_exc.run_exc = None  # m2 second run should succeed quickly
    for method in ("m", "m2", "m3", "m4"):
        result = await manager.submit_batch(method, [f"again-{method}@x|p"])
        jid = result.created_job_ids[0]
        await _wait_until(
            lambda jid=jid: manager.get_job(jid).status  # type: ignore[union-attr]
            in (JobStatus.QR_READY, JobStatus.ERROR, JobStatus.STOPPED),
            timeout=2.0,
        )
        assert manager._method_limiters[method].in_use == 0  # noqa: SLF001


# ---------------------------------------------------------------------------
# AC-6: over-budget when lowering live limit
# ---------------------------------------------------------------------------


async def test_lower_method_limit_over_budget_then_raise() -> None:
    """AC-6: lower limit below running → over-budget; raise frees capacity."""
    manager, *_ = _make_manager()
    gate = asyncio.Event()
    handler = MethodHandler("m", gate=gate)
    manager.register_handler("m", handler)
    await manager.update_method_max_concurrent("m", 3)

    await manager.submit_batch("m", ["a@x|p", "b@x|p", "c@x|p"])
    await _wait_until(lambda: _running_for(manager, "m") == 3)

    await manager.update_method_max_concurrent("m", 1)
    lim = manager._method_limiters["m"]  # noqa: SLF001
    assert lim.configured_max == 1
    assert lim.over_budget_slots == 2
    assert lim.in_use == 3
    # Still no free capacity for a 4th job.
    await manager.submit_batch("m", ["d@x|p"])
    await asyncio.sleep(0.05)
    assert _running_for(manager, "m") == 3

    # Let one finish → over_budget decrements, still no new start (need 2 more).
    gate.set()
    # Re-hold remaining: use a new gate for next wave by waiting terminals.
    # Actually all three pass the same gate once set — they all finish.
    await _wait_until(
        lambda: all(
            r.status == JobStatus.QR_READY
            for r in manager.list_jobs()
            if r.job.account_line.startswith(("a@", "b@", "c@"))
        ),
        timeout=2.0,
    )
    # in_use should be 0 or 1 (d may have started)
    await _wait_until(
        lambda: manager.get_job(  # type: ignore[union-attr]
            next(
                r.job.job_id
                for r in manager.list_jobs()
                if r.job.account_line.startswith("d@")
            )
        ).status
        in (JobStatus.QR_READY, JobStatus.RUNNING),
        timeout=2.0,
    )

    # Raise limit while idle-ish and confirm capacity works.
    await manager.update_method_max_concurrent("m", 2)
    assert manager._method_limiters["m"].configured_max == 2  # noqa: SLF001
    gate2 = asyncio.Event()
    handler.gate = gate2
    await manager.submit_batch("m", ["e@x|p", "f@x|p", "g@x|p"])
    await _wait_until(lambda: _running_for(manager, "m") == 2, timeout=1.5)
    await asyncio.sleep(0.03)
    assert _running_for(manager, "m") <= 2
    gate2.set()
    await _wait_until(
        lambda: all(r.status == JobStatus.QR_READY for r in manager.list_jobs()),
        timeout=3.0,
    )


# ---------------------------------------------------------------------------
# AC-7: proxy_acquired masks raw proxy_id
# ---------------------------------------------------------------------------


async def test_proxy_acquired_log_masks_raw_proxy_id() -> None:
    """AC-7: proxy_acquired has masked id, never raw lease.proxy_id."""
    raw_id = "raw-secret-proxy-id-42"
    manager, _, proxy_pool, sse = _make_manager(proxy_mode="lease", proxy_id=raw_id)
    manager.register_handler("ideal", MethodHandler("ideal"))
    result = await manager.submit_batch("ideal", ["a@x|p"])
    job_id = result.created_job_ids[0]
    await _wait_until(
        lambda: manager.get_job(job_id).status == JobStatus.QR_READY  # type: ignore[union-attr]
    )

    expected_safe = safe_proxy_id(raw_id)
    record = manager.get_job(job_id)
    assert record is not None
    job_acquired = [e for e in record.logs if e.get("message") == "proxy_acquired"]
    sse_acquired = [
        ev for ev in sse.log_events if ev.get("message") == "proxy_acquired"
    ]
    assert job_acquired, "expected proxy_acquired in job log buffer"
    found_safe = False
    for entry in job_acquired + sse_acquired:
        blob = str(entry)
        assert raw_id not in blob, f"raw proxy_id leaked in log: {entry!r}"
        proxy_id_field = entry.get("proxy_id")
        if proxy_id_field is not None:
            assert proxy_id_field == expected_safe
            found_safe = True
    assert found_safe, "expected safe_proxy_id in proxy_id field"


# ---------------------------------------------------------------------------
# AC-8: explicit proxy_lease_health drives mark_alive/mark_dead
# ---------------------------------------------------------------------------


async def test_explicit_proxy_lease_health_skips_legacy_inference() -> None:
    """AC-8: explicit DEAD/ALIVE; legacy None keeps QR_READY→alive inference."""
    # Explicit DEAD even on QR_READY → mark_dead, not mark_alive
    manager, _, pool, _ = _make_manager(proxy_mode="lease")
    h_dead = MethodHandler(
        "m_dead",
        run_result=JobResult(
            status=JobStatus.QR_READY,
            artifact_path="/tmp/qr.png",
            proxy_lease_health=ProxyLeaseHealth.DEAD,
        ),
    )
    manager.register_handler("m_dead", h_dead)
    r = await manager.submit_batch("m_dead", ["d@x|p"])
    jid = r.created_job_ids[0]
    await _wait_until(lambda: manager.get_job(jid).status == JobStatus.QR_READY)  # type: ignore[union-attr]
    assert pool.dead_marks == [pool.proxy_id]
    assert pool.alive_marks == []

    # Explicit ALIVE on ERROR (non-network) → mark_alive, not dead
    pool.alive_marks.clear()
    pool.dead_marks.clear()
    h_alive = MethodHandler(
        "m_alive",
        run_result=JobResult(
            status=JobStatus.ERROR,
            error_code="business",
            error_message="account locked",
            proxy_lease_health=ProxyLeaseHealth.ALIVE,
        ),
    )
    manager.register_handler("m_alive", h_alive)
    r2 = await manager.submit_batch("m_alive", ["a@x|p"])
    j2 = r2.created_job_ids[0]
    await _wait_until(lambda: manager.get_job(j2).status == JobStatus.ERROR)  # type: ignore[union-attr]
    assert pool.alive_marks == [pool.proxy_id]
    assert pool.dead_marks == []

    # Legacy: no field, QR_READY → mark_alive (existing inference)
    pool.alive_marks.clear()
    pool.dead_marks.clear()
    h_legacy = MethodHandler(
        "m_legacy",
        run_result=JobResult(status=JobStatus.QR_READY, artifact_path="/tmp/q.png"),
    )
    manager.register_handler("m_legacy", h_legacy)
    r3 = await manager.submit_batch("m_legacy", ["l@x|p"])
    j3 = r3.created_job_ids[0]
    await _wait_until(lambda: manager.get_job(j3).status == JobStatus.QR_READY)  # type: ignore[union-attr]
    assert pool.alive_marks == [pool.proxy_id]
    assert pool.dead_marks == []

    # Legacy: ERROR with network marker → mark_dead
    pool.alive_marks.clear()
    pool.dead_marks.clear()
    h_net = MethodHandler(
        "m_net",
        run_result=JobResult(
            status=JobStatus.ERROR,
            error_code="login_failed",
            error_message="ConnectTimeout: proxy connection timed out",
        ),
    )
    manager.register_handler("m_net", h_net)
    r4 = await manager.submit_batch("m_net", ["n@x|p"])
    j4 = r4.created_job_ids[0]
    await _wait_until(lambda: manager.get_job(j4).status == JobStatus.ERROR)  # type: ignore[union-attr]
    assert pool.dead_marks == [pool.proxy_id]
    assert pool.alive_marks == []
