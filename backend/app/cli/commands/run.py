"""Sub-command `ideal-qr run <account_line>` — chạy 1 IdealJob end-to-end.

Requirement 15.2 (command spec), 15.5 (SSE stream realtime từ CLI qua
`CliSseConsumer` filter theo `job_id`), 15.7 (exit code 0/1/2/130),
15.8 (redact stdout — không log password/token thô).

Flow tổng quát (chi tiết trong docstring `cmd_run`):

    1. `bootstrap_cli(db_path_override)` → BootstrappedServices (share
       singleton với Backend_Service khi cùng DB path).
    2. `submit_batch("ideal", [account_line])` — batch chỉ 1 dòng.
    3. Nếu dòng bị skip (parse fail) → in reason (kèm line ĐÃ redact) qua
       stderr, exit 1.
    4. Start `CliSseConsumer` filter theo `{job_id}` để in event realtime
       (Requirement 15.5).
    5. Poll `job_manager.get_job(job_id)` mỗi 100ms; đồng thời bắt SIGINT
       → `job_manager.stop(job_id)` → exit 130 (Requirement 15.7).
    6. Khi status → terminal (`qr_ready`/`error`/`stopped`): chờ 1 tick để
       SSE event cuối được flush, in kết quả cuối qua formatter và trả
       exit code tương ứng:
       - `qr_ready` → 0 (Requirement 15.7)
       - `error` / `stopped` → 2 (Requirement 15.7)

Signal handling: `loop.add_signal_handler(SIGINT, ...)` là Unix-only. Trên
Windows `NotImplementedError` sẽ được nuốt (Fail_Fast không áp dụng —
Windows path không phải target chính của CLI, KeyboardInterrupt vẫn bubble
qua `asyncio.run()` và cho phép cleanup ở outer main.py).

Module KHÔNG import bất kỳ `app.main` / `fastapi.*` (Requirement 15.9 —
CLI process không kéo theo HTTP stack).

_Requirements: 15.2, 15.5, 15.7, 15.8_
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from typing import Any

from app.cli.bootstrap import bootstrap_cli
from app.cli.formatters import EventFormatter, JsonFormatter, TextFormatter
from app.cli.sse_consumer import CliSseConsumer
from app.core.payment_flow import JobStatus
from app.core.redaction import redact_message


__all__ = ["cmd_run", "register_parser"]


# ---------------------------------------------------------------------------
# Hằng số nội bộ — tách ra để tránh magic number rải rác trong logic poll.
# ---------------------------------------------------------------------------

# Terminal statuses (Requirement 15.7): job đến 1 trong 3 trạng thái này thì
# CLI dừng poll và trả exit code.
_TERMINAL_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.QR_READY, JobStatus.ERROR, JobStatus.STOPPED}
)

# Chu kỳ poll trạng thái Job — 100ms đủ nhanh cho UX CLI, đồng thời không
# gây CPU busy-loop. Chỉ số này nhỏ hơn nhiều so với ngưỡng "state change
# tới UI <2s" của Requirement 8.8, còn dư margin cho SSE event flush.
_POLL_INTERVAL_SECONDS = 0.1

# Số tick chờ SSE event cuối được flush sau khi job đạt terminal / sau khi
# CLI phát tín hiệu stop. `_reader_loop` của `CliSseConsumer` chạy async,
# cần vài lần yield event-loop để queue drain trước khi consumer.stop().
_SSE_FLUSH_TICKS = 2

# Exit code (Requirement 15.7).
_EXIT_OK = 0
_EXIT_CONFIG_ERROR = 1
_EXIT_JOB_ERROR = 2
_EXIT_SIGINT = 130

# Tên `payment_method` mà `run` command dispatch. `run` cố định `ideal` do
# scope hiện tại chỉ có handler này (Task 41.1 spec). Không đọc từ Settings
# vì không có key nào lưu payment_method mặc định — CLI expose tường minh.
_PAYMENT_METHOD_IDEAL = "ideal"

# Default format khi `--format` chưa được truyền (main.py task 42.1 sẽ set
# global flag; tại thời điểm này giữ fallback để `cmd_run` hoạt động độc lập).
_DEFAULT_FORMAT = "text"
_FORMAT_JSON = "json"


def _build_formatter(fmt_name: str | None) -> EventFormatter:
    """Chọn formatter theo giá trị của `--format` (case-insensitive).

    Fail_Fast (R14.2): giá trị hợp lệ do argparse `choices=` quyết định (task
    42.1). Ở đây chỉ so khớp `json` → JSON; mọi giá trị khác (bao gồm `text`,
    `None`, chuỗi rỗng do argparse default) → TextFormatter.
    """
    if isinstance(fmt_name, str) and fmt_name.lower() == _FORMAT_JSON:
        return JsonFormatter()
    return TextFormatter()


def _print_skipped(
    skipped: list[dict[str, str]], account_line: str
) -> None:
    """In danh sách dòng bị skip (parse fail) ra stderr — line ĐÃ redact.

    Áp dụng `redact_message(line, [account_line])` để mask password/token
    thô: vì `account_line` chính là dòng thô do user cung cấp, `redact_message`
    sẽ mask toàn bộ occurrence của account_line trong `line` — trong `run`
    (batch 1 dòng) điều này ẩn hoàn toàn line thô, lộ mỗi `reason` cho user.
    Đây là fail-safe theo Sensitive_Data_Redaction (R14.8, R15.8).
    """
    for item in skipped:
        raw_line = item.get("line", "")
        reason = item.get("reason", "unknown")
        safe_line = redact_message(raw_line, [account_line]) if account_line else raw_line
        print(f"skipped: {safe_line} :: {reason}", file=sys.stderr, flush=True)


def _build_final_record(
    job_id: str, record: Any, account_line: str
) -> dict[str, Any]:
    """Build dict tổng kết 1 job cho `format_batch_summary`.

    `error_message` có thể chứa account_line raw nếu handler include vào
    exception message — redact bằng `redact_message` với `account_line` làm
    known secret (Sensitive_Data_Redaction R14.8, R15.8). `artifact_path`,
    `error_code`, `status` là chuỗi cố định không nhạy cảm.
    """
    error_message = record.error_message
    if error_message and account_line:
        error_message = redact_message(error_message, [account_line])

    return {
        "job_id": job_id,
        "status": record.status.value,
        "artifact_path": record.artifact_path,
        "error_code": record.error_code,
        "error_message": error_message,
    }


async def cmd_run(args: argparse.Namespace) -> int:
    """Handler `ideal-qr run <account_line>`.

    Args:
        args: argparse Namespace, kỳ vọng các attribute:
            - `account_line` (str, positional): dòng account thô cho `ideal`.
            - `format` (str | None, global flag): `text` (default) hoặc `json`.
            - `db_path` (str | None, global flag): override DB path.

    Returns:
        Exit code theo Requirement 15.7:
            - 0 = job kết thúc `qr_ready`.
            - 1 = dòng account không parse được hoặc submit không tạo được job.
            - 2 = job kết thúc `error` hoặc `stopped` (không phải do SIGINT).
            - 130 = user Ctrl+C, đã yêu cầu stop và exit.
    """
    account_line: str = args.account_line
    fmt_name: str | None = getattr(args, "format", None) or _DEFAULT_FORMAT
    db_path_override: str | None = getattr(args, "db_path", None)

    formatter = _build_formatter(fmt_name)

    services = await bootstrap_cli(db_path_override)

    try:
        # -----------------------------------------------------------------
        # Bước 1: Submit batch 1 dòng.
        #
        # `force_rerun=True`: CLI `run` là action single-shot user chủ động
        # gọi — nếu account đã có job terminal từ session trước đó thì reset
        # record cũ về pending và chạy lại thay vì báo `job_already_exists`
        # (khác với nút "Tạo job" trên UI mặc định add-only).
        # -----------------------------------------------------------------
        submit_result = await services.job_manager.submit_batch(
            _PAYMENT_METHOD_IDEAL, [account_line], force_rerun=True
        )

        if submit_result.skipped:
            # Dòng không parse được — in reason redacted, exit 1.
            _print_skipped(submit_result.skipped, account_line)
            return _EXIT_CONFIG_ERROR

        if not submit_result.created_job_ids:
            # Guard defensive — không skip mà cũng không tạo job → bug
            # nghiêm trọng của JobManager, Fail_Fast bằng exit 1 + stderr.
            print(
                "run: submit_batch trả về created_job_ids rỗng nhưng "
                "không có dòng skipped",
                file=sys.stderr,
                flush=True,
            )
            return _EXIT_CONFIG_ERROR

        job_id = submit_result.created_job_ids[0]

        # -----------------------------------------------------------------
        # Bước 2: Setup SIGINT handler + start SSE consumer.
        # -----------------------------------------------------------------
        sigint_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        # `add_signal_handler` không hỗ trợ trên Windows — bọc để KHÔNG
        # sập CLI. Windows path chấp nhận KeyboardInterrupt bubble qua
        # `asyncio.run()` (main.py chịu trách nhiệm map thành exit 130).
        sigint_installed = False
        try:
            loop.add_signal_handler(signal.SIGINT, sigint_event.set)
            sigint_installed = True
        except NotImplementedError:
            sigint_installed = False

        consumer = CliSseConsumer(services.sse, formatter)
        await consumer.start({job_id})

        try:
            # -------------------------------------------------------------
            # Bước 3: Poll trạng thái tới terminal HOẶC SIGINT.
            # -------------------------------------------------------------
            while True:
                if sigint_event.is_set():
                    # User Ctrl+C — signal stop, chờ SSE stopped event flush
                    # rồi thoát 130 (R15.7). Không đợi handler thực sự
                    # kết thúc: JobManager sẽ cleanup khi handler exit
                    # trong background task của scheduler.
                    await services.job_manager.stop(job_id)
                    await asyncio.sleep(_POLL_INTERVAL_SECONDS * _SSE_FLUSH_TICKS)
                    return _EXIT_SIGINT

                record = services.job_manager.get_job(job_id)
                if record is None:
                    # Job biến mất khỏi JobManager — không xảy ra ở luồng
                    # bình thường (JobManager giữ record vô thời hạn trong
                    # scope process), nên đây là bug. Fail_Fast exit 2.
                    print(
                        f"run: job {job_id} không tồn tại trong JobManager",
                        file=sys.stderr,
                        flush=True,
                    )
                    return _EXIT_JOB_ERROR

                if record.status in _TERMINAL_STATUSES:
                    # Chờ 1 tick để SSE terminal event (status change cuối
                    # cùng) được `_reader_loop` của consumer flush ra stdout
                    # trước khi in bảng tổng kết.
                    await asyncio.sleep(_POLL_INTERVAL_SECONDS)

                    final_record = _build_final_record(job_id, record, account_line)
                    print(
                        formatter.format_batch_summary([final_record]),
                        flush=True,
                    )

                    if record.status == JobStatus.QR_READY:
                        return _EXIT_OK
                    return _EXIT_JOB_ERROR

                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        finally:
            # Stop SSE consumer trước khi remove signal handler để đảm bảo
            # không còn task nền nào phụ thuộc vào event loop.
            await consumer.stop()
            if sigint_installed:
                try:
                    loop.remove_signal_handler(signal.SIGINT)
                except (NotImplementedError, ValueError):
                    # `remove_signal_handler` idempotent về mặt state (đã
                    # remove thì raise ValueError) — swallow để cleanup không
                    # bao giờ raise ra ngoài finally.
                    pass
    finally:
        # Close DB engine — luôn cleanup dù cmd_run thoát thế nào. Không
        # release ProxyPool/SessionCache: cả 2 không giữ resource I/O ngoài
        # DB (proxy_pool giữ state in-memory, session_cache flush theo op).
        await services.db_engine.close()


def register_parser(subparsers: argparse._SubParsersAction) -> None:
    """Đăng ký sub-command `run` vào argparse root của CLI.

    Task 42.1 (`app/cli/main.py`) sẽ gọi hàm này với `subparsers` root để
    dựng cây argparse. Handler dispatch qua `args.handler` (set bằng
    `set_defaults(handler=cmd_run)`).
    """
    parser = subparsers.add_parser(
        "run",
        help="Chạy 1 IdealJob end-to-end cho 1 account line",
        description=(
            "Submit 1 account_line vào JobManager với payment_method='ideal', "
            "stream event realtime qua SSE, chờ tới terminal state và trả exit "
            "code 0 (qr_ready) / 2 (error/stopped) / 130 (Ctrl+C)."
        ),
    )
    parser.add_argument(
        "account_line",
        help=(
            "Dòng account thô cho payment method 'ideal', format "
            "'email|password|totp_secret' hoặc 'email|access_token' "
            "(chi tiết ở IdealFlowHandler.parse_account_line)."
        ),
    )
    parser.set_defaults(handler=cmd_run)
