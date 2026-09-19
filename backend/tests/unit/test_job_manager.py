"""Unit test cho `core/job_manager.py` (task 8.3).

Test theo hành vi thực tế của `JobManager` — không mock deep, chỉ dùng stub
minimal cho `SettingsRepository`/`ProxyPool`/`SseBroadcaster` để cô lập
Job_Manager. Handler dùng `FakeHandler` implement `PaymentFlowHandler` để
kiểm soát flow chính xác trong test.

Đây là unit test đầy đủ cho JobManager. File này KHÔNG được chạy trong
phase implementation hiện tại — chỉ được chạy khi phase test toàn bộ
implementation được kích hoạt (theo directive "Code hết đi đã rồi test
sau").
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import pytest

from app.core.errors import HandlerAlreadyRegisteredError, ProxyExhaustedError
from app.core.job_manager import (
    BatchSubmitResult,
    HandlerContractError,
    JobManager,
    UnknownPaymentMethodError,
)
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


class FakeSettings:
    """Minimal stub cho `SettingsRepository.list()`."""

    def __init__(self, snapshot: dict[str, Any] | None = None) -> None:
        self._snapshot = snapshot or {}

    async def list(self, prefix: str | None = None) -> dict[str, Any]:
        return dict(self._snapshot)

    async def get(self, key: str) -> Any | None:
        # `_run_handler` (auto-retry, proxy-health) và các đường Pull_Mode
        # đọc setting đơn lẻ qua `.get()` — trả từ cùng `_snapshot` để fake
        # nhất quán với `.list()`. `None` cho key chưa cấu hình (caller tự
        # fallback default).
        return self._snapshot.get(key)


@dataclass
class FakeLease:
    proxy_id: str
    materialized_url: str
    leased_at: float


class FakeProxyPool:
    """Direct_Mode mặc định (acquire → None). Có thể cấu hình để raise/return lease."""

    def __init__(self) -> None:
        self.acquired_for: list[str] = []
        self.released: list[str] = []
        self.mode: str = "direct"  # "direct" | "lease" | "exhausted"
        # `_run_handler` đọc `probe_config` (feature proxy-health) qua
        # getattr — default nếu thiếu là `ProbeConfig(enabled=False)` với
        # `fallback_direct_on_exhausted=True`, khiến ProxyExhaustedError
        # rơi vào fallback-direct thay vì ERROR. Set tường minh
        # `fallback_direct_on_exhausted=False` để mode "exhausted" đi đúng
        # nhánh set `error_code="proxy_exhausted"` (chỉ ảnh hưởng mode
        # exhausted — direct/lease không chạm nhánh fallback này).
        from app.core.proxy_health import ProbeConfig

        self.probe_config = ProbeConfig(
            enabled=False, fallback_direct_on_exhausted=False
        )

    async def acquire(
        self,
        job_id: str,
        *,
        wait_for_release: bool = False,  # noqa: ARG002 — nhận & bỏ qua
        cancellation_token: Any | None = None,  # noqa: ARG002
    ):
        # `_run_handler` gọi qua `acquire_live_proxy` với 2 kwarg mới
        # (`wait_for_release`, `cancellation_token`) từ feature proxy-health.
        # Fake chấp nhận (keyword-only) và bỏ qua, giữ nguyên hành vi mode.
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
    """Configurable handler implement `PaymentFlowHandler`."""

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


def _make_manager(
    snapshot: dict[str, Any] | None = None,
    proxy_mode: str = "direct",
) -> tuple[JobManager, FakeSettings, FakeProxyPool, FakeSse]:
    settings = FakeSettings(snapshot=snapshot)
    proxy_pool = FakeProxyPool()
    proxy_pool.mode = proxy_mode
    sse = FakeSse()
    manager = JobManager(settings=settings, proxy_pool=proxy_pool, sse=sse)  # type: ignore[arg-type]
    return manager, settings, proxy_pool, sse


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    """Poll predicate mỗi 5ms cho tới khi True hoặc timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise TimeoutError("predicate did not become true within timeout")


# ---------------------------------------------------------------------------
# register_handler
# ---------------------------------------------------------------------------


async def test_register_handler_success() -> None:
    manager, *_ = _make_manager()
    handler = FakeHandler()
    manager.register_handler("ideal", handler)
    assert manager.get_handler("ideal") is handler


async def test_register_handler_duplicate_raises() -> None:
    manager, *_ = _make_manager()
    manager.register_handler("ideal", FakeHandler())
    with pytest.raises(HandlerAlreadyRegisteredError):
        manager.register_handler("ideal", FakeHandler())


