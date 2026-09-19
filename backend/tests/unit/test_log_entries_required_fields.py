"""Unit test log entries chứa đầy đủ field bắt buộc + job isolation khi 1 job vào ERROR (Task 35.2).

**Requirements: 1.9, 2.8, 3.7, 4.10, 5.9, 14.2**

Bổ sung `test_log_entries_complete_fields.py` (đã cover (A) request/attempt/
http_status ở tầng `StripeClient` và (B) shape của `IdealFlowError` subclass)
bằng 2 khía cạnh runtime:

    (1) SSE `job_status` event khi handler trả `JobResult(ERROR)` mang ĐỦ
        3 phần theo R14.2:
          - bước xảy ra lỗi (step) — dùng `error_code` non-empty +
            `error_message` chứa định danh bước (ví dụ "[login]").
          - mã lỗi HTTP nếu có — với `LoginError` bước login chưa có HTTP
            status; verify `error_code="login_failed"` (mã lỗi ổn định lộ
            ra API/log — R14.4).
          - mô tả nguyên nhân cụ thể — `error_message` phải chứa `reason`
            thực tế (ví dụ `"account_locked"`), KHÔNG dùng thông báo chung
            mơ hồ (R14.2).

    (2) Job isolation (R14.2 — Fail_Fast per-job): khi 1 job vào `ERROR`,
        các job khác đang `RUNNING`/`PENDING` KHÔNG bị dừng theo. Đây là
        chốt hợp đồng cho luồng multi-job của `JobManager._run_handler`
        (boundary duy nhất) — bug lâm ngoài scope 1 job cụ thể sẽ vi phạm
        R14.2 và làm job tuyến khác chết oan.

Setup: `SseBroadcaster` + `JobManager` thật (không mock deep). Fake handler
implement `PaymentFlowHandler` Protocol để mimic behaviour boundary của
`IdealFlowHandler.run()` khi bắt `LoginError` → trả `JobResult(status=ERROR,
error_code="login_failed", error_message=str(exc))`. Subscribe queue của
broadcaster để capture toàn bộ SSE event, parse `text/event-stream` payload
theo cú pháp chuẩn (`event: <type>\ndata: <json>\n\n`) → dict Python.

Timeout mỗi test cứng 10s qua `asyncio.wait_for` — tránh stuck khi có bug
trong scheduler/lock/semaphore của JobManager.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.core.job_manager import JobManager
from app.core.payment_flow import (
    AccountLineError,
    Job,
    JobResult,
    JobStatus,
    ParsedAccount,
)
from app.core.proxy_pool import ProxyLease
from app.core.sse import SseBroadcaster
from app.payments.ideal.errors import LoginError

_PER_TEST_TIMEOUT_SECONDS = 10.0


# ---------------------------------------------------------------------------
# Test doubles — minimal, không mock deep
# ---------------------------------------------------------------------------


class _FakeSettings:
    """Stub `SettingsRepository.list` — trả snapshot rỗng cho mọi lần gọi."""

    async def list(self, prefix: str | None = None) -> dict[str, Any]:  # noqa: ARG002
        return {}

    async def get(self, key: str) -> Any | None:  # noqa: ARG002
        # `_run_handler` (auto-retry, proxy-health) đọc setting đơn lẻ qua
        # `.get()`; snapshot rỗng → trả `None` để caller tự fallback default.
        return None


class _FakeProxyPool:
    """Direct_Mode (R9.2) — `acquire` luôn trả `None`, `release` no-op."""

    async def acquire(
        self,
        job_id: str,  # noqa: ARG002
        *,
        wait_for_release: bool = False,  # noqa: ARG002
        cancellation_token: Any | None = None,  # noqa: ARG002
    ) -> ProxyLease | None:
        return None

    def release(self, lease: ProxyLease | None) -> None:  # noqa: ARG002
        return None


class _LoginFailingHandler:
    """Fake handler mimic boundary catch của `IdealFlowHandler.run()`.

    Trong sản phẩm thực, `IdealFlowHandler.run()` bắt `LoginError` (subclass
    của `IdealFlowError`) tại boundary duy nhất và map thành `JobResult(
    status=ERROR, error_code=exc.error_code, error_message=redact(str(exc)))`.
    Fake handler mô phỏng chính xác luồng đó để test SSE `job_status` event
    chứa ĐỦ 3 phần (step/error_code/reason cụ thể — R14.2).

    KHÔNG raise `LoginError` ra ngoài `run()` vì đó sẽ đi qua path
    `_run_handler.except Exception` → `error_code="internal_error"` (bug nội
    tại chưa lường trước, không phải lỗi domain có step tường minh).
    """

    def get_max_concurrent_key(self) -> str:
        return "ideal.max_concurrent"

    def parse_account_line(self, line: str) -> ParsedAccount | AccountLineError:
        if not line.strip():
            return AccountLineError(line=line, reason="empty")
        return ParsedAccount(raw_line=line)

    async def run(self, job: Job, proxy_lease: ProxyLease | None) -> JobResult:  # noqa: ARG002
        try:
            raise LoginError(reason="account_locked")
        except LoginError as exc:
            return JobResult(
                status=JobStatus.ERROR,
                error_code=exc.error_code,
                error_message=str(exc),
            )


class _MixedIsolationHandler:
    """Handler branching theo `account_line` — dùng cho test isolation.

    - `job.account_line` bắt đầu bằng `"fail"` → mimic boundary catch
      `LoginError` → `JobResult(ERROR)`.
    - Ngược lại → chờ `ok_delay` giây (respect cancellation token) rồi trả
      `QR_READY`.

    Respect `cancellation_token`: nếu bị force-stop từ ngoài, thoát sớm với
    `JobResult(STOPPED)` — dùng để verify job OK KHÔNG bị cancel oan khi
    job fail vào ERROR.
    """

    def __init__(self, ok_delay: float = 0.2) -> None:
        self._ok_delay = ok_delay

    def get_max_concurrent_key(self) -> str:
        return "ideal.max_concurrent"

    def parse_account_line(self, line: str) -> ParsedAccount | AccountLineError:
        if not line.strip():
            return AccountLineError(line=line, reason="empty")
        return ParsedAccount(raw_line=line)

    async def run(self, job: Job, proxy_lease: ProxyLease | None) -> JobResult:  # noqa: ARG002
        if job.account_line.startswith("fail"):
            try:
                raise LoginError(reason="account_locked")
            except LoginError as exc:
                return JobResult(
                    status=JobStatus.ERROR,
                    error_code=exc.error_code,
                    error_message=str(exc),
                )

        loop = asyncio.get_event_loop()
        deadline = loop.time() + self._ok_delay
        while loop.time() < deadline:
            if job.cancellation_token.is_cancelled():
                return JobResult(status=JobStatus.STOPPED)
            await asyncio.sleep(0.005)
        return JobResult(
            status=JobStatus.QR_READY,
            artifact_path=f"/tmp/{job.job_id}.png",
        )


# ---------------------------------------------------------------------------
# SSE queue helpers — parse `text/event-stream` payload
# ---------------------------------------------------------------------------


def _parse_sse_message(raw: str) -> dict[str, Any]:
    """Parse 1 message SSE (`event: <type>\\ndata: <json>\\n\\n`) → dict.

    Trả về `{"event": <type>, "data": <parsed_json>}`. Raise `ValueError`
    nếu message không đúng cú pháp — fail-fast để phát hiện regression
    trong `format_sse_event()` sớm, không silent-swallow.
    """
    event_type: str | None = None
    data_str: str | None = None
    for line in raw.strip().splitlines():
        if line.startswith("event: "):
            event_type = line[len("event: ") :]
        elif line.startswith("data: "):
            data_str = line[len("data: ") :]
    if event_type is None or data_str is None:
        raise ValueError(f"SSE message không đúng cú pháp: {raw!r}")
    return {"event": event_type, "data": json.loads(data_str)}


async def _drain_queue(
    queue: asyncio.Queue[str], *, quiet_gap: float = 0.05, max_wait: float = 2.0
) -> list[dict[str, Any]]:
    """Đọc tất cả event trong queue tới khi im lặng `quiet_gap` giây liên tục
    hoặc tổng thời gian đạt `max_wait`.

    Không dùng `queue.empty()` để tránh race — dùng `wait_for(get(), quiet_gap)`
    làm signal "không còn event mới trong `quiet_gap`".
    """
    events: list[dict[str, Any]] = []
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max_wait
    while loop.time() < deadline:
        try:
            raw = await asyncio.wait_for(queue.get(), timeout=quiet_gap)
        except asyncio.TimeoutError:
            return events
        events.append(_parse_sse_message(raw))
    return events


async def _wait_for_status(
    manager: JobManager,
    job_id: str,
    target: JobStatus,
    *,
    timeout: float = 5.0,
) -> None:
    """Poll `manager.get_job(job_id).status` mỗi 5ms cho tới khi đạt `target`.

    Fail-fast với `TimeoutError` — không silent-swallow để test không stuck.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        record = manager.get_job(job_id)
        if record is not None and record.status == target:
            return
        await asyncio.sleep(0.005)
    record = manager.get_job(job_id)
    actual = record.status if record is not None else "<not_found>"
    raise TimeoutError(
        f"Job {job_id} không đạt trạng thái {target} trong {timeout}s "
        f"(actual={actual!r})"
    )


