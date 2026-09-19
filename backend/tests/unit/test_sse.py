"""Unit test cơ bản cho `core/sse.py` — verify register/broadcast/unregister.

Không có property test riêng cho module này (task 8.2 không yêu cầu).
"""

from __future__ import annotations

import json

import pytest

from app.core.sse import SseBroadcaster, format_sse_event


def _parse_sse_message(message: str) -> tuple[str, dict]:
    lines = message.strip("\n").split("\n")
    event_line, data_line = lines[0], lines[1]
    event_type = event_line.removeprefix("event: ")
    data = json.loads(data_line.removeprefix("data: "))
    return event_type, data


@pytest.mark.asyncio
async def test_broadcast_delivers_event_to_all_registered_clients() -> None:
    broadcaster = SseBroadcaster()
    queue_a = broadcaster.register_client()
    queue_b = broadcaster.register_client()

    await broadcaster.broadcast("job_status", {"job_id": "job-1", "status": "running"})

    for queue in (queue_a, queue_b):
        message = queue.get_nowait()
        event_type, data = _parse_sse_message(message)
        assert event_type == "job_status"
        assert data == {"job_id": "job-1", "status": "running"}


@pytest.mark.asyncio
async def test_broadcast_job_status_and_job_log_build_correct_payload() -> None:
    broadcaster = SseBroadcaster()
    queue = broadcaster.register_client()

    await broadcaster.broadcast_job_status("job-1", "qr_ready")
    await broadcaster.broadcast_job_log("job-1", "starting checkout")

    status_type, status_data = _parse_sse_message(queue.get_nowait())
    log_type, log_data = _parse_sse_message(queue.get_nowait())

    assert status_type == "job_status"
    assert status_data == {"job_id": "job-1", "status": "qr_ready"}
    assert log_type == "job_log"
    # `broadcast_job_log` tự set `ts` = epoch seconds (time.time()) khi caller
    # không truyền — non-deterministic nên pop ra + assert type thay vì so
    # sánh giá trị chính xác, phần payload còn lại phải khớp nguyên vẹn.
    log_ts = log_data.pop("ts")
    assert isinstance(log_ts, (int, float))
    assert log_data == {"job_id": "job-1", "message": "starting checkout"}


@pytest.mark.asyncio
async def test_broadcast_redacts_sensitive_fields_in_payload() -> None:
    broadcaster = SseBroadcaster()
    queue = broadcaster.register_client()

    await broadcaster.broadcast("job_log", {"job_id": "job-1", "access_token": "raw-secret"})

    _, data = _parse_sse_message(queue.get_nowait())
    assert data["access_token"] == "***REDACTED***"


@pytest.mark.asyncio
async def test_unregister_client_then_broadcast_does_not_raise() -> None:
    broadcaster = SseBroadcaster()
    queue = broadcaster.register_client()

    broadcaster.unregister_client(queue)

    await broadcaster.broadcast("job_status", {"job_id": "job-1", "status": "error"})
    assert queue.empty()


def test_format_sse_event_matches_text_event_stream_syntax() -> None:
    message = format_sse_event("job_status", {"job_id": "job-1"})

    assert message == 'event: job_status\ndata: {"job_id": "job-1"}\n\n'
