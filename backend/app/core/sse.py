"""SSE broadcaster — Requirement 8.8.

Module thuộc `core/`, generic hoàn toàn — KHÔNG biết cụ thể payment method
nào, KHÔNG import bất kỳ gì từ `app.payments.*` (Payment_Module_Boundary).

Thiết kế theo design.md phần "SSE một chiều đơn giản": 1 kênh broadcast duy
nhất (không multiplexer, không scope theo user — tool chạy single-operator),
phát 2 loại event `job_status` và `job_log` tới TẤT CẢ client đang kết nối
tại endpoint `GET /api/events/stream` (route layer ở `api/routes_events.py`,
task 25.1, dùng lại `format_sse_event` ở đây để ghi ra response
`text/event-stream`).

Mỗi client subscribe nhận 1 `asyncio.Queue` riêng (pattern chuẩn cho SSE với
FastAPI/Starlette — route handler consume queue trong 1 async generator).
`broadcast()` KHÔNG block toàn bộ nếu 1 client chậm: queue có giới hạn size,
dùng `put_nowait` và bỏ qua (skip) client nào đầy queue thay vì chờ hoặc
raise lỗi làm hỏng broadcast cho các client còn lại.

Áp dụng Sensitive_Data_Redaction làm lớp an toàn double-check: `broadcast()`
luôn gọi `redact_dict()` trên `data` trước khi phát, đề phòng caller quên
redact trước khi gọi (caller chính, ví dụ `JobLogger`, vẫn PHẢI tự redact
theo design — đây chỉ là lớp phòng thủ bổ sung, không phải điểm redact duy
nhất).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

from app.core.redaction import redact_dict

# Giới hạn số event tối đa giữ trong queue của 1 client trước khi bị coi là
# "chậm" và bị bỏ qua ở lượt broadcast tiếp theo (không unregister tự động —
# unregister chỉ xảy ra khi route layer phát hiện client disconnect).
_DEFAULT_MAX_QUEUE_SIZE = 100


def format_sse_event(event_type: str, data: dict) -> str:
    """Format 1 event theo cú pháp `text/event-stream` chuẩn SSE.

    Route layer (`api/routes_events.py`) dùng lại hàm này để ghi trực tiếp
    ra response stream — tách riêng khỏi `SseBroadcaster` để có thể unit
    test format độc lập, không cần khởi tạo broadcaster.
    """
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event_type}\ndata: {payload}\n\n"


class SseBroadcaster:
    """Quản lý danh sách client SSE đang subscribe và broadcast event tới tất cả.

    Mỗi client là 1 `asyncio.Queue[str]` riêng — route handler `GET
    /api/events/stream` gọi `register_client()` khi có kết nối mới, đọc
    tuần tự từ queue trả về để yield ra response, và gọi
    `unregister_client()` trong `finally` khi client disconnect.
    """

    def __init__(self, max_queue_size: int = _DEFAULT_MAX_QUEUE_SIZE) -> None:
        self._clients: set[asyncio.Queue[str]] = set()
        self._max_queue_size = max_queue_size

    def register_client(self) -> "asyncio.Queue[str]":
        """Đăng ký 1 client mới, trả về queue để consumer đọc event tuần tự."""
        queue: "asyncio.Queue[str]" = asyncio.Queue(maxsize=self._max_queue_size)
        self._clients.add(queue)
        return queue

    def unregister_client(self, queue: "asyncio.Queue[str]") -> None:
        """Hủy đăng ký 1 client. An toàn gọi nhiều lần / với queue lạ (no-op)."""
        self._clients.discard(queue)

    async def broadcast(self, event_type: str, data: dict) -> None:
        """Phát 1 event tới TẤT CẢ client đang đăng ký.

        Redact `data` trước (double-check Sensitive_Data_Redaction), format
        thành chuỗi SSE 1 lần, rồi `put_nowait` vào từng queue. Queue của 1
        client đầy (client đọc chậm/không đọc) SHALL bị bỏ qua ở lượt này —
        KHÔNG block hoặc raise lỗi làm hỏng broadcast cho các client khác.
        """
        redacted_data = redact_dict(data)
        message = format_sse_event(event_type, redacted_data)
        for queue in list(self._clients):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                continue

    async def broadcast_job_status(self, job_id: str, status: str, **extra: object) -> None:
        """Convenience: build payload `job_status` rồi gọi `broadcast()`."""
        data = {"job_id": job_id, "status": status, **extra}
        await self.broadcast("job_status", data)

    async def broadcast_job_notified(
        self, job_id: str, entry: dict
    ) -> None:
        """Phát event `job_notified` khi TelegramNotifier gửi QR PNG xong.

        Payload gọn — chỉ `job_id` + entry mới nhất; FE tự merge vào
        `job.telegram_notifications` array trong store. KHÔNG re-broadcast
        toàn bộ list để tránh payload lớn khi 1 job có nhiều notification.

        Event mới (khác `job_status`) để FE KHÔNG trigger logic status
        transition side-effect — job đã ở terminal state (QR_READY),
        không cần re-render toast/log status change.

        Shape entry (khớp `_JobRecord.telegram_notifications`):
            {"chat_id": str, "chat_label": str, "sent_at": float,
             "success": bool, "error": dict | None}
        """
        data = {"job_id": job_id, "entry": entry}
        await self.broadcast("job_notified", data)

    async def broadcast_push_gate_updated(self, snapshot: dict) -> None:
        """Phát event `push_gate_updated` khi Push_Success_Gate đổi state.

        Payload là snapshot đầy đủ do `PushSuccessGate.snapshot()` build:
            {"enabled": bool, "threshold": int, "counter": int, "paused": bool}

        FE dùng payload này thay thế toàn bộ state cache — KHÔNG merge từng
        field vì các field có ràng buộc lẫn nhau (VD `paused=True` thì
        `counter >= threshold`), replace snapshot đảm bảo consistency.
        """
        await self.broadcast("push_gate_updated", dict(snapshot))

    async def broadcast_live_qr_gate_updated(self, snapshot: dict) -> None:
        """SSE `live_qr_gate_updated` — Live QR capacity gate snapshot.

        Payload from LiveQrGate.snapshot():
            {enabled, max_per_chat, live, capacity, blocked, per_chat}
        """
        await self.broadcast("live_qr_gate_updated", dict(snapshot))

    async def broadcast_setting_updated(
        self, key: str, value: object, source_client_id: str | None = None
    ) -> None:
        """Phát event `setting_updated` khi 1 setting key được ghi qua API.

        Dùng cho các setting có thuộc tính "chia sẻ real-time giữa nhiều
        client" — VD `ui.input_draft` (textarea account chung). Client
        nào có LogPanel/InputPanel bind key này sẽ update state ngay khi
        client khác edit.

        `source_client_id` (optional) để client tự lọc echo — client vừa PUT
        setting sẽ nhận SSE event của chính mình, có thể skip update nếu
        `client_id` trùng để tránh cursor jitter / dup input event.
        """
        data: dict[str, object] = {"key": key, "value": value}
        if source_client_id is not None:
            data["source_client_id"] = source_client_id
        await self.broadcast("setting_updated", data)

    async def broadcast_job_log(self, job_id: str, message: str, **extra: object) -> None:
        """Convenience: build payload `job_log` rồi gọi `broadcast()`.

        `ts` được tự set = `asyncio.get_event_loop().time()` fallback về
        wall-clock nếu caller không truyền — FE cần epoch seconds để format
        `HH:MM:SS`. Caller (VD `SseLogHandler`) đã truyền `ts` từ
        `record.created` (wall-clock chuẩn epoch) sẽ override được.
        """
        import time as _time

        data: dict[str, object] = {
            "job_id": job_id,
            "message": message,
            "ts": _time.time(),
            **extra,  # extra có `ts` sẽ override giá trị mặc định trên
        }
        await self.broadcast("job_log", data)


# ---------------------------------------------------------------------------
# SseLogHandler — bridge stdlib logging → SSE broadcast
# ---------------------------------------------------------------------------


class SseLogHandler(logging.Handler):
    """Handler gắn vào logger per-job để broadcast mọi `logger.info(...)` qua SSE.

    Cơ chế:
    - Khi factory (do `make_sse_logger_factory` trả về) được gọi với tên
      logger `<namespace>.<step>.<job_id>` (ví dụ `ideal.flow.abc123`), nó
      tạo `LoggerAdapter` với `extra={"job_id": <job_id>}`.
    - Handler đọc `record.job_id` — nếu có → append vào buffer log của job
      (qua `job_manager.record_log_line`) + broadcast job_log qua SSE.
    - Broadcast là async → phải schedule qua `asyncio.run_coroutine_threadsafe`
      trên main event loop (handler `emit` là sync).

    Fail-safe: mọi exception bên trong `emit()` được swallow (log stdlib
    contract) — không được raise ra ngoài caller `logger.info(...)`, tránh
    làm hỏng flow business chỉ vì broadcast SSE fail.
    """

    def __init__(
        self,
        sse: SseBroadcaster,
        loop: asyncio.AbstractEventLoop,
        # Callback trả `bool | None`:
        #   True → record tồn tại, log đã lưu, tiếp tục broadcast SSE.
        #   False → record đã bị pop khỏi map, SKIP broadcast tránh
        #     tạo entry ghost ở FE.
        #   None → backward compat cho implementations cũ không return
        #     (được coi là "alive", giữ behavior broadcast).
        record_log_line: Callable[[str, str, float, str], bool | None] | None = None,
    ) -> None:
        super().__init__()
        self._sse = sse
        self._loop = loop
        # Callable (thường là JobManager.record_log_line) để lưu vào buffer
        # của job. Optional — nếu None, chỉ broadcast SSE, không persist log
        # cho JobDetailPanel refresh về sau.
        self._record_log_line = record_log_line

    def emit(self, record: logging.LogRecord) -> None:
        job_id = getattr(record, "job_id", None)
        if not isinstance(job_id, str) or not job_id:
            return
        try:
            msg = self.format(record)
        except Exception:
            try:
                msg = record.getMessage()
            except Exception:
                return

        level = record.levelname.lower() if record.levelname else "info"
        ts = record.created

        # Lưu vào buffer log của job (nếu callback đã inject) — sync mutation
        # trên dict/list an toàn ở đây vì handler emit chạy trên cùng event
        # loop thread với coroutine business (Python asyncio single-thread).
        #
        # Callback trả `bool`: True nếu record tồn tại, False nếu job đã bị
        # pop khỏi `JobManager._jobs` (delete_job / cleanup). Khi False,
        # SKIP broadcast SSE — nếu vẫn broadcast, FE `applyLogEvent` sẽ
        # TỰ TẠO entry ghost trong Map (fallback "if !existing → create
        # shell"), dẫn tới toast `job_not_found` khi user click vào job
        # ghost. Đây là fix triệt để cho race handler_task còn log sau
        # khi user delete job.
        job_alive = True
        if self._record_log_line is not None:
            try:
                result = self._record_log_line(job_id, msg, ts, level)
                # Backward compat: implementations cũ trả `None` được coi
                # là alive (giữ behavior broadcast).
                if result is False:
                    job_alive = False
            except Exception:
                pass

        if not job_alive:
            return

        try:
            asyncio.run_coroutine_threadsafe(
                self._sse.broadcast_job_log(
                    job_id, msg, ts=ts, level=level
                ),
                self._loop,
            )
        except Exception:
            # SSE broadcast là best-effort — không được sập caller.
            pass


class _JobLoggerAdapter(logging.LoggerAdapter):
    """LoggerAdapter chỉ merge extra `job_id` vào record — chấp nhận
    `logger.info(msg, *args, extra=...)` mà không override job_id đã set."""

    def process(self, msg, kwargs):  # type: ignore[override]
        merged_extra = dict(self.extra or {})
        caller_extra = kwargs.get("extra")
        if isinstance(caller_extra, dict):
            merged_extra.update(caller_extra)
        kwargs["extra"] = merged_extra
        return msg, kwargs


def make_sse_logger_factory(
    sse: SseBroadcaster,
    loop: asyncio.AbstractEventLoop,
    record_log_line: Callable[[str, str, float, str], None] | None = None,
    job_id_extract: Callable[[str], str | None] | None = None,
) -> Callable[[str], logging.Logger]:
    """Tạo `logger_factory` inject SSE handler tự động vào logger per-job.

    Args:
        sse: SseBroadcaster singleton (từ bootstrap).
        loop: Main event loop (từ `asyncio.get_event_loop()` khi bootstrap).
        job_id_extract: Callable custom parse job_id từ tên logger. Mặc định
            lấy segment cuối cùng nếu tên có ≥3 phần (`ideal.flow.<jobid>`).

    Returns:
        Callable[[str], Logger] dùng làm `logger_factory` cho
        `register_ideal_handler(..., logger_factory=...)`. Trả về logger
        (hoặc adapter) đã gắn `SseLogHandler` singleton — mọi `logger.info`
        của module payment sẽ broadcast qua SSE.
    """
    handler = SseLogHandler(sse, loop, record_log_line=record_log_line)
    handler.setLevel(logging.INFO)
    # Formatter tối thiểu — chỉ cần message (LogPanel FE format ts riêng).
    handler.setFormatter(logging.Formatter("%(message)s"))

    def _default_extract(name: str) -> str | None:
        parts = name.split(".")
        # Ví dụ `ideal.flow.abc123` → `abc123`. Cần ≥ 3 phần để tránh match
        # logger namespace generic (VD `app.core.job_manager`).
        if len(parts) >= 3:
            candidate = parts[-1]
            # job_id thực tế là hex 32 ký tự, nhưng KHÔNG hardcode length
            # tại đây để giữ tương thích với `check_plan.<jobid>` v.v.
            if candidate and not candidate.startswith("_"):
                return candidate
        return None

    extract = job_id_extract or _default_extract

    def factory(name: str) -> logging.Logger:
        base = logging.getLogger(name)
        # Đảm bảo handler gắn đúng 1 lần cho mỗi logger name.
        if handler not in base.handlers:
            base.addHandler(handler)
            base.setLevel(logging.INFO)
            # KHÔNG propagate lên root — tránh log ra stdout 2 lần khi
            # bootstrap.py đã cấu hình root logger (uvicorn access log).
            # Vẫn giữ propagate = True để dev thấy log stdout cùng SSE.

        job_id = extract(name)
        if job_id:
            return _JobLoggerAdapter(base, {"job_id": job_id})  # type: ignore[return-value]
        return base

    return factory
