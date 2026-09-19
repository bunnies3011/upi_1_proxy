"""Unit test cho `core/payment_flow.py` (task 8.1).

Kiểm tra các kiểu dữ liệu thuần (JobStatus, Job, JobResult) và
`SimpleCancellationToken` hoạt động đúng — không cần mock, không phụ thuộc
module nào khác của `payments/ideal/`.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.core.payment_flow import (
    AccountLineError,
    Job,
    JobResult,
    JobStatus,
    ParsedAccount,
    ProxyLeaseHealth,
    SimpleCancellationToken,
)


def test_job_status_values() -> None:
    assert JobStatus.PENDING == "pending"
    assert JobStatus.RUNNING == "running"
    assert JobStatus.QR_READY == "qr_ready"
    assert JobStatus.ERROR == "error"
    assert JobStatus.STOPPED == "stopped"
    assert {status.value for status in JobStatus} == {
        "pending",
        "running",
        "qr_ready",
        "error",
        "stopped",
    }


def test_job_is_frozen_dataclass_with_expected_fields() -> None:
    token = SimpleCancellationToken()
    job = Job(
        job_id="job-1",
        payment_method="ideal",
        account_line="user@example.com|secret",
        created_at=1234.5,
        cancellation_token=token,
    )

    assert job.job_id == "job-1"
    assert job.payment_method == "ideal"
    assert job.account_line == "user@example.com|secret"
    assert job.created_at == 1234.5
    assert job.cancellation_token is token
    assert dataclasses.is_dataclass(job)

    with_raises = False
    try:
        job.job_id = "job-2"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        with_raises = True
    assert with_raises


def test_job_result_defaults() -> None:
    result = JobResult(status=JobStatus.QR_READY)

    assert result.status == JobStatus.QR_READY
    assert result.artifact_path is None
    assert result.error_code is None
    assert result.error_message is None
    assert result.plan is None


def test_job_result_with_error_fields() -> None:
    result = JobResult(
        status=JobStatus.ERROR,
        error_code="login_failed",
        error_message="invalid credential",
    )

    assert result.status == JobStatus.ERROR
    assert result.artifact_path is None
    assert result.error_code == "login_failed"
    assert result.error_message == "invalid credential"
    assert result.plan is None


def test_job_result_with_plan_hint() -> None:
    result = JobResult(
        status=JobStatus.ERROR,
        error_code="oaipay_already_paid",
        error_message="User is already paid",
        plan="plus",
    )
    assert result.plan == "plus"


def test_parsed_account_and_account_line_error_are_dataclasses() -> None:
    parsed = ParsedAccount(raw_line="user@example.com|secret")
    assert parsed.raw_line == "user@example.com|secret"

    error = AccountLineError(line="bad-line", reason="missing separator")
    assert error.line == "bad-line"
    assert error.reason == "missing separator"


def test_simple_cancellation_token_starts_not_cancelled() -> None:
    token = SimpleCancellationToken()
    assert token.is_cancelled() is False


def test_simple_cancellation_token_cancel_then_is_cancelled_true() -> None:
    token = SimpleCancellationToken()
    token.cancel()
    assert token.is_cancelled() is True


def test_simple_cancellation_token_cancel_is_idempotent() -> None:
    token = SimpleCancellationToken()
    token.cancel()
    token.cancel()
    assert token.is_cancelled() is True


# ---------------------------------------------------------------------------
# Awaitable cancellation + explicit proxy lease health (Phase 1 core)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_cancelled_resolves_after_cancel() -> None:
    """AC-1: wait_cancelled() resolves promptly after cancel()."""
    import asyncio

    token = SimpleCancellationToken()
    wait_task = asyncio.create_task(token.wait_cancelled())
    await asyncio.sleep(0)  # let wait_task start and block
    assert not wait_task.done()
    token.cancel()
    await asyncio.wait_for(wait_task, timeout=0.5)
    assert wait_task.done()


@pytest.mark.asyncio
async def test_wait_cancelled_does_not_resolve_on_pause_alone() -> None:
    """AC-1: pause() alone must not unblock wait_cancelled()."""
    import asyncio

    token = SimpleCancellationToken()
    wait_task = asyncio.create_task(token.wait_cancelled())
    await asyncio.sleep(0)
    token.pause()
    await asyncio.sleep(0.05)
    assert not wait_task.done()
    assert token.is_paused() is True
    assert token.is_cancelled() is False
    # Cleanup: cancel so the wait task can finish and not leak.
    token.cancel()
    await asyncio.wait_for(wait_task, timeout=0.5)


def test_proxy_lease_health_enum_and_job_result_field() -> None:
    """AC-2: ProxyLeaseHealth ALIVE/DEAD; JobResult defaults + round-trip."""
    assert ProxyLeaseHealth.ALIVE == "alive"
    assert ProxyLeaseHealth.DEAD == "dead"
    assert {m.value for m in ProxyLeaseHealth} == {"alive", "dead"}

    bare = JobResult(status=JobStatus.QR_READY)
    assert bare.proxy_lease_health is None

    alive = JobResult(
        status=JobStatus.QR_READY,
        artifact_path="/tmp/qr.png",
        proxy_lease_health=ProxyLeaseHealth.ALIVE,
    )
    assert alive.proxy_lease_health is ProxyLeaseHealth.ALIVE

    dead = JobResult(
        status=JobStatus.ERROR,
        error_code="login_failed",
        proxy_lease_health=ProxyLeaseHealth.DEAD,
    )
    assert dead.proxy_lease_health is ProxyLeaseHealth.DEAD