async def test_register_handler_missing_contract() -> None:
    manager, *_ = _make_manager()

    class Broken:
        async def run(self, job, proxy_lease):
            return JobResult(status=JobStatus.QR_READY)

        def parse_account_line(self, line):
            return ParsedAccount(raw_line=line)
        # Cố tình thiếu get_max_concurrent_key

    with pytest.raises(HandlerContractError) as exc:
        manager.register_handler("ideal", Broken())
    assert exc.value.missing_attr == "get_max_concurrent_key"


# ---------------------------------------------------------------------------
# submit_batch
# ---------------------------------------------------------------------------


async def test_submit_batch_unknown_payment_method() -> None:
    manager, *_ = _make_manager()
    with pytest.raises(UnknownPaymentMethodError):
        await manager.submit_batch("nope", ["a@b|x"])


async def test_submit_batch_returns_jobs_in_order_with_skipped() -> None:
    manager, _, _, sse = _make_manager()
    handler = FakeHandler(parse_valid=True, run_delay=1.0)  # keep running so job stays running
    manager.register_handler("ideal", handler)
    lines = ["ok1@x|p", "", "ok2@x|p", "  "]
    result = await manager.submit_batch("ideal", lines)

    assert isinstance(result, BatchSubmitResult)
    assert len(result.created_job_ids) == 2
    assert len(result.skipped) == 2
    assert {row["reason"] for row in result.skipped} == {"empty"}
    # Đảm bảo status pending được broadcast
    assert all(
        any(ev["job_id"] == jid and ev["status"] == "pending" for ev in sse.status_events)
        for jid in result.created_job_ids
    )


async def test_submit_batch_empty_valid_lines_ok() -> None:
    manager, *_ = _make_manager()
    handler = FakeHandler(parse_valid=False)
    manager.register_handler("ideal", handler)
    result = await manager.submit_batch("ideal", ["a", "b"])
    assert result.created_job_ids == []
    assert len(result.skipped) == 2


async def test_submit_batch_snapshot_isolated_per_batch() -> None:
    """R11.2: job dùng snapshot tại thời điểm submit_batch, không retroactive."""
    settings = FakeSettings(snapshot={"ideal.max_concurrent": 5})
    proxy_pool = FakeProxyPool()
    sse = FakeSse()
    manager = JobManager(settings=settings, proxy_pool=proxy_pool, sse=sse)  # type: ignore[arg-type]
    handler = FakeHandler(run_delay=1.0)
    manager.register_handler("ideal", handler)

    result_1 = await manager.submit_batch("ideal", ["a@x|p"])
    # Đổi settings sau khi submit
    settings._snapshot = {"ideal.max_concurrent": 42}  # noqa: SLF001
    result_2 = await manager.submit_batch("ideal", ["b@x|p"])

    rec_1 = manager.get_job(result_1.created_job_ids[0])
    rec_2 = manager.get_job(result_2.created_job_ids[0])
    assert rec_1 is not None and rec_2 is not None
    assert rec_1.settings_snapshot.get("ideal.max_concurrent") == 5
    assert rec_2.settings_snapshot.get("ideal.max_concurrent") == 42


async def test_force_rerun_skips_no_free_offer_terminal_job() -> None:
    """Permanent amount-gate rejects must not be reset by Retry failed."""

    class DedupFakeHandler(FakeHandler):
        def get_account_dedup_key(self, parsed: ParsedAccount) -> str:
            return parsed.raw_line.split("|", 1)[0]

    manager, *_ = _make_manager()
    handler = DedupFakeHandler(
        run_result=JobResult(
            status=JobStatus.ERROR,
            error_code="no_free_offer",
            error_message="Free offer required but amount=169407",
        )
    )
    manager.register_handler("ideal", handler)

    first = await manager.submit_batch("ideal", ["paid@x|p|t"])
    job_id = first.created_job_ids[0]
    await _wait_until(
        lambda: manager.get_job(job_id).status == JobStatus.ERROR  # type: ignore[union-attr]
    )

    rerun = await manager.submit_batch(
        "ideal",
        ["paid@x|p|t"],
        force_rerun=True,
    )

    record = manager.get_job(job_id)
    assert rerun.created_job_ids == []
    assert rerun.skipped == [{"line": "paid@x|p|t", "reason": "no_free_offer"}]
    assert record is not None
    assert record.status == JobStatus.ERROR
    assert record.error_code == "no_free_offer"
    assert len(handler.run_called_with) == 1


