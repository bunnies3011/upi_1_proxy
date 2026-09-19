"""CLI orchestrator — parse argv, dispatch sub-command (Requirement 15.1, 15.2, 15.7).

Root argparse:
- Global flags: ``--format {text,json}`` (default ``text``),
  ``--db-path <path>`` (optional).
- Sub-parsers: ``run``, ``run-batch``, ``settings``, ``session-cache`` — mỗi
  cái do module ``app.cli.commands.*`` tự đăng ký qua
  ``register_parser(subparsers)``. Handler async được gắn qua
  ``parser.set_defaults(handler=...)`` bên trong từng module.

Dispatch: ``await args.handler(args)`` — mỗi handler trả về ``int`` exit
code theo hợp đồng chung.

Exit code (Requirement 15.7):

- ``0``  = success
- ``1``  = config / validation error (do handler tự trả về)
- ``2``  = job runtime error / job stopped (do handler tự trả về)
- ``130`` = SIGINT (``KeyboardInterrupt`` bubble từ Windows path hoặc
  handler chưa nuốt)
- ``3``  = unexpected exception (Fail_Fast — Requirement 14.3: in
  traceback ra stderr, KHÔNG catch-all silent)

Note: Với Unix, các sub-command tự cài SIGINT handler qua
``loop.add_signal_handler`` (task 41.1 / 41.2) và convert Ctrl+C thành
cooperative stop. Trên Windows, ``add_signal_handler`` không hỗ trợ nên
Ctrl+C sẽ raise ``KeyboardInterrupt`` bubble qua đây — main() bắt và trả
exit code 130 (task 42.2).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import traceback

# ---------------------------------------------------------------------------
# Windows-only: force `WindowsSelectorEventLoopPolicy` TRƯỚC khi bất kỳ
# event loop nào được tạo (asyncio.run bên dưới sẽ tạo loop).
#
# Xem docstring block tương ứng trong `app/main.py` cho lý do đầy đủ. Tóm
# tắt: `curl_cffi.AsyncSession` cần `loop.add_reader`/`add_writer` — 2
# method này không tồn tại trên `WindowsProactorEventLoopPolicy` (default
# Python 3.8+). Fallback bridge của `curl_cffi` hay drop event khi có
# nhiều request async concurrent (batch job), dẫn tới timeout 30s hit ở
# step `checkout` / `stripe_init` / `pay_ideal_get_page`. macOS/Linux dùng
# `SelectorEventLoop` (kqueue/epoll) native nên không dính.
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.cli.commands import run as run_cmd
from app.cli.commands import run_batch as run_batch_cmd
from app.cli.commands import session_cache_cmd
from app.cli.commands import settings_cmd


EXIT_UNEXPECTED = 3
EXIT_SIGINT = 130


def _build_parser() -> argparse.ArgumentParser:
    """Build root argparse parser + gắn 4 sub-command groups.

    Returns:
        Parser đã đăng ký đủ ``run``, ``run-batch``, ``settings``,
        ``session-cache`` — mỗi sub-command đã tự ``set_defaults(handler=...)``
        trong module tương ứng.
    """
    parser = argparse.ArgumentParser(
        prog="ideal-qr",
        description="CLI debug/test tool cho iDEAL QR Tool (Requirement 15).",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format cho log/summary (default: text).",
    )
    parser.add_argument(
        "--db-path",
        dest="db_path",
        default=None,
        help=(
            "Override SQLite DB path. Nếu bỏ trống → dùng env "
            "IDEAL_QR_TOOL_DB_PATH hoặc default backend/runtime/ideal_qr_tool.db."
        ),
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="<command>",
    )

    # Từng module command tự đăng ký sub-parser + set_defaults(handler=async_fn).
    run_cmd.register_parser(subparsers)
    run_batch_cmd.register_parser(subparsers)
    settings_cmd.register_parser(subparsers)
    session_cache_cmd.register_parser(subparsers)

    return parser


async def main(argv: list[str] | None = None) -> int:
    """Async orchestrator CLI.

    Args:
        argv: Danh sách argument (KHÔNG bao gồm ``sys.argv[0]``). ``None``
            → dùng ``sys.argv[1:]`` (giữ signature cho test/embed).

    Returns:
        Exit code theo Requirement 15.7.
    """
    if argv is None:
        argv = sys.argv[1:]

    parser = _build_parser()
    # argparse.parse_args tự raise SystemExit(2) khi validation fail — để
    # bubble lên sync_entry, không nuốt ở đây (Requirement 14.3 Fail_Fast).
    args = parser.parse_args(argv)

    handler = getattr(args, "handler", None)
    if handler is None:
        # argparse có ``required=True`` nên tới đây phải có sub-command;
        # đường này chỉ đi vào nếu có regression trong register_parser
        # (thiếu ``set_defaults(handler=...)``). Fail_Fast — in help ra
        # stderr + exit 3 để dev thấy ngay.
        parser.print_help(sys.stderr)
        return EXIT_UNEXPECTED

    try:
        return await handler(args)
    except KeyboardInterrupt:
        # Task 42.2: trên Windows ``loop.add_signal_handler`` không hỗ trợ
        # nên Ctrl+C sẽ raise KeyboardInterrupt bubble qua asyncio task —
        # bắt ở đây, in cảnh báo ra stderr, trả exit code 130.
        print("\nInterrupted by user (SIGINT)", file=sys.stderr, flush=True)
        return EXIT_SIGINT
    except Exception:
        # Fail_Fast (Requirement 14.3): unexpected exception → in traceback
        # đầy đủ ra stderr + exit 3. KHÔNG swallow silent.
        traceback.print_exc()
        return EXIT_UNEXPECTED


def sync_entry() -> None:
    """Sync wrapper cho ``[project.scripts] ideal-qr = ...`` (Requirement 15.1).

    ``pyproject.toml`` entry point chỉ nhận sync callable, nên phải wrap
    ``asyncio.run(main(...))`` ở đây. Đồng thời giữ backup catch
    ``KeyboardInterrupt`` phòng khi ngoại lệ escape khỏi ``main()`` (VD
    ``asyncio.run`` bị interrupt lúc chưa vào coroutine).
    """
    try:
        exit_code = asyncio.run(main(sys.argv[1:]))
    except KeyboardInterrupt:
        exit_code = EXIT_SIGINT
    sys.exit(exit_code)


if __name__ == "__main__":
    sync_entry()
