"""Cancellation race helpers for UPI Direct long awaits."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from app.core.payment_flow import Job, JobResult, JobStatus

_STOPPED = JobResult(status=JobStatus.STOPPED)
_PAUSED = JobResult(status=JobStatus.STOPPED, pause_requested=True)


def check_stop(job: Job) -> JobResult | None:
    if job.cancellation_token.is_cancelled():
        return _STOPPED
    return None


def check_stop_or_pause(job: Job) -> JobResult | None:
    if job.cancellation_token.is_cancelled():
        return _STOPPED
    if job.cancellation_token.is_paused():
        return _PAUSED
    return None


async def await_cancelable(job: Job, awaitable: Any) -> Any:
    """Race an awaitable against cancel; cancel/await the loser; re-raise task cancel."""
    io_task = asyncio.ensure_future(awaitable)
    cancel_task = asyncio.create_task(job.cancellation_token.wait_cancelled())
    try:
        done, pending = await asyncio.wait(
            {io_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        io_task.cancel()
        cancel_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await io_task
        with contextlib.suppress(asyncio.CancelledError):
            await cancel_task
        raise

    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    if job.cancellation_token.is_cancelled() and (
        cancel_task in done or not io_task.done() or io_task.cancelled()
    ):
        if not io_task.done() or io_task.cancelled():
            raise asyncio.CancelledError()
        if cancel_task in done:
            raise asyncio.CancelledError()
    return io_task.result()


__all__ = ["check_stop", "check_stop_or_pause", "await_cancelable"]
