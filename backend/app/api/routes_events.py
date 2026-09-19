"""Route SSE — `GET /api/events/stream` (Requirements 8.8, 12.5).

Endpoint duy nhất phát 2 loại event `job_status` và `job_log` (đã được
`SseBroadcaster.broadcast()` format sẵn theo cú pháp `text/event-stream`)
tới TẤT CẢ client đang kết nối. Tool chạy single-operator, không cần scope
theo user — thiết kế "SSE một chiều đơn giản" theo design.md.

Cơ chế subscribe (theo hợp đồng của `core.sse.SseBroadcaster`):

1. `queue = sse.register_client()` — nhận `asyncio.Queue[str]` riêng, mỗi
   client 1 queue độc lập.
2. Vòng lặp `await queue.get()` để consume event tuần tự và `yield` ra
   response. `SseBroadcaster.broadcast()` đã `put_nowait` chuỗi SSE đã
   format vào queue, nên route KHÔNG format lại — chỉ chuyển tiếp.
3. `finally: sse.unregister_client(queue)` — luôn cleanup khi client
   disconnect (`asyncio.CancelledError` do Starlette bắn ra) hoặc khi có
   exception; queue rời khỏi tập subscriber, không nhận event nữa.

Keepalive: sau mỗi 15 giây không có event thực (`asyncio.wait_for` hết
timeout → raise `asyncio.TimeoutError`), phát 1 comment SSE `":\n\n"` để
giữ kết nối sống qua proxy/load balancer có idle timeout ngắn. Comment SSE
(dòng bắt đầu bằng `:`) bị client bỏ qua theo spec HTML5 SSE, không ảnh
hưởng logic dispatch ở phía frontend.

Auth: đã gỡ bỏ hoàn toàn — tool chạy trên mạng nội bộ tin cậy, không còn
kiểm tra token trước khi bắt đầu stream.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.api import deps
from app.core.sse import SseBroadcaster

# Chu kỳ keepalive (giây) — nhỏ hơn idle timeout mặc định của các proxy phổ
# biến (nginx ~60s, cloud LB ~30s). 15s cho biên độ an toàn 2×.
_KEEPALIVE_INTERVAL_SECONDS = 15.0

# Comment SSE giữ kết nối sống. Theo spec HTML5 SSE, dòng bắt đầu bằng ':'
# là comment và bị client bỏ qua — không phát tán data giả, không gây
# nhiễu handler `job_status`/`job_log` ở frontend.
_KEEPALIVE_COMMENT = ":\n\n"

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get(
    "/stream",
)
async def stream_events(
    sse: SseBroadcaster = Depends(deps.get_sse),
) -> StreamingResponse:
    """Mở kênh SSE và stream event `job_status`/`job_log` cho client.

    Response giữ mở đến khi client disconnect (Starlette raise
    `CancelledError` vào generator). Generator cleanup queue qua
    `unregister_client` trong `finally` bất kể lý do kết thúc.
    """

    queue = sse.register_client()

    async def event_generator():
        try:
            while True:
                try:
                    # Chờ event thực tối đa `_KEEPALIVE_INTERVAL_SECONDS`
                    # trước khi bắn keepalive — cho phép rời trạng thái
                    # idle mà không đợi thêm event nào.
                    message = await asyncio.wait_for(
                        queue.get(), timeout=_KEEPALIVE_INTERVAL_SECONDS
                    )
                except asyncio.TimeoutError:
                    yield _KEEPALIVE_COMMENT
                    continue
                yield message
        finally:
            sse.unregister_client(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            # Tránh middleware cache / proxy buffer làm trễ event.
            "Cache-Control": "no-cache",
            # Hint đặc thù cho nginx tắt output buffering trên response
            # streaming — vô hại với các proxy khác.
            "X-Accel-Buffering": "no",
        },
    )
