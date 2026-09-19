"""Formatter cho output CLI — text/JSONL (Requirement 15.6).

Module khai báo 3 hình thái formatter tương ứng 3 loại output CLI cần
render:

- `format_event(event_type, data)` — 1 event SSE (`job_status` /
  `job_log` / event type khác) → 1 dòng string. Caller (`stream_cmd`) tự
  thêm newline khi `print`.
- `format_settings_map(settings)` — toàn bộ map settings hiện tại (dict
  key → value) → string đa dòng, phục vụ `settings get`/`settings list`.
- `format_batch_summary(records)` — bảng tổng kết kết quả `run-batch`
  sau khi terminal (mỗi record ứng với 1 job đã kết thúc: qr_ready /
  error / stopped).

Payload event từ `SseBroadcaster.broadcast` đã tự đi qua `redact_dict`
(xem `core/sse.py`), formatter KHÔNG redact lại — chỉ format nguyên
vẹn dữ liệu nhận được. Settings map có thể chứa secret (vd
`web.auth_token`) — caller (`settings_cmd`) tự chịu trách nhiệm redact
trước khi truyền vào formatter, không phải job của formatter.

Format text log tuân đúng Requirement 15.6:
  `[{iso_ts}] [{job_id}] [{level}] {message}`
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Protocol


__all__ = ["EventFormatter", "TextFormatter", "JsonFormatter"]


# Format ISO timestamp có millisecond, không kèm timezone — CLI đơn giản,
# in ra để user đọc log local. Không dùng UTC/`Z` vì user chạy trực tiếp
# trên máy operator, timestamp local dễ đối chiếu với log OS.
_ISO_TS_TIMESPEC = "milliseconds"

# Default level khi event `job_log` không kèm `level` trong extra — theo
# yêu cầu Task 40.1 (fallback INFO).
_DEFAULT_LEVEL = "INFO"

# Cột STATUS trong bảng batch summary — pad đến 10 ký tự (đủ chứa
# `qr_ready`/`error`/`stopped`/`in_progress`).
_STATUS_COL_WIDTH = 10

# Cột JOB_ID: UUID chuẩn 36 ký tự.
_JOB_ID_COL_WIDTH = 36


def _iso_ts_from_epoch(ts: Any) -> str:
    """Convert `ts` (epoch seconds float/int) → ISO string với millisecond.

    Trả về ISO của `datetime.now()` khi `ts` không hợp lệ (thiếu, kiểu
    khác float/int, hoặc raise `OSError`/`OverflowError` vì giá trị lạ) —
    formatter KHÔNG được crash chỉ vì log event dị thường.
    """
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts).isoformat(timespec=_ISO_TS_TIMESPEC)
        except (OSError, OverflowError, ValueError):
            pass
    return datetime.now().isoformat(timespec=_ISO_TS_TIMESPEC)


def _artifact_or_error(record: dict) -> str:
    """Cột cuối của bảng batch summary — tuỳ status hiển thị artifact hay error.

    - `qr_ready`: hiện `artifact_path` (fallback `-` nếu thiếu).
    - `error`/`stopped`: hiện `error_code: error_message` (bỏ phần vắng).
    - Còn lại: `-`.
    """
    status = record.get("status") or ""
    if status == "qr_ready":
        return str(record.get("artifact_path") or "-")
    if status in {"error", "stopped"}:
        code = record.get("error_code")
        message = record.get("error_message")
        if code and message:
            return f"{code}: {message}"
        if code:
            return str(code)
        if message:
            return str(message)
        return "-"
    return "-"


class EventFormatter(Protocol):
    """Interface formatter — 3 hình thái output CLI cần render."""

    def format_event(self, event_type: str, data: dict) -> str: ...

    def format_settings_map(self, settings: dict) -> str: ...

    def format_batch_summary(self, records: list[dict]) -> str: ...


class TextFormatter:
    """Human-readable formatter — log format Requirement 15.6.

    Không giữ state, an toàn tạo mỗi lệnh CLI 1 instance riêng.
    """

    def format_event(self, event_type: str, data: dict) -> str:
        if event_type == "job_log":
            iso_ts = _iso_ts_from_epoch(data.get("ts"))
            job_id = data.get("job_id", "-")
            level = data.get("level", _DEFAULT_LEVEL)
            message = data.get("message", "")
            return f"[{iso_ts}] [{job_id}] [{level}] {message}"

        if event_type == "job_status":
            iso_ts = _iso_ts_from_epoch(data.get("ts"))
            job_id = data.get("job_id", "-")
            status = data.get("status", "-")
            line = f"[{iso_ts}] [{job_id}] [STATUS] {status}"
            error_code = data.get("error_code")
            error_message = data.get("error_message")
            if error_code or error_message:
                parts: list[str] = []
                if error_code:
                    parts.append(f"error_code={error_code}")
                if error_message:
                    parts.append(f"error_message={error_message}")
                line = f"{line} ({', '.join(parts)})"
            return line

        # Event type khác (dự phòng — schema hiện chỉ có 2 loại trên).
        iso_ts = _iso_ts_from_epoch(data.get("ts"))
        payload = json.dumps(data, ensure_ascii=False, default=str, sort_keys=True)
        return f"[{iso_ts}] [{event_type}] {payload}"

    def format_settings_map(self, settings: dict) -> str:
        # Sort keys để output deterministic — dễ diff/grep. Value dùng
        # `json.dumps` để giữ nguyên dạng: list_str hiện `["a","b"]`,
        # bool hiện `true/false`, number hiện không quote, string quoted.
        lines = [
            f"{key} = {json.dumps(settings[key], ensure_ascii=False, default=str)}"
            for key in sorted(settings.keys())
        ]
        return "\n".join(lines)

    def format_batch_summary(self, records: list[dict]) -> str:
        header = (
            f"{'JOB_ID':<{_JOB_ID_COL_WIDTH}} | "
            f"{'STATUS':<{_STATUS_COL_WIDTH}} | "
            "ARTIFACT / ERROR"
        )
        separator = "-" * len(header)
        rows = [
            f"{str(record.get('job_id', '-')):<{_JOB_ID_COL_WIDTH}} | "
            f"{str(record.get('status', '-')):<{_STATUS_COL_WIDTH}} | "
            f"{_artifact_or_error(record)}"
            for record in records
        ]
        return "\n".join([header, separator, *rows])


class JsonFormatter:
    """JSONL formatter (pipe-safe) — 1 event = 1 dòng JSON độc lập.

    Không giữ state, an toàn tạo mỗi lệnh CLI 1 instance riêng.
    """

    def format_event(self, event_type: str, data: dict) -> str:
        return json.dumps(
            {"event": event_type, "data": data},
            default=str,
            ensure_ascii=False,
        )

    def format_settings_map(self, settings: dict) -> str:
        return json.dumps(
            settings,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )

    def format_batch_summary(self, records: list[dict]) -> str:
        return json.dumps(
            {"summary": records, "total": len(records)},
            indent=2,
            ensure_ascii=False,
            default=str,
        )
