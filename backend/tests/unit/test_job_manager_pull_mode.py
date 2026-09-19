"""Unit test cho `JobManager.claim_pull_account` + `resolve_pull_outcome` +
race condition (task 15, spec telegram-pull-job-mode).

Cover (Requirement 5.5, 5.6, 6.2, 6.3, 13.1, 20.5, 20.6, 22.1, 22.2, 22.10):
- `claim_pull_account` trả `None` khi Pull_Account_Pool rỗng.
- `claim_pull_account` trả `None` khi worker đã đạt
  `telegram.pull_mode.max_concurrent_jobs_per_user`.
- `claim_pull_account` gán đúng account FIFO đầu tiên theo `order`.
- `claim_pull_account` lưu đúng `pull_assigned_username`/`pull_assigned_first_name`.
- Race: 5 worker khác nhau `claim_pull_account` đồng thời trên 1 account
  duy nhất → đúng 1 thành công (R22.10).
- Race: 1 worker `claim_pull_account` đồng thời 2 lần khi limit=1, pool có
  2 account → chỉ 1 thành công (R22.2).
- `resolve_pull_outcome` raise `JobNotFoundError` với job lạ, raise
  `JobAlreadyResolvedError` khi gọi 2 lần, và cộng dồn counter đúng qua
  `FakeJobRepo` stub.
- Handler ERROR tự động gọi `resolve_pull_error` → `resolve_pull_outcome(FAIL)`.

Tái dùng test double từ `test_job_manager.py` (task 8.3) qua import trực
tiếp — chỉ mở rộng tối thiểu (`_PullModeFakeSettings`/`_PullModeFakeProxyPool`)
để bù 2 chỗ không tương thích với code hiện tại của `claim_pull_account`/
`_run_handler` (xem docstring 2 class dưới).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.errors import JobAlreadyResolvedError, JobNotFoundError
from app.core.job_manager import JobManager, PullOutcome
from app.core.payment_flow import JobResult, JobStatus

from tests.unit.test_job_manager import (
    FakeHandler,
    FakeProxyPool,
    FakeSettings,
    FakeSse,
    _wait_until,
)


# ---------------------------------------------------------------------------
# Test doubles mở rộng — riêng cho file này
# ---------------------------------------------------------------------------


class _PullModeFakeSettings(FakeSettings):
    """`FakeSettings` (từ `test_job_manager.py`) chỉ implement `.list()`.

    `claim_pull_account` đọc limit qua `await self._settings.get(
    "telegram.pull_mode.max_concurrent_jobs_per_user")` trực tiếp (KHÔNG
    qua `.list()`) — thêm `async def get()` đọc từ cùng `_snapshot` để
    class cha giữ nguyên, không ảnh hưởng test khác dùng `FakeSettings`.
    """

    async def get(self, key: str) -> Any | None:
        return self._snapshot.get(key)


class _PullModeFakeProxyPool(FakeProxyPool):
    """`FakeProxyPool.acquire()` gốc chỉ nhận `job_id` — `_run_handler`
    hiện tại gọi `acquire_live_proxy` → `pool.acquire(job_id,
    wait_for_release=True, cancellation_token=...)`. Mở rộng để nhận (và
    bỏ qua) 2 kwarg này, forward `job_id` cho hành vi gốc — cần cho test
    ERROR-handler-auto-resolve (job phải chạy qua `_run_handler` đầy đủ).
    """

    async def acquire(  # type: ignore[override]
        self,
        job_id: str,
        wait_for_release: bool = True,  # noqa: ARG002
        cancellation_token: Any | None = None,  # noqa: ARG002
    ):
        return await super().acquire(job_id)


def _make_pull_manager(
    max_concurrent_per_user: int = 10,
) -> tuple[JobManager, _PullModeFakeSettings, _PullModeFakeProxyPool, FakeSse]:
    """Factory JobManager ở Pull_Mode (`telegram.mode == "pull"`) với limit
    `max_concurrent_per_user` cấu hình sẵn — KHÔNG inject `job_repo` (mọi
    persist no-op), test nào cần counter tự gắn `FakeJobRepo` riêng."""
    settings = _PullModeFakeSettings(
        snapshot={
            "telegram.mode": "pull",
            "telegram.pull_mode.max_concurrent_jobs_per_user": max_concurrent_per_user,
        }
    )
    proxy_pool = _PullModeFakeProxyPool()
    sse = FakeSse()
    manager = JobManager(settings=settings, proxy_pool=proxy_pool, sse=sse)  # type: ignore[arg-type]
    return manager, settings, proxy_pool, sse


async def _seed_accounts(manager: JobManager, n: int) -> list[str]:
    """Submit `n` dòng account hợp lệ vào Pull_Account_Pool (UNASSIGNED)
    qua `submit_batch` — caller phải đăng ký handler "ideal" trước."""
    lines = [f"acc{i}@x|pass{i}" for i in range(n)]
    result = await manager.submit_batch("ideal", lines)
    return result.created_job_ids


class FakeJobRepo:
    """Stub tối giản cho `JobRepository` — chỉ implement 2 method mà
    `resolve_pull_outcome` (qua `record_worker_stat_delta` +
    `_persist_record`) thực sự gọi. `upsert` no-op (test không cần đọc lại
    DB), `upsert_worker_stat_delta` ghi lại lời gọi để assert counter."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int, str | None, str | None]] = []

    async def upsert_worker_stat_delta(
        self,
        telegram_user_id: str,
        *,
        success_delta: int = 0,
        fail_delta: int = 0,
        username: str | None = None,
        first_name: str | None = None,
    ) -> None:
        self.calls.append(
            (telegram_user_id, success_delta, fail_delta, username, first_name)
        )

    async def upsert(self, row: Any) -> None:  # noqa: ARG002
        pass