async def test_force_rerun_skips_deactivated_login_terminal_job() -> None:
    """Deleted/deactivated login failures are permanent rejects too."""

    class DedupFakeHandler(FakeHandler):
        def get_account_dedup_key(self, parsed: ParsedAccount) -> str:
            return parsed.raw_line.split("|", 1)[0]

    manager, *_ = _make_manager()
    handler = DedupFakeHandler(
        run_result=JobResult(
            status=JobStatus.ERROR,
            error_code="login_failed",
            error_message="Login failed: reason=invalid_credential",
        )
    )
    manager.register_handler("ideal", handler)

    first = await manager.submit_batch("ideal", ["dead@x|p|t"])
    job_id = first.created_job_ids[0]
    await _wait_until(
        lambda: manager.get_job(job_id).status == JobStatus.ERROR  # type: ignore[union-attr]
    )
    record = manager.get_job(job_id)
    assert record is not None
    record.logs.append(
        {
            "ts": time.time(),
            "message": (
                "[login] password/verify 403: You do not have an account "
                "because it has been deleted or deactivated."
            ),
            "extra": {},
        }
    )

    rerun = await manager.submit_batch(
        "ideal",
        ["dead@x|p|t"],
        force_rerun=True,
    )

    assert rerun.created_job_ids == []
    assert rerun.skipped == [{"line": "dead@x|p|t", "reason": "account_deactivated"}]
    assert record.status == JobStatus.ERROR
    assert record.error_code == "login_failed"
    assert len(handler.run_called_with) == 1


# ---------------------------------------------------------------------------
# scheduler + concurrency
# ---------------------------------------------------------------------------


async def test_scheduler_runs_job_to_completion() -> None:
    manager, _, proxy_pool, sse = _make_manager()
    handler = FakeHandler(run_result=JobResult(status=JobStatus.QR_READY, artifact_path="/tmp/qr.png"))
    manager.register_handler("ideal", handler)

    result = await manager.submit_batch("ideal", ["a@x|p"])
    job_id = result.created_job_ids[0]

    await _wait_until(
        lambda: manager.get_job(job_id) is not None
        and manager.get_job(job_id).status == JobStatus.QR_READY,  # type: ignore[union-attr]
    )
    # SSE có event QR_READY
    assert any(ev["job_id"] == job_id and ev["status"] == "qr_ready" for ev in sse.status_events)


async def test_scheduler_handler_exception_marks_error_and_keeps_scheduler_alive() -> None:
    manager, *_, sse = _make_manager()
    bad_handler = FakeHandler(run_exc=RuntimeError("boom"))
    manager.register_handler("ideal", bad_handler)
    result_1 = await manager.submit_batch("ideal", ["fail@x|p"])
    job_id_1 = result_1.created_job_ids[0]

    await _wait_until(
        lambda: manager.get_job(job_id_1).status == JobStatus.ERROR  # type: ignore[union-attr]
    )
    # Submit thêm job — scheduler còn sống → job kế cũng chạy được (khác handler)
    good_handler = FakeHandler(run_result=JobResult(status=JobStatus.QR_READY))
    # Overwrite handler direct — normally đăng ký new payment_method.
    manager._handlers["ideal"] = good_handler  # noqa: SLF001
    result_2 = await manager.submit_batch("ideal", ["ok@x|p"])
    job_id_2 = result_2.created_job_ids[0]
    await _wait_until(
        lambda: manager.get_job(job_id_2).status == JobStatus.QR_READY  # type: ignore[union-attr]
    )


async def test_scheduler_proxy_exhausted_marks_error() -> None:
    manager, _, proxy_pool, sse = _make_manager(proxy_mode="exhausted")
    manager.register_handler("ideal", FakeHandler())
    result = await manager.submit_batch("ideal", ["a@x|p"])
    job_id = result.created_job_ids[0]
    await _wait_until(
        lambda: manager.get_job(job_id).status == JobStatus.ERROR  # type: ignore[union-attr]
    )
    error_events = [
        ev for ev in sse.status_events if ev["job_id"] == job_id and ev["status"] == "error"
    ]
    assert error_events
    assert error_events[0].get("error_code") == "proxy_exhausted"


async def test_scheduler_lease_released_after_run() -> None:
    manager, _, proxy_pool, _ = _make_manager(proxy_mode="lease")
    manager.register_handler("ideal", FakeHandler())
    result = await manager.submit_batch("ideal", ["a@x|p"])
    job_id = result.created_job_ids[0]
    await _wait_until(
        lambda: manager.get_job(job_id).status == JobStatus.QR_READY  # type: ignore[union-attr]
    )
    assert proxy_pool.released == ["proxy-1"]


