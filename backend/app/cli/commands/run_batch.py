"""Sub-command `ideal-qr run-batch <path>` — chạy N IdealJob song song.

Flow:
1. Đọc file UTF-8, split `\\n`, trim, filter dòng rỗng và comment `#`.
2. `bootstrap_cli(db_path_override)` → `BootstrappedServices` (Requirement
   15.3, 15.4).
3. `job_manager.submit_batch("ideal", lines)` → `BatchSubmitResult`
   (Requirement 8.1, 8.2).
4. Nếu `skipped` non-empty: in danh sách skip (đã redact) ra stderr —
   `skipped` KHÔNG làm exit ngay, chỉ log warning vì batch có thể mix dòng
   hợp lệ + không hợp lệ (Requirement 15.7 chỉ exit 1 khi lỗi cấu hình toàn
   cục, không phải per-line skip).
5. Nếu `created_job_ids` rỗng (toàn batch bị skip) → exit 1 (Requirement
   15.7 — lỗi cấu hình, không có job nào chạy).
6. Start `CliSseConsumer` với `job_ids=set(created_job_ids)` — filter event
   theo tập job vừa submit, tránh nhiễu output từ job của Backend_Service
   chạy song song (Requirement 15.5, 15.10).
7. Setup SIGINT handler → set flag; khi trigger → gọi `job_manager.stop`
   cho TẤT CẢ job (Requirement 15.7 — propagate cancel trước khi exit).
8. Poll loop: mỗi 200 ms check status của mọi job — dùng poll thay vì
   subscribe SSE thứ hai để giữ control flow đơn giản, tách biệt kênh
   log-realtime (consumer) khỏi kênh check-terminal (poll).
9. Khi tất cả job đạt terminal state (hoặc SIGINT) → dừng consumer.
10. In bảng tổng kết qua `formatter.format_batch_summary`.
11. Exit code (Requirement 15.7):
    - `0` — TẤT CẢ job `qr_ready`.
    - `1` — file input lỗi/thiếu dòng hợp lệ/toàn batch bị skip.
    - `2` — ≥1 job kết thúc `error`/`stopped`.
    - `130` — SIGINT.

Sensitive_Data_Redaction (Requirement 15.8): danh sách skip in ra stderr
đi qua `redact_message` với `known_secrets=lines` để mask toàn bộ dòng thô
(có thể chứa `password`/`totp_secret`) — user chỉ thấy reason, không lộ
credential.

_Requirements: 15.2, 15.5, 15.7, 15.8_
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

from app.cli.bootstrap import bootstrap_cli
from app.cli.formatters import EventFormatter, JsonFormatter, TextFormatter
from app.cli.sse_consumer import CliSseConsumer
from app.core.payment_flow import JobStatus
from app.core.redaction import redact_message


__all__ = ["cmd_run_batch", "register_parser"]


# Terminal state — trùng semantic với JobManager: 1 job đạt trạng thái này
# thì `_run_handler` đã release lease/slot, không còn transition tiếp.
_TERMINAL_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.QR_READY, JobStatus.ERROR, JobStatus.STOPPED}
)

# Poll interval — 200 ms là compromise giữa (a) không giật CPU khi có nhiều
# job đang chạy, (b) phản ứng đủ nhanh với SIGINT để trải nghiệm user
# không thấy "hang" sau khi Ctrl+C.
_POLL_INTERVAL_SECONDS: float = 0.2

# Grace period sau khi SIGINT được xử lý: đợi thêm 2 * poll để SSE cuối
# cùng của các job (broadcast STOPPED) kịp tới consumer trước khi stop().
_SIGINT_DRAIN_SECONDS: float = _POLL_INTERVAL_SECONDS * 2

# Exit codes — Requirement 15.7.
_EXIT_OK = 0
_EXIT_CONFIG_ERROR = 1
_EXIT_JOB_ERROR = 2
_EXIT_SIGINT = 130

_PAYMENT_METHOD = "ideal"

# Ký tự comment trong file account — dòng bắt đầu bằng `#` (sau strip) bị
# bỏ qua như bash/toml, tiện thêm ghi chú vào file batch mà không cần xoá.
_COMMENT_PREFIX = "#"


def _load_account_lines(file_path: Path) -> list[str]:
    """Đọc file UTF-8 → list dòng account đã trim, bỏ dòng rỗng + comment.

    Fail_Fast: encoding UTF-8 sai → raise `UnicodeDecodeError` propagate ra
    ngoài (caller catch để exit 1). KHÔNG dùng `errors="ignore"` — sẽ che
    lỗi encoding thật.
    """
    lines: list[str] = []
    for raw in file_path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith(_COMMENT_PREFIX):
            continue
        lines.append(stripped)
    return lines


def _pick_formatter(fmt_name: str) -> EventFormatter:
    """Chọn formatter theo flag `--format` (Requirement 15.6)."""
    if fmt_name == "json":
        return JsonFormatter()
    return TextFormatter()


def _build_summary_records(
    job_ids_ordered: list[str],
    job_manager,
) -> tuple[list[dict], bool]:
    """Tổng hợp record cho từng job theo đúng thứ tự submit, trả kèm cờ
    `all_ok` (True chỉ khi mọi job đều `qr_ready`).

    Case bất thường (record không tồn tại trong `_jobs` — về lý thuyết
    không xảy ra vì submit_batch đã tạo record và không có API xoá) được
    xử lý tường minh với status marker `"missing"` để formatter không crash
    mà không cần fallback im lặng.
    """
    records: list[dict] = []
    all_ok = True
    for job_id in job_ids_ordered:
        rec = job_manager.get_job(job_id)
        if rec is None:
            all_ok = False
            records.append(
                {
                    "job_id": job_id,
                    "status": "missing",
                    "artifact_path": None,
                    "error_code": "job_disappeared",
                    "error_message": None,
                }
            )
            continue
        status = rec.status
        # `JobStatus` là `str, Enum` nên `.value` = string form (`"qr_ready"`).
        status_str = status.value if isinstance(status, JobStatus) else str(status)
        records.append(
            {
                "job_id": job_id,
                "status": status_str,
                "artifact_path": rec.artifact_path,
                "error_code": rec.error_code,
                "error_message": rec.error_message,
            }
        )
        if status != JobStatus.QR_READY:
            all_ok = False
    return records, all_ok


async def cmd_run_batch(args: argparse.Namespace) -> int:
    """Handler cho sub-command `run-batch <file_path>`.

    Args:
        args: `argparse.Namespace` từ `register_parser` — chứa `file_path`,
            `format`, và `db_path` (do parent parser inject qua flag global
            `--db-path`).

    Returns:
        Exit code theo Requirement 15.7.
    """
    file_path = Path(args.file_path)
    fmt_name: str = args.format
    db_path_override: str | None = args.db_path

    if not file_path.is_file():
        print(f"File không tồn tại: {file_path}", file=sys.stderr, flush=True)
        return _EXIT_CONFIG_ERROR

    try:
        lines = _load_account_lines(file_path)
    except UnicodeDecodeError as ex:
        print(
            f"File {file_path} không phải UTF-8 hợp lệ: {ex}",
            file=sys.stderr,
            flush=True,
        )
        return _EXIT_CONFIG_ERROR
    except OSError as ex:
        print(
            f"Không đọc được file {file_path}: {ex}",
            file=sys.stderr,
            flush=True,
        )
        return _EXIT_CONFIG_ERROR

    if not lines:
        print(
            f"File {file_path} không có dòng account hợp lệ nào",
            file=sys.stderr,
            flush=True,
        )
        return _EXIT_CONFIG_ERROR

    formatter: EventFormatter = _pick_formatter(fmt_name)

    services = await bootstrap_cli(db_path_override)

    # Setup SIGINT handler + consumer nằm SAU bootstrap để lifecycle của
    # cả 2 gói gọn trong try/finally cùng `services` — đảm bảo cleanup
    # thứ tự ngược lại lúc setup (consumer.stop → remove_signal_handler →
    # db_engine.close) dù có exception ở bất kỳ đoạn nào bên trong.
    consumer = CliSseConsumer(services.sse, formatter)
    sigint_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    sigint_handler_installed = False

    def _on_sigint() -> None:
        # Chỉ set flag — cancel/stop job thực hiện trong poll loop dưới
        # `_lock` của JobManager để tránh race với scheduler đang start
        # job mới cùng thời điểm.
        sigint_event.set()

    try:
        loop.add_signal_handler(signal.SIGINT, _on_sigint)
        sigint_handler_installed = True
    except NotImplementedError:
        # Windows / non-main-loop → skip. SIGINT sẽ được Python default
        # xử lý (raise KeyboardInterrupt), asyncio.run() propagate ra
        # ngoài — vẫn đảm bảo cleanup qua finally.
        pass

    try:
        result = await services.job_manager.submit_batch(_PAYMENT_METHOD, lines)

        # In danh sách skip ra stderr — mask toàn bộ line thô để không lộ
        # password/totp (Requirement 15.8). `known_secrets=lines` khiến
        # `redact_message` replace toàn bộ occurrence của mỗi line trong
        # `item["line"]` → user chỉ thấy `***REDACTED*** :: reason`, đủ
        # để debug (biết có N dòng bị skip vì reason nào) mà không lộ giá
        # trị nhạy cảm.
        for item in result.skipped:
            safe_line = redact_message(item["line"], lines)
            print(
                f"skipped: {safe_line} :: {item['reason']}",
                file=sys.stderr,
                flush=True,
            )

        if not result.created_job_ids:
            print(
                "Không tạo được job nào từ file input",
                file=sys.stderr,
                flush=True,
            )
            return _EXIT_CONFIG_ERROR

        job_ids_ordered: list[str] = list(result.created_job_ids)
        job_ids_set = set(job_ids_ordered)

        await consumer.start(job_ids_set)

        # ---- Poll loop chờ terminal state ----
        while True:
            if sigint_event.is_set():
                # Propagate cancel tới TẤT CẢ job (Requirement 15.7).
                # `return_exceptions=True` để 1 stop() lỗi không kill các
                # stop() còn lại — vẫn phải cố gắng dừng hết.
                await asyncio.gather(
                    *(services.job_manager.stop(jid) for jid in job_ids_ordered),
                    return_exceptions=True,
                )
                # Grace: chờ SSE STOPPED cuối cùng tới consumer + handler
                # thực sự thoát (release lease/slot trong finally).
                await asyncio.sleep(_SIGINT_DRAIN_SECONDS)
                break

            all_terminal = True
            for jid in job_ids_ordered:
                rec = services.job_manager.get_job(jid)
                if rec is None or rec.status not in _TERMINAL_STATUSES:
                    all_terminal = False
                    break

            if all_terminal:
                # 1 tick nữa để SSE cuối cùng (STATUS terminal) chắc chắn
                # được consumer in ra trước khi bảng summary chèn vào
                # stdout — giữ thứ tự log hợp lý cho user đọc.
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                break

            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

        # Dừng consumer TRƯỚC khi in summary — tránh interleave giữa dòng
        # SSE trailing và bảng summary trên stdout.
        await consumer.stop()

        records, all_ok = _build_summary_records(
            job_ids_ordered, services.job_manager
        )
        print(formatter.format_batch_summary(records), flush=True)

        if sigint_event.is_set():
            return _EXIT_SIGINT
        return _EXIT_OK if all_ok else _EXIT_JOB_ERROR
    finally:
        # Cleanup theo thứ tự ngược lại lúc setup — consumer.stop() idempotent
        # (theo `CliSseConsumer` docstring) nên gọi lại vẫn an toàn khi
        # đã stop ở happy path.
        await consumer.stop()
        if sigint_handler_installed:
            try:
                loop.remove_signal_handler(signal.SIGINT)
            except (NotImplementedError, ValueError):
                pass
        await services.db_engine.close()


def register_parser(subparsers: argparse._SubParsersAction) -> None:
    """Đăng ký sub-parser `run-batch` vào parser gốc của CLI.

    File input dùng positional để user gõ tự nhiên: `ideal-qr run-batch
    accounts.txt`. Comment prefix `#` được ghi trong help để user không
    cần đọc source biết cú pháp file.
    """
    parser = subparsers.add_parser(
        "run-batch",
        help="Chạy nhiều IdealJob từ file (mỗi dòng 1 account, UTF-8)",
        description=(
            "Đọc file account (UTF-8, mỗi dòng 1 account, dòng bắt đầu "
            "bằng '#' là comment và được bỏ qua), submit toàn bộ vào "
            "JobManager, chờ mọi job đạt terminal state, in bảng tổng "
            "kết. Exit 0 khi tất cả job qr_ready, 2 nếu có ≥1 job "
            "error/stopped, 1 khi lỗi cấu hình, 130 khi Ctrl+C."
        ),
    )
    parser.add_argument(
        "file_path",
        help="Đường dẫn file account (UTF-8, mỗi dòng 1 account, '#' để comment)",
    )
    parser.set_defaults(handler=cmd_run_batch)
