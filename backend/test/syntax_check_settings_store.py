"""Syntax check cho `app/core/settings_store.py` sau khi thêm `get_constraint`.

Task 41.4 (spec ideal-qr-tool): additive change không đụng logic hiện có.
Script chạy stdlib `py_compile` — không cần import runtime dependency
(aiosqlite, DbEngine...), chỉ parse + compile bytecode.
"""

from __future__ import annotations

import py_compile
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "app" / "core" / "settings_store.py"


def main() -> int:
    if not TARGET.is_file():
        print(f"[FAIL] không tìm thấy file target: {TARGET}", flush=True)
        return 1

    try:
        py_compile.compile(str(TARGET), doraise=True)
    except py_compile.PyCompileError as exc:
        print(f"[FAIL] settings_store.py syntax error :: {exc}", flush=True)
        return 1

    print(f"[PASS] settings_store.py :: syntax OK ({TARGET})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
