"""Probe script — chạy trong subprocess sạch để đo import graph của CLI.

File này KHÔNG phải test (leading underscore → pytest bỏ qua khi collect).
Được `test_cli_no_http_import.py` gọi qua `subprocess.run([sys.executable,
str(probe_path)])` để isolate `sys.modules` khỏi pytest worker (nơi có thể
đã import `fastapi.applications` từ integration test khác).

Contract:
- Exit code 0 + stdout ``CLEAN`` → CLI import graph không leak FastAPI
  runtime (Requirement 15.9 đạt).
- Exit code 1 + stdout ``LEAKED:<module1>,<module2>,...`` → có module
  FastAPI/uvicorn/starlette runtime bị nạp khi import CLI → vi phạm 15.9.
- Exit code khác → probe tự lỗi (import error, syntax error, ...) →
  test caller phải fail và show stderr để dev debug.

Danh sách module bị cấm được đồng bộ với `test_cli_no_http_import.py`
qua argv (test truyền tên module cần check qua sys.argv để tránh drift
giữa 2 file).
"""

from __future__ import annotations

import sys


def main() -> int:
    """Import CLI + build parser, sau đó check sys.modules.

    Argv: mỗi element là 1 tên module (dotted path) mà CLI KHÔNG được nạp.
    """
    forbidden_modules = sys.argv[1:]
    if not forbidden_modules:
        print("PROBE_ERROR: no forbidden modules passed via argv", file=sys.stderr)
        return 2

    # Snapshot trước import để phân biệt module do CLI nạp và module có
    # sẵn trong Python interpreter (không bao giờ chứa fastapi/uvicorn ở
    # subprocess sạch, nhưng defensive vẫn tốt).
    pre_import_modules = set(sys.modules.keys())

    # Import CLI theo đúng cách entry point làm: parse args + dispatch.
    # KHÔNG chạy `main()` async — chỉ cần `_build_parser()` để đảm bảo
    # toàn bộ command modules (run, run-batch, settings, session-cache)
    # cũng đã được import → nếu 1 command nào vô ý import fastapi runtime
    # thì leak sẽ bị bắt ở đây.
    import app.cli.main as cli_main

    parser = cli_main._build_parser()
    # Assert parser build được (nếu register_parser fail sẽ raise trước).
    assert parser is not None

    leaked = sorted(m for m in forbidden_modules if m in sys.modules)

    if leaked:
        # In để test caller assert + hiển thị ra dev khi fail.
        # Cũng in thêm module mới được nạp bởi CLI để dễ điều tra
        # (giới hạn 30 dòng để không spam).
        newly_loaded = sorted(set(sys.modules.keys()) - pre_import_modules)
        print("LEAKED:" + ",".join(leaked))
        print("--- newly loaded modules (max 30) ---", file=sys.stderr)
        for name in newly_loaded[:30]:
            print(name, file=sys.stderr)
        return 1

    print("CLEAN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
