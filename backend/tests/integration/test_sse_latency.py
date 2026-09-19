"""Integration test đo latency SSE — Requirement 8.8, 12.5.

Requirement 8.8: WHEN `Job_Status` của 1 IdealJob thay đổi hoặc có dòng log
mới, THE Backend_Service SHALL đẩy sự kiện SSE tương ứng trong vòng 2 giây
kể từ khi sự kiện xảy ra.
Requirement 12.5: WHEN 1 IdealJob chuyển trạng thái (`Job_Status` thay
đổi) hoặc có log mới, THE Frontend_App SHALL cập nhật hiển thị trong vòng
2 giây (nhất quán với Requirement 8.8).

Test này đo latency thực tế của lớp `SseBroadcaster` (`app.core.sse`) —
khoảng thời gian từ khi `broadcast_job_status()`/`broadcast_job_log()`
được gọi cho tới khi event xuất hiện ở queue của client đã subscribe. Đây
là latency tối thiểu của kênh SSE ở tầng server (chưa tính đường mạng),
nên nếu ngay lớp broadcaster đã > 2s thì Requirement 8.8 chắc chắn không
đạt.

Mọi test đều bọc `queue.get()` bằng `asyncio.wait_for(..., timeout=2.0)`
để tránh treo test khi event không tới: timeout sẽ raise `TimeoutError`
và test fail rõ ràng thay vì hang. Ngoài ra, mỗi test parse frame SSE
(`event: <type>\\ndata: <json>\\n\\n`) để verify đúng loại event và đúng
payload (`job_id`, `status`/`message`) — tránh assert bằng substring
lỏng, có thể sai sót khi format thay đổi.

Tổng thời gian toàn bộ test file kỳ vọng < 10s (thực tế < 1s trên máy dev).
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.core.sse import SseBroadcaster

# Ngưỡng latency tối đa theo Requirement 8.8 (giây). Dùng làm cả timeout
# của `asyncio.wait_for` và ngưỡng assert đo `t1 - t0`.
_LATENCY_BUDGET_SECONDS = 2.0


def _parse_sse_frame(raw: str) -> tuple[str, dict]:
    """Parse 1 frame SSE dạng `event: <type>\\ndata: <json>\\n\\n`.

    Trả về `(event_type, payload_dict)`. Raise `AssertionError` với thông
    điệp rõ ràng nếu frame không đúng format — giúp debug nhanh khi
    `SseBroadcaster.format_sse_event` bị đổi mà không cập nhật test.
    """
    lines = raw.split("\n")
    event_line = next((l for l in lines if l.startswith("event: ")), None)
    data_line = next((l for l in lines if l.startswith("data: ")), None)
    assert event_line is not None, f"SSE frame thiếu 'event:' line: {raw!r}"
    assert data_line is not None, f"SSE frame thiếu 'data:' line: {raw!r}"
    event_type = event_line[len("event: "):]
    payload = json.loads(data_line[len("data: "):])
    return event_type, payload


async def test_broadcast_job_status_latency_under_2s() -> None:
    """Test 1 — Requirement 8.8: `broadcast_job_status` tới client trong < 2s."""
    broadcaster = SseBroadcaster()
    queue = broadcaster.register_client()

    t0 = time.monotonic()
    await broadcaster.broadcast_job_status("job-1", "running")
    raw = await asyncio.wait_for(queue.get(), timeout=_LATENCY_BUDGET_SECONDS)
    t1 = time.monotonic()

    latency = t1 - t0
    assert latency < _LATENCY_BUDGET_SECONDS, (
        f"SSE job_status latency {latency:.3f}s vượt ngưỡng "
        f"{_LATENCY_BUDGET_SECONDS}s (Requirement 8.8)"
    )
    event_type, payload = _parse_sse_frame(raw)
    assert event_type == "job_status"
    assert payload["job_id"] == "job-1"
    assert payload["status"] == "running"


async def test_broadcast_job_log_latency_under_2s() -> None:
    """Test 2 — Requirement 8.8: `broadcast_job_log` tới client trong < 2s."""
    broadcaster = SseBroadcaster()
    queue = broadcaster.register_client()

    t0 = time.monotonic()
    await broadcaster.broadcast_job_log("job-1", "test message")
    raw = await asyncio.wait_for(queue.get(), timeout=_LATENCY_BUDGET_SECONDS)
    t1 = time.monotonic()

    latency = t1 - t0
    assert latency < _LATENCY_BUDGET_SECONDS, (
        f"SSE job_log latency {latency:.3f}s vượt ngưỡng "
        f"{_LATENCY_BUDGET_SECONDS}s (Requirement 8.8)"
    )
    event_type, payload = _parse_sse_frame(raw)
    assert event_type == "job_log"
    assert payload["job_id"] == "job-1"
    assert payload["message"] == "test message"


async def test_broadcast_multi_client_latency_under_2s() -> None:
    """Test 3 — Requirement 8.8: 5 client subscribe cùng lúc, 1 broadcast → tất
    cả nhận event trong < 2s.

    Kịch bản mô phỏng nhiều panel Frontend cùng mở SSE stream (Requirement
    12.3: user có thể chủ động mở nhiều panel chi tiết) — mỗi panel là 1
    client với queue riêng. Nếu broadcaster fan-out chậm/blocking thì
    client cuối cùng sẽ nhận trễ vượt 2s.

    Mỗi queue có `wait_for` riêng để cover case tồi nhất (worst-case
    latency), thay vì chỉ đo tổng thời gian gather.
    """
    broadcaster = SseBroadcaster()
    client_count = 5
    queues = [broadcaster.register_client() for _ in range(client_count)]

    t0 = time.monotonic()
    await broadcaster.broadcast_job_status("job-multi", "running")

    async def _wait_one(q: "asyncio.Queue[str]") -> str:
        return await asyncio.wait_for(q.get(), timeout=_LATENCY_BUDGET_SECONDS)

    events = await asyncio.gather(*(_wait_one(q) for q in queues))
    t1 = time.monotonic()

    latency = t1 - t0
    assert latency < _LATENCY_BUDGET_SECONDS, (
        f"SSE multi-client latency {latency:.3f}s vượt ngưỡng "
        f"{_LATENCY_BUDGET_SECONDS}s (Requirement 8.8)"
    )
    assert len(events) == client_count
    for raw in events:
        event_type, payload = _parse_sse_frame(raw)
        assert event_type == "job_status"
        assert payload["job_id"] == "job-multi"
        assert payload["status"] == "running"


async def test_broadcast_five_sequential_events_total_latency_under_2s() -> None:
    """Test 4 — Requirement 8.8: 5 event phát liên tiếp tới 1 client, tổng
    thời gian nhận đủ 5 event phải < 2s.

    Kịch bản: broadcast lần lượt 5 event `job_status` (mô phỏng Job_Status
    thay đổi liên tục qua các state PENDING → RUNNING → QR_READY, kèm log
    entries emit gần nhau). Sau đó đọc tuần tự từ queue của client. Nếu
    queue put/get của broadcaster có backlog hoặc bị starve, tổng thời
    gian sẽ vượt 2s.

    Đây là biến thể "throughput" của Requirement 8.8 — mỗi lần
    `Job_Status`/log thay đổi phải phát trong <2s, kể cả khi state thay
    đổi liên tục (scheduler emit nhiều log entry gần nhau).

    Validates Requirements: 8.8, 12.5.
    """
    broadcaster = SseBroadcaster()
    queue = broadcaster.register_client()

    event_count = 5
    statuses = ["pending", "running", "running", "running", "qr_ready"]

    t0 = time.monotonic()
    for status in statuses:
        await broadcaster.broadcast_job_status("job-seq", status)
    # Đọc lần lượt cả 5 event — mỗi lần bọc timeout riêng để không bao
    # giờ block quá 2s tổng cộng dù có event tới muộn.
    received: list[tuple[str, dict]] = []
    for _ in range(event_count):
        raw = await asyncio.wait_for(
            queue.get(), timeout=_LATENCY_BUDGET_SECONDS
        )
        received.append(_parse_sse_frame(raw))
    t1 = time.monotonic()

    total_latency = t1 - t0
    assert total_latency < _LATENCY_BUDGET_SECONDS, (
        f"Tổng latency 5 event liên tiếp {total_latency:.3f}s vượt ngưỡng "
        f"{_LATENCY_BUDGET_SECONDS}s (Requirement 8.8)"
    )
    assert len(received) == event_count
    # Assert đúng thứ tự và payload — SSE queue FIFO không được đảo event.
    for (event_type, payload), expected_status in zip(received, statuses):
        assert event_type == "job_status"
        assert payload["job_id"] == "job-seq"
        assert payload["status"] == expected_status


if __name__ == "__main__":  # pragma: no cover — cho phép chạy trực tiếp
    raise SystemExit(pytest.main([__file__, "-v"]))