async def test_max_concurrent_limits_running_jobs() -> None:
    manager, *_ = _make_manager()
    handler = FakeHandler(run_delay=0.5)
    manager.register_handler("ideal", handler)
    await manager.update_max_concurrent(2)

    result = await manager.submit_batch("ideal", ["a@x|p", "b@x|p", "c@x|p", "d@x|p"])
    assert len(result.created_job_ids) == 4

    # Tại 1 thời điểm, không có quá 2 job running
    await asyncio.sleep(0.05)
    running_count = sum(
        1 for r in manager.list_jobs() if r.status == JobStatus.RUNNING
    )
    assert running_count <= 2

    # Đợi tất cả xong
    await _wait_until(
        lambda: all(r.status == JobStatus.QR_READY for r in manager.list_jobs()),
        timeout=3.0,
    )


async def test_update_max_concurrent_over_budget_does_not_kill_running() -> None:
    """update_max_concurrent(new < running) không giết job đang running.

    Với `running_count > new_value`, các job đang running vẫn tiếp tục;
    job mới không được start tới khi số running drop xuống dưới new_value.
    """
    manager, *_ = _make_manager()
    handler = FakeHandler(run_delay=0.3)
    manager.register_handler("ideal", handler)
    await manager.update_max_concurrent(3)

    result = await manager.submit_batch("ideal", ["a@x|p", "b@x|p", "c@x|p"])
    # Đợi cho 3 job đều đang running
    await _wait_until(
        lambda: sum(
            1 for r in manager.list_jobs() if r.status == JobStatus.RUNNING
        ) == 3,
        timeout=1.0,
    )

    # Hạ limit xuống 1 — 3 job đang running KHÔNG bị giết
    await manager.update_max_concurrent(1)
    running_now = [r for r in manager.list_jobs() if r.status == JobStatus.RUNNING]
    assert len(running_now) == 3, "Running jobs bị giết oan"

    # Submit thêm job — phải chờ 3 job cũ xong hết trước khi 1 job mới chạy
    result_new = await manager.submit_batch("ideal", ["d@x|p"])
    new_id = result_new.created_job_ids[0]
    await _wait_until(
        lambda: manager.get_job(new_id).status == JobStatus.QR_READY,  # type: ignore[union-attr]
        timeout=3.0,
    )


