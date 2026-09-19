"""Sub-command `ideal-qr session-cache clear` — xoá AccountSessionCache.

Requirement 15.2 (CLI command spec), 10.8 (clear idempotent per-account),
10.9 (clear_all best-effort toàn bộ).

Tham số MUTUALLY EXCLUSIVE (required):

- `<account_key>` (positional): xoá cache của 1 account cụ thể (`account_key`
  là hash sha256(email_normalized), Requirement 10.4).
- `--all`: xoá toàn bộ cache của mọi account.

Argparse enforcement (`add_mutually_exclusive_group(required=True)`):

- Truyền cả 2 → argparse raise `SystemExit` với message "not allowed with".
- Không truyền cái nào → argparse raise `SystemExit` với message "one of the
  arguments ... is required".

Fail_Fast: KHÔNG tự implement branch "không có tham số nào" — argparse đã
enforce ở tầng parse, không cần double-check.

Runtime lifecycle:

- Gọi `bootstrap_cli(db_path_override)` để có `BootstrappedServices` singleton
  (Requirement 15.1, 15.3). CLI KHÔNG bind socket, nên `db_path_override`
  từ `--db-path` là điểm duy nhất phải resolve.
- Sau khi thao tác xong PHẢI `await services.db_engine.close()` trong
  `finally` để giải phóng shared connection — nếu skip, process CLI có thể
  hang khi asyncio loop tear-down (Requirement 11.6 idempotent close).

Output convention:

- STDOUT: 1 dòng xác nhận kết quả (`session-cache cleared: <key|all>`) để
  script wrap CLI có thể grep. `flush=True` để không buffer khi pipe.
- Exit code 0 khi thành công, non-zero KHÔNG được emit ở đây (argparse tự
  xử case validation fail; các exception hạ tầng cứ để propagate — chính
  sách Fail_Fast của CLI).

_Requirements: 15.2, 10.8, 10.9_
"""

from __future__ import annotations

import argparse

from app.cli.bootstrap import bootstrap_cli


__all__ = ["cmd_session_cache_clear", "register_parser"]


# Exit code duy nhất trả về từ handler — argparse handle validation errors,
# lỗi runtime để exception propagate lên `cli/main.py` (Fail_Fast).
_EXIT_OK = 0


async def cmd_session_cache_clear(args: argparse.Namespace) -> int:
    """Handler cho `ideal-qr session-cache clear [<account_key>] [--all]`.

    Contract:

    - Đọc `args.db_path` (từ parent parser `--db-path`, optional),
      `args.account_key` (positional, có thể `None`), `args.clear_all`
      (bool từ `--all`).
    - Bootstrap toàn bộ singleton core qua `bootstrap_cli` để tái sử dụng
      100% wiring của Backend_Service (Requirement 15.3), nhưng CHỈ dùng
      `session_cache` service để clear.
    - Nhánh `--all` ưu tiên hơn positional trong logic ở đây — nhưng thực
      tế argparse đã enforce mutex, nên chỉ 1 trong 2 sẽ có value tại
      runtime. Kiểm tra `clear_all_flag` trước là style choice (dễ đọc).
    - `await` bắt buộc trên `clear`/`clear_all` vì chúng là async I/O
      (unlink file JSON dưới `session_cache_dir`) — sync call sẽ trả về
      coroutine chưa await → không có tác dụng + RuntimeWarning.

    Returns:
        `_EXIT_OK` (0) — mọi lỗi hạ tầng đều propagate exception, không
        map về exit code ở tầng này (Fail_Fast).
    """
    # Parent parser sử dụng `dest="db_path"` cho `--db-path`; nếu vắng flag,
    # argparse set về `None` → `bootstrap_cli` fallback env/default.
    db_path_override: str | None = getattr(args, "db_path", None)
    account_key: str | None = args.account_key
    clear_all_flag: bool = args.clear_all

    services = await bootstrap_cli(db_path_override)
    try:
        if clear_all_flag:
            await services.session_cache.clear_all()
            print("session-cache cleared: all", flush=True)
        else:
            # Argparse mutex + required=True đảm bảo tới đây `account_key`
            # LUÔN có value (non-None, non-empty) — không cần defensive
            # branch. Nếu vẫn `None`, đó là argparse regression → Fail_Fast
            # bằng cách để `clear` raise (nó accept str, None sẽ TypeError
            # khi hash path).
            assert account_key is not None, (
                "argparse mutex regression: account_key None mà --all False"
            )
            await services.session_cache.clear(account_key)
            print(f"session-cache cleared: {account_key}", flush=True)
        return _EXIT_OK
    finally:
        # Idempotent close (Requirement 11.6) — luôn chạy kể cả khi
        # `clear`/`clear_all` raise, để không leak SQLite connection.
        await services.db_engine.close()


def register_parser(subparsers: argparse._SubParsersAction) -> None:
    """Đăng ký cây sub-command `session-cache` vào top-level parser.

    Cấu trúc:

        ideal-qr session-cache clear <account_key>
        ideal-qr session-cache clear --all

    Design decision — `session-cache` là "namespace" parser cha, `clear`
    là action con (chỉ 1 action tại thời điểm này; thêm `list` / `inspect`
    trong tương lai sẽ append thêm sub-sub-parser). Cấu trúc lồng cho phép
    mở rộng mà không break CLI surface.

    Argparse quirk khi kết hợp positional `nargs="?"` + mutex group
    `required=True`:

    - `<account_key>` alone → group thoả (positional present, --all False).
    - `--all` alone → group thoả (positional None-default, --all True).
    - Cả 2 → argparse raise "argument --all: not allowed with argument
      account_key".
    - Không có gì → argparse raise "one of the arguments account_key --all
      is required".

    Behavior này ổn định từ Python 3.9+ (issue bpo-9351 fix). Repo yêu cầu
    Python 3.11+ → an toàn.
    """
    ns_parser = subparsers.add_parser(
        "session-cache",
        help="Thao tác AccountSessionCache (Requirement 10.x)",
    )
    ns_sub = ns_parser.add_subparsers(
        dest="session_cache_action",
        required=True,
        metavar="<action>",
    )

    clear_parser = ns_sub.add_parser(
        "clear",
        help="Xoá cache của 1 account (positional) hoặc toàn bộ (--all)",
    )
    mutex = clear_parser.add_mutually_exclusive_group(required=True)
    mutex.add_argument(
        "account_key",
        nargs="?",
        default=None,
        help="account_key cần xoá (sha256(email_normalized), Requirement 10.4)",
    )
    mutex.add_argument(
        "--all",
        dest="clear_all",
        action="store_true",
        help="Xoá toàn bộ cache của mọi account (Requirement 10.9)",
    )
    clear_parser.set_defaults(handler=cmd_session_cache_clear)