# ---------------------------------------------------------------------------
# claim_pull_account — case cơ bản
# ---------------------------------------------------------------------------


async def test_claim_pull_account_returns_none_when_pool_empty() -> None:
    manager, *_ = _make_pull_manager()

    result = await manager.claim_pull_account("worker-1", "chat-1", None, None)

    assert result is None


async def test_claim_pull_account_returns_none_when_worker_at_limit() -> None:
    manager, *_ = _make_pull_manager(max_concurrent_per_user=1)
    handler = FakeHandler(run_result=JobResult(status=JobStatus.QR_READY))
    manager.register_handler("ideal", handler)
    await _seed_accounts(manager, 2)

    first = await manager.claim_pull_account("worker-1", "chat-1", None, None)
    second = await manager.claim_pull_account("worker-1", "chat-1", None, None)

    assert first is not None
    assert second is None


async def test_claim_pull_account_assigns_first_by_order_fifo() -> None:
    manager, *_ = _make_pull_manager()
    handler = FakeHandler(run_result=JobResult(status=JobStatus.QR_READY))
    manager.register_handler("ideal", handler)
    job_ids = await _seed_accounts(manager, 3)

    claimed = await manager.claim_pull_account("worker-1", "chat-1", None, None)

    assert claimed == job_ids[0]


async def test_claim_pull_account_stores_username_and_first_name() -> None:
    manager, *_ = _make_pull_manager()
    handler = FakeHandler(run_result=JobResult(status=JobStatus.QR_READY))
    manager.register_handler("ideal", handler)
    await _seed_accounts(manager, 1)

    job_id = await manager.claim_pull_account(
        "worker-1", "chat-1", "user_a", "Nguyen"
    )

    assert job_id is not None
    record = manager.get_job(job_id)
    assert record is not None
    assert record.pull_assigned_username == "user_a"
    assert record.pull_assigned_first_name == "Nguyen"