async def test_update_max_concurrent_reject_bad_value() -> None:
    manager, *_ = _make_manager()
    with pytest.raises(ValueError):
        await manager.update_max_concurrent(0)
    with pytest.raises(TypeError):
        await manager.update_max_concurrent(True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


async def test_stop_pending_job_moves_to_stopped() -> None:
    manager, *_, sse = _make_manager()
    handler = FakeHandler(run_delay=5.0)
    manager.register_handler("ideal", handler)
    await manager.update_max_concurrent(1)

    result = await manager.submit_batch("ideal", ["a@x|p", "b@x|p"])
    running_id, pending_id = result.created_job_ids

    # Đợi cho job đầu tiên đang running
    await _wait_until(
        lambda: manager.get_job(running_id).status == JobStatus.RUNNING  # type: ignore[union-attr]
    )
    # Job thứ 2 phải vẫn pending
    assert manager.get_job(pending_id).status == JobStatus.PENDING  # type: ignore[union-attr]

    await manager.stop(pending_id)
    assert manager.get_job(pending_id).status == JobStatus.STOPPED  # type: ignore[union-attr]
    # SSE có event stopped cho pending_id
    assert any(
        ev["job_id"] == pending_id and ev["status"] == "stopped"
        for ev in sse.status_events
    )


async def test_stop_running_job_signals_cancel_handler_exits() -> None:
    manager, *_ = _make_manager()
    handler = FakeHandler(run_delay=2.0, respect_cancel=True)
    manager.register_handler("ideal", handler)
    result = await manager.submit_batch("ideal", ["a@x|p"])
    job_id = result.created_job_ids[0]
    await _wait_until(
        lambda: manager.get_job(job_id).status == JobStatus.RUNNING  # type: ignore[union-attr]
    )
    await manager.stop(job_id)
    await _wait_until(
        lambda: manager.get_job(job_id).status == JobStatus.STOPPED,  # type: ignore[union-attr]
        timeout=2.0,
    )


async def test_stop_nonexistent_job_is_noop() -> None:
    manager, *_ = _make_manager()
    await manager.stop("nope-xxx")  # KHÔNG raise


# ---------------------------------------------------------------------------
# get_settings_snapshot_for_new_jobs
# ---------------------------------------------------------------------------


async def test_settings_snapshot_reads_from_repo() -> None:
    manager, settings, _, _ = _make_manager(snapshot={"ideal.max_concurrent": 7, "foo.bar": 1})
    snapshot = await manager.get_settings_snapshot_for_new_jobs()
    assert snapshot == {"ideal.max_concurrent": 7, "foo.bar": 1}


# ---------------------------------------------------------------------------
# Add / Run tách bạch — `held` flag + start_job + start_all_held
# ---------------------------------------------------------------------------


async def test_submit_batch_start_true_default_puts_jobs_in_queue() -> None:
    """Hành vi cũ 100% — submit không kèm `start` (mặc định True) → job
    vào `_pending_order`, scheduler chạy như trước."""
    manager, *_ = _make_manager()
    handler = FakeHandler(parse_valid=True, run_delay=0.5)
    manager.register_handler("ideal", handler)

    result = await manager.submit_batch("ideal", ["a@x|p", "b@x|p"])

    assert len(result.created_job_ids) == 2
    for jid in result.created_job_ids:
        record = manager.get_job(jid)
        assert record is not None
        assert record.held is False


async def test_submit_batch_start_false_holds_jobs_out_of_queue() -> None:
    """Nút Add (start=False): job tạo ở `pending + held=True`, KHÔNG vào
    `_pending_order` → scheduler bỏ qua tới khi user bấm Start.
    """
    manager, *_ = _make_manager()
    handler = FakeHandler(parse_valid=True, run_delay=0.5)
    manager.register_handler("ideal", handler)

    result = await manager.submit_batch(
        "ideal", ["a@x|p", "b@x|p"], start=False
    )

    assert len(result.created_job_ids) == 2
    for jid in result.created_job_ids:
        record = manager.get_job(jid)
        assert record is not None
        assert record.status.value == "pending"
        assert record.held is True
        # Không có trong pending_order → scheduler không nhặt.
        assert jid not in manager._pending_order  # noqa: SLF001


async def test_start_job_transitions_held_into_queue() -> None:
    """`start_job(id)` chuyển job từ pending+held=True sang pending+held=False
    và push vào `_pending_order` — scheduler nhặt chạy."""
    manager, *_ = _make_manager()
    handler = FakeHandler(parse_valid=True, run_delay=0.5)
    manager.register_handler("ideal", handler)

    result = await manager.submit_batch("ideal", ["a@x|p"], start=False)
    job_id = result.created_job_ids[0]
    assert manager.get_job(job_id).held is True  # type: ignore[union-attr]

    started = await manager.start_job(job_id)
    assert started is True

    record = manager.get_job(job_id)
    assert record is not None
    assert record.held is False
    # Scheduler picks lên chạy — status chuyển sang running trong ~short window.
    await _wait_until(
        lambda: manager.get_job(job_id).status == JobStatus.RUNNING,  # type: ignore[union-attr]
        timeout=2.0,
    )


async def test_start_job_noop_when_not_held() -> None:
    """Job không held → `start_job` trả False, không đổi state."""
    manager, *_ = _make_manager()
    handler = FakeHandler(parse_valid=True, run_delay=0.5)
    manager.register_handler("ideal", handler)

    result = await manager.submit_batch("ideal", ["a@x|p"])  # start=True default
    job_id = result.created_job_ids[0]

    started = await manager.start_job(job_id)
    assert started is False


async def test_get_auto_retry_config_returns_failed_to_pending_false_by_default() -> None:
    """Config chưa set `auto_retry_failed_to_pending` → default False (giữ
    hành vi cũ: ERROR final khi hết retry budget)."""
    manager, *_ = _make_manager(
        snapshot={
            "ideal.auto_retry_blocked_enabled": True,
            "ideal.auto_retry_blocked_max": 3,
            "ideal.auto_retry_blocked_delay_seconds": 1.0,
            "ideal.auto_retry_blocked_codes": ["approve_blocked"],
            "ideal.auto_retry_mode": "round_robin",
            # KHÔNG set auto_retry_failed_to_pending — kỳ vọng default False.
        }
    )
    (
        enabled,
        max_retries,
        delay,
        codes,
        mode,
        failed_to_pending,
    ) = await manager._get_auto_retry_config()  # noqa: SLF001
    assert enabled is True
    assert max_retries == 3
    assert failed_to_pending is False


async def test_maybe_auto_retry_exhausted_with_failed_to_pending_off_stays_error() -> None:
    """Job hết `retry_count == max` VÀ `failed_to_pending=False` → giữ ERROR,
    KHÔNG requeue."""
    from app.core.job_manager import _JobRecord
    from app.core.payment_flow import Job, JobStatus, SimpleCancellationToken

    manager, *_ = _make_manager(
        snapshot={
            "ideal.auto_retry_blocked_enabled": True,
            "ideal.auto_retry_blocked_max": 3,
            "ideal.auto_retry_blocked_delay_seconds": 1.0,
            "ideal.auto_retry_blocked_codes": ["approve_blocked"],
            "ideal.auto_retry_mode": "round_robin",
            "ideal.auto_retry_failed_to_pending": False,
        }
    )

    # Build 1 job ERROR với retry_count đã đạt max.
    job = Job(
        job_id="j1",
        payment_method="ideal",
        account_line="x@y|p",
        created_at=0.0,
        cancellation_token=SimpleCancellationToken(),
    )
    record = _JobRecord(
        job=job,
        status=JobStatus.ERROR,
        error_code="approve_blocked",
        retry_count=3,
    )
    manager._jobs["j1"] = record  # noqa: SLF001

    scheduled = await manager._maybe_auto_retry(record)  # noqa: SLF001
    assert scheduled is False
    assert record.status == JobStatus.ERROR
    assert record.retry_count == 3
    assert "j1" not in manager._pending_order  # noqa: SLF001


async def test_maybe_auto_retry_exhausted_with_failed_to_pending_on_requeues_and_resets_counter() -> None:
    """Job hết `retry_count == max` VÀ `failed_to_pending=True` → requeue
    NGAY về PENDING, reset `retry_count=0`, push `_pending_order`. Loop vòng
    mới bắt đầu (user đã chấp nhận nguy cơ loop vô hạn)."""
    from app.core.job_manager import _JobRecord
    from app.core.payment_flow import Job, JobStatus, SimpleCancellationToken

    manager, *_ = _make_manager(
        snapshot={
            "ideal.auto_retry_blocked_enabled": True,
            "ideal.auto_retry_blocked_max": 3,
            "ideal.auto_retry_blocked_delay_seconds": 1.0,
            "ideal.auto_retry_blocked_codes": ["approve_blocked"],
            "ideal.auto_retry_mode": "round_robin",
            "ideal.auto_retry_failed_to_pending": True,
        }
    )

    job = Job(
        job_id="j1",
        payment_method="ideal",
        account_line="x@y|p",
        created_at=0.0,
        cancellation_token=SimpleCancellationToken(),
    )
    record = _JobRecord(
        job=job,
        status=JobStatus.ERROR,
        error_code="approve_blocked",
        retry_count=3,
    )
    manager._jobs["j1"] = record  # noqa: SLF001

    scheduled = await manager._maybe_auto_retry(record)  # noqa: SLF001
    assert scheduled is True
    # Reset về vòng mới.
    assert record.status == JobStatus.PENDING
    assert record.retry_count == 0
    assert record.error_code is None
    assert "j1" in manager._pending_order  # noqa: SLF001


async def test_start_all_held_starts_only_held_jobs_in_fifo_order() -> None:
    """Bulk `start_all_held` chỉ chuyển job pending+held, giữ FIFO theo
    `order`, KHÔNG chạm job pending+held=False hay job terminal."""
    manager, *_ = _make_manager()
    handler = FakeHandler(parse_valid=True, run_delay=0.5)
    manager.register_handler("ideal", handler)

    # 2 job Run + 2 job Add (held). Order tăng dần theo thứ tự submit.
    run_result = await manager.submit_batch("ideal", ["r1@x|p", "r2@x|p"])
    add_result = await manager.submit_batch(
        "ideal", ["h1@x|p", "h2@x|p"], start=False
    )

    started_ids = await manager.start_all_held()

    # Chỉ 2 held job được start, theo đúng thứ tự order tạo.
    assert started_ids == add_result.created_job_ids
    for jid in add_result.created_job_ids:
        assert manager.get_job(jid).held is False  # type: ignore[union-attr]
    # 2 job Run không bị đụng.
    for jid in run_result.created_job_ids:
        record = manager.get_job(jid)
        assert record is not None
        assert record.held is False  # đã False từ đầu

    # Gọi lại — no-op (đã start hết).
    assert await manager.start_all_held() == []
