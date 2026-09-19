"""Adapter subscribe SseBroadcaster → in event ra stdout (Requirement 15.5).

`CliSseConsumer` cắm vào `SseBroadcaster` (cùng singleton mà Backend_Service
dùng), filter event theo `job_ids` để CHỈ in event của job do CLI submit —
tránh nhiễu output khi CLI chạy song song với Backend_Service trên cùng DB
(Requirement 15.5, 15.10).

Consumer nhận `str` từ queue — đó là message ĐÃ được `format_sse_event()`
format sẵn theo cú pháp SSE (`event: <type>\\ndata: <json>\\n\\n`). Để filter
theo `job_id` và đưa vào `EventFormatter`, consumer parse ngược lại chuỗi
SSE → tách `event_type` + `data` (dict). Đây là chi phí bắt buộc do
`SseBroadcaster` được thiết kế generic cho HTTP route (task 25.1) — CLI
subscribe cùng broadcaster để dùng lại toàn bộ pipeline event thay vì tạo
kênh mới song song (SOLID/DRY).
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import TextIO

from app.cli.formatters import EventFormatter
from app.core.sse import SseBroadcaster


__all__ = ["CliSseConsumer"]


# Prefix của 2 dòng trong 1 SSE frame do `format_sse_event()` sinh ra.
# Tách hằng số để tránh magic number cho slice index (`line[len(prefix):]`).
_EVENT_PREFIX = "event: "
_DATA_PREFIX = "data: "


class CliSseConsumer:
    """SSE consumer cho CLI — subscribe broadcaster + in event lọc theo job_ids.

    Không tự khởi tạo broadcaster/formatter — cả 2 được inject để:

    - CLI (main.py) và Backend_Service share cùng 1 `SseBroadcaster` singleton
      (Requirement 15.5 — CLI dùng lại pipeline event của backend).
    - Test/CLI chọn formatter (text/json) qua flag `--format` (Requirement 15.6).
    - Test bắt output qua `io.StringIO` bằng cách pass `stream=`.
    """

    def __init__(
        self,
        broadcaster: SseBroadcaster,
        formatter: EventFormatter,
        stream: TextIO | None = None,
    ) -> None:
        self._broadcaster = broadcaster
        self._formatter = formatter
        self._stream: TextIO = stream if stream is not None else sys.stdout

        self._queue: asyncio.Queue[str] | None = None
        self._task: asyncio.Task[None] | None = None
        self._job_ids: set[str] = set()

    async def start(self, job_ids: set[str]) -> None:
        """Đăng ký queue với broadcaster + chạy task nền in event.

        Fail-fast nếu đã start rồi — không cho phép double-start để tránh
        rò task nền / queue mồ côi (Fail_Fast, không fallback che lỗi).
        """
        if self._task is not None:
            raise RuntimeError("CliSseConsumer already started")

        # Copy sang set mới để cô lập với mutation từ caller sau khi start.
        self._job_ids = set(job_ids)
        self._queue = self._broadcaster.register_client()
        self._task = asyncio.create_task(self._reader_loop())

    async def stop(self) -> None:
        """Cancel task nền + unregister queue + reset state.

        Idempotent: gọi khi chưa start (hoặc đã stop) là no-op — hữu ích
        cho code cleanup đặt trong `finally` mà không cần check trạng thái.
        """
        task = self._task
        queue = self._queue

        if task is not None:
            task.cancel()
            # `gather(..., return_exceptions=True)` nuốt CancelledError để
            # `stop()` không raise ra ngoài khi task bị cancel bình thường.
            await asyncio.gather(task, return_exceptions=True)

        if queue is not None:
            self._broadcaster.unregister_client(queue)

        self._task = None
        self._queue = None
        self._job_ids = set()

    async def _reader_loop(self) -> None:
        """Task nền: đọc queue → parse SSE → filter theo job_id → format → print."""
        assert self._queue is not None, "start() phải khởi tạo _queue trước reader_loop"
        queue = self._queue

        while True:
            try:
                raw = await queue.get()
            except asyncio.CancelledError:
                # `stop()` đã cancel — thoát loop gracefully, không re-raise
                # để `asyncio.gather(..., return_exceptions=True)` ở stop()
                # nhận kết quả sạch.
                break

            parsed = _parse_sse_frame(raw)
            if parsed is None:
                # Frame malformed (không có `event:` / `data:` / JSON hỏng) —
                # log-and-drop, KHÔNG kill loop (log-and-continue chỉ áp
                # dụng cho lỗi parse frame nội bộ, không phải fallback che
                # lỗi logic).
                print(
                    f"[CliSseConsumer] bỏ qua frame SSE malformed: {raw!r}",
                    file=sys.stderr,
                    flush=True,
                )
                continue

            event_type, data = parsed

            # Filter theo job_id: nếu event không gắn job_id hoặc job_id
            # không thuộc set CLI đang theo dõi → skip (tránh nhiễu output
            # từ job của CLI/backend khác chạy song song — Requirement 15.5).
            job_id = data.get("job_id")
            if not isinstance(job_id, str) or job_id not in self._job_ids:
                continue

            formatted = self._formatter.format_event(event_type, data)
            print(formatted, file=self._stream, flush=True)


def _parse_sse_frame(raw: str) -> tuple[str, dict] | None:
    """Parse chuỗi SSE do `format_sse_event()` sinh ra → (event_type, data).

    Trả về `None` nếu frame malformed (thiếu `event:`/`data:` hoặc JSON hỏng)
    — caller xử lý log-and-drop, không raise để không kill task nền.
    """
    event_type: str | None = None
    data_json: str | None = None

    for line in raw.split("\n"):
        if line.startswith(_EVENT_PREFIX):
            event_type = line[len(_EVENT_PREFIX):].strip()
        elif line.startswith(_DATA_PREFIX):
            # KHÔNG strip toàn phần: chỉ trim khoảng trắng đầu/cuối do SSE
            # spec không thêm padding vào phần data (json.dumps output
            # không có leading/trailing whitespace, nhưng strip cho chắc).
            data_json = line[len(_DATA_PREFIX):]

    if event_type is None or data_json is None:
        return None

    try:
        data = json.loads(data_json)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    return event_type, data