def _make_manager() -> tuple[JobManager, SseBroadcaster]:
    """Factory JobManager thật + SseBroadcaster thật với stub Settings/Proxy."""
    sse = SseBroadcaster()
    manager = JobManager(
        settings=_FakeSettings(),  # type: ignore[arg-type]
        proxy_pool=_FakeProxyPool(),  # type: ignore[arg-type]
        sse=sse,
    )
    return manager, sse


async def _shutdown_manager(manager: JobManager) -> None:
    """Cancel scheduler task để không rớt lại pending task khi event loop
    của test đóng — tránh warning "Task was destroyed but it is pending".

    Không kill job đang chạy vì các test đều đợi terminal state trước khi
    gọi hàm này; chỉ cần dừng vòng lặp scheduler.
    """
    task = manager._scheduler_task  # noqa: SLF001 — cleanup nội bộ cho test
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        return


# ---------------------------------------------------------------------------
# Test 1 — SSE `job_status` ERROR event chứa đủ 3 phần (R14.2)
# ---------------------------------------------------------------------------


async def test_error_job_status_event_contains_step_code_and_specific_reason() -> None:
    """SSE `job_status` event khi handler trả `JobResult(ERROR)` phải chứa
    đủ 3 phần theo R14.2 (bước xảy ra lỗi + mã lỗi HTTP nếu có + mô tả
    nguyên nhân cụ thể):

        - `job_id`: định danh job bị lỗi (R14.2 — log realtime định danh).
        - `error_code`: mã lỗi ổn định lộ ra API (R14.4), encode `step`
          qua prefix: `login_failed` (step="login"), `stripe_http_4xx`
          (step="stripe_*"), `refresh_poll_exhausted` (step="refresh_poll"),
          v.v — user/CLI đọc được step trực tiếp từ `error_code` này.
        - `error_message` non-empty chứa `reason` cụ thể (ví dụ
          `"account_locked"`) — KHÔNG dùng thông báo chung mơ hồ (R14.2).

    Với `LoginError` bước login chưa có HTTP request nào → không có
    `http_status` field trong data (đúng semantics: step login pre-HTTP).
    Test bước có HTTP status (Stripe/approve/refresh/redirect/initiate) đã
    được cover ở `test_log_entries_complete_fields.py` — khía cạnh SSE
    runtime của handler → JobResult(ERROR) mới là chốt bổ sung ở đây.

    **Validates: Requirements 1.9, 14.2, 14.4**
    """

    async def _body() -> None:
        manager, sse = _make_manager()
        queue = sse.register_client()
        try:
            manager.register_handler("ideal", _LoginFailingHandler())
            result = await manager.submit_batch("ideal", ["fail@example.com|pw"])
            assert len(result.created_job_ids) == 1
            job_id = result.created_job_ids[0]

            await _wait_for_status(manager, job_id, JobStatus.ERROR, timeout=3.0)

            events = await _drain_queue(queue, quiet_gap=0.05, max_wait=1.0)
            error_events = [
                ev
                for ev in events
                if ev["event"] == "job_status"
                and ev["data"].get("job_id") == job_id
                and ev["data"].get("status") == JobStatus.ERROR.value
            ]
            assert error_events, (
                f"Thiếu SSE `job_status` event với status=error cho job "
                f"{job_id!r} (R14.2 — bước xảy ra lỗi PHẢI được broadcast). "
                f"Events: {events!r}"
            )
            error_event = error_events[0]
            data = error_event["data"]

            # (1) job_id — định danh job bị lỗi.
            assert data["job_id"] == job_id

            # (2) error_code — mã lỗi ổn định (R14.4) encode step qua prefix.
            error_code = data.get("error_code")
            assert error_code == "login_failed", (
                f"Thiếu/sai `error_code` (R14.4 — mã lỗi ổn định lộ ra "
                f"API/log): data={data!r}"
            )
            # `error_code` phải encode step "login" — user/CLI đọc trực
            # tiếp bước xảy ra lỗi từ mã này (R14.2 — bước xảy ra lỗi).
            assert "login" in error_code, (
                f"`error_code` không encode step 'login' (R14.2 — bước "
                f"xảy ra lỗi phải nhận diện được từ mã lỗi): {error_code!r}"
            )

            # (3) error_message — non-empty, chứa reason cụ thể.
            message = data.get("error_message")
            assert isinstance(message, str) and message, (
                f"Thiếu `error_message` non-empty (R14.2 — mô tả nguyên "
                f"nhân cụ thể): data={data!r}"
            )
            assert "account_locked" in message, (
                f"`error_message` thiếu reason 'account_locked' — R14.2 "
                f"cấm dùng thông báo chung mơ hồ, phải nêu nguyên nhân "
                f"cụ thể: {message!r}"
            )
            # Message phải mang thông tin cụ thể vượt khỏi mã lỗi trần
            # (không phải chỉ nhắc lại `error_code`) — R14.2.
            assert message.strip() != error_code, (
                f"`error_message` trùng `error_code` — không mang mô tả "
                f"nguyên nhân bổ sung (R14.2): {message!r}"
            )

            # Cross-check record trên JobManager — nguồn API layer đọc.
            record = manager.get_job(job_id)
            assert record is not None
            assert record.status == JobStatus.ERROR
            assert record.error_code == "login_failed"
            assert record.error_message == message
        finally:
            sse.unregister_client(queue)
            await _shutdown_manager(manager)

    await asyncio.wait_for(_body(), timeout=_PER_TEST_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# Test 2 — Job isolation: 1 job ERROR không dừng job khác đang running
# ---------------------------------------------------------------------------


async def test_error_in_one_job_does_not_stop_other_running_job() -> None:
    """Job isolation (R14.2 — Fail_Fast per-job): 2 job song song, 1 vào
    ERROR (LoginError), job kia PHẢI chạy tiếp và hoàn thành QR_READY.

    Setup: `max_concurrent=2` để cả 2 job chạy đồng thời. Handler branching
    theo `account_line`:
        - `fail_...` → boundary catch LoginError → `JobResult(ERROR)`.
        - `ok_...` → chờ 0.2s (respect cancellation token) → `QR_READY`.

    Assert:
        - Job fail: trạng thái `ERROR`, `error_code="login_failed"`.
        - Job ok tại thời điểm job fail vào ERROR: KHÔNG bị force-stop
          (không ở `STOPPED`/`ERROR`).
        - Job ok cuối cùng: trạng thái `QR_READY` — chạy tới cùng, không
          bị cancel oan.

    **Validates: Requirements 14.2**
    """

    async def _body() -> None:
        manager, _sse = _make_manager()
        try:
            manager.register_handler("ideal", _MixedIsolationHandler(ok_delay=0.2))
            await manager.update_max_concurrent(2)

            result = await manager.submit_batch(
                "ideal", ["fail_account@x|p", "ok_account@x|p"]
            )
            assert len(result.created_job_ids) == 2
            fail_id, ok_id = result.created_job_ids

            # Job fail phải vào ERROR sớm.
            await _wait_for_status(manager, fail_id, JobStatus.ERROR, timeout=3.0)

            # Ngay sau khi fail_id ERROR — job ok KHÔNG được ở STOPPED/ERROR.
            ok_record = manager.get_job(ok_id)
            assert ok_record is not None
            assert ok_record.status not in {JobStatus.STOPPED, JobStatus.ERROR}, (
                f"Job OK bị dừng theo khi job khác vào ERROR — vi phạm "
                f"R14.2 (Fail_Fast per-job KHÔNG lan tới job khác). Actual "
                f"status={ok_record.status!r}"
            )

            # Đợi job OK hoàn thành tự nhiên → QR_READY.
            await _wait_for_status(manager, ok_id, JobStatus.QR_READY, timeout=3.0)

            # Job fail vẫn ở ERROR (không bị "hồi phục" oan).
            fail_record = manager.get_job(fail_id)
            assert fail_record is not None
            assert fail_record.status == JobStatus.ERROR
            assert fail_record.error_code == "login_failed"
        finally:
            await _shutdown_manager(manager)

    await asyncio.wait_for(_body(), timeout=_PER_TEST_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# Test 3 (bổ trợ) — cancellation token của job ok KHÔNG bị set khi job fail lỗi
# ---------------------------------------------------------------------------


async def test_other_job_cancellation_token_untouched_when_one_job_errors() -> None:
    """Verify sâu hơn Test 2: khi job fail vào ERROR, `cancellation_token`
    của job OK PHẢI vẫn `is_cancelled() == False`.

    Đây là chốt hợp đồng invariant: JobManager KHÔNG được gọi
    `record.job.cancellation_token.cancel()` cho job khác khi 1 job vào
    ERROR — nếu gọi nhầm, handler OK sẽ tự thoát sớm với `STOPPED` (theo
    hợp đồng `CancellationToken` — R8.6).

    **Validates: Requirements 14.2**
    """

    async def _body() -> None:
        manager, _sse = _make_manager()
        try:
            manager.register_handler("ideal", _MixedIsolationHandler(ok_delay=0.4))
            await manager.update_max_concurrent(2)

            result = await manager.submit_batch(
                "ideal", ["fail_account@x|p", "ok_account@x|p"]
            )
            fail_id, ok_id = result.created_job_ids

            await _wait_for_status(manager, fail_id, JobStatus.ERROR, timeout=3.0)

            # Ngay khi fail_id ERROR — token của job OK phải chưa bị cancel.
            ok_record = manager.get_job(ok_id)
            assert ok_record is not None
            assert not ok_record.job.cancellation_token.is_cancelled(), (
                "cancellation_token của job OK bị set khi job khác vào "
                "ERROR — vi phạm R14.2 (Fail_Fast per-job không lan)."
            )

            # Cleanup: đợi job OK xong để không leak task.
            await _wait_for_status(manager, ok_id, JobStatus.QR_READY, timeout=3.0)
        finally:
            await _shutdown_manager(manager)

    await asyncio.wait_for(_body(), timeout=_PER_TEST_TIMEOUT_SECONDS)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