# ---------------------------------------------------------------------------
# Race condition (R22.10, R22.2)
# ---------------------------------------------------------------------------


async def test_claim_pull_account_race_only_one_worker_wins_single_account() -> None:
    """5 worker khác nhau claim đồng thời trên đúng 1 account trong pool —
    chỉ đúng 1 thành công, 4 còn lại trả `None` (R22.10)."""
    manager, *_ = _make_pull_manager(max_concurrent_per_user=10)
    handler = FakeHandler(run_result=JobResult(status=JobStatus.QR_READY))
    manager.register_handler("ideal", handler)
    await _seed_accounts(manager, 1)

    results = await asyncio.gather(
        *[
            manager.claim_pull_account(f"worker-{i}", f"chat-{i}", None, None)
            for i in range(5)
        ]
    )

    successes = [r for r in results if r is not None]
    assert len(successes) == 1


async def test_claim_pull_account_race_same_worker_limited_to_one() -> None:
    """1 worker claim đồng thời 2 lần khi limit=1 và pool có 2 account —
    chỉ đúng 1 thành công (R22.2 — worker không được vượt limit của
    chính mình dù race)."""
    manager, *_ = _make_pull_manager(max_concurrent_per_user=1)
    handler = FakeHandler(run_result=JobResult(status=JobStatus.QR_READY))
    manager.register_handler("ideal", handler)
    await _seed_accounts(manager, 2)

    results = await asyncio.gather(
        manager.claim_pull_account("worker-1", "chat-1", None, None),
        manager.claim_pull_account("worker-1", "chat-1", None, None),
    )

    successes = [r for r in results if r is not None]
    assert len(successes) == 1


# ---------------------------------------------------------------------------
# resolve_pull_outcome
# ---------------------------------------------------------------------------


async def test_resolve_pull_outcome_raises_job_not_found() -> None:
    manager, *_ = _make_pull_manager()

    with pytest.raises(JobNotFoundError):
        await manager.resolve_pull_outcome("nonexistent-id", PullOutcome.SUCCESS)


async def test_resolve_pull_outcome_raises_already_resolved_on_second_call() -> None:
    manager, *_ = _make_pull_manager()
    handler = FakeHandler(run_result=JobResult(status=JobStatus.QR_READY))
    manager.register_handler("ideal", handler)
    await _seed_accounts(manager, 1)
    job_id = await manager.claim_pull_account("worker-1", "chat-1", None, None)
    assert job_id is not None

    await manager.resolve_pull_outcome(job_id, PullOutcome.SUCCESS)

    with pytest.raises(JobAlreadyResolvedError):
        await manager.resolve_pull_outcome(job_id, PullOutcome.SUCCESS)


async def test_resolve_pull_outcome_increments_counter_via_job_repo() -> None:
    manager, *_ = _make_pull_manager()
    handler = FakeHandler(run_result=JobResult(status=JobStatus.QR_READY))
    manager.register_handler("ideal", handler)
    await _seed_accounts(manager, 1)
    job_id = await manager.claim_pull_account(
        "worker-1", "chat-1", "user_a", "Nguyen"
    )
    assert job_id is not None

    fake_repo = FakeJobRepo()
    manager._job_repo = fake_repo  # noqa: SLF001 — gắn thủ công sau khi tạo,
    # theo pattern test khác trong suite trực tiếp thao túng attribute
    # private (VD `manager._handlers`), tránh phải sửa constructor test.

    await manager.resolve_pull_outcome(job_id, PullOutcome.SUCCESS)

    assert ("worker-1", 1, 0, "user_a", "Nguyen") in fake_repo.calls


# ---------------------------------------------------------------------------
# ERROR handler tự động resolve_pull_error → resolve_pull_outcome(FAIL)
# ---------------------------------------------------------------------------


async def test_error_handler_auto_resolves_pull_outcome_fail() -> None:
    manager, *_ = _make_pull_manager()
    handler = FakeHandler(
        run_result=JobResult(status=JobStatus.ERROR, error_code="some_error")
    )
    manager.register_handler("ideal", handler)
    await _seed_accounts(manager, 1)

    job_id = await manager.claim_pull_account("worker-1", "chat-1", None, None)
    assert job_id is not None

    await _wait_until(
        lambda: manager.get_job(job_id).pull_outcome == PullOutcome.FAIL  # type: ignore[union-attr]
    )


# ---------------------------------------------------------------------------
# GAP fix (review 2026-07): mọi nhánh ERROR kỹ thuật trước QR_READY của job
# Pull_Mode đã ASSIGNED PHẢI chốt pull_outcome=FAIL (R13.1) — không chỉ nhánh
# handler-trả-ERROR mà cả nhánh exception bất ngờ (internal_error) và nhánh
# proxy_exhausted (khi fallback_direct tắt).
# ---------------------------------------------------------------------------


async def test_unexpected_exception_auto_resolves_pull_outcome_fail() -> None:
    """GAP-2: handler raise exception bất ngờ → `_run_handler` block
    `except Exception` set `internal_error` → job Pull_Mode ASSIGNED phải
    được `_dispatch_error_outcome` chốt `pull_outcome = FAIL` (R13.1)."""
    manager, *_ = _make_pull_manager()
    handler = FakeHandler(run_exc=RuntimeError("boom"))
    manager.register_handler("ideal", handler)
    await _seed_accounts(manager, 1)

    job_id = await manager.claim_pull_account("worker-1", "chat-1", None, None)
    assert job_id is not None

    await _wait_until(
        lambda: manager.get_job(job_id).pull_outcome == PullOutcome.FAIL  # type: ignore[union-attr]
    )
    record = manager.get_job(job_id)
    assert record is not None
    assert record.status == JobStatus.ERROR
    assert record.error_code == "internal_error"


class _ExhaustedProxyPool(_PullModeFakeProxyPool):
    """Pool luôn raise `ProxyExhaustedError` + expose `probe_config` với
    `fallback_direct_on_exhausted=False` để `_run_handler` đi vào đúng
    nhánh set `proxy_exhausted` ERROR (thay vì fallback direct — mặc định
    `_DEFAULT_FALLBACK_DIRECT=True` nên phải tắt tường minh trong test)."""

    def __init__(self) -> None:
        super().__init__()
        self.mode = "exhausted"
        from app.core.proxy_health import ProbeConfig

        self.probe_config = ProbeConfig(
            enabled=False, fallback_direct_on_exhausted=False
        )


async def test_proxy_exhausted_auto_resolves_pull_outcome_fail() -> None:
    """GAP-1: job Pull_Mode ASSIGNED chết ở bước xin proxy
    (`proxy_exhausted`, fallback direct tắt) vẫn là lỗi kỹ thuật trước
    QR_READY → phải chốt `pull_outcome = FAIL` (R13.1)."""
    settings = _PullModeFakeSettings(
        snapshot={
            "telegram.mode": "pull",
            "telegram.pull_mode.max_concurrent_jobs_per_user": 10,
        }
    )
    proxy_pool = _ExhaustedProxyPool()
    sse = FakeSse()
    manager = JobManager(settings=settings, proxy_pool=proxy_pool, sse=sse)  # type: ignore[arg-type]
    handler = FakeHandler(run_result=JobResult(status=JobStatus.QR_READY))
    manager.register_handler("ideal", handler)
    await _seed_accounts(manager, 1)

    job_id = await manager.claim_pull_account("worker-1", "chat-1", None, None)
    assert job_id is not None

    await _wait_until(
        lambda: manager.get_job(job_id).pull_outcome == PullOutcome.FAIL  # type: ignore[union-attr]
    )
    record = manager.get_job(job_id)
    assert record is not None
    assert record.status == JobStatus.ERROR
    assert record.error_code == "proxy_exhausted"
