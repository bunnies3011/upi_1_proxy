"""Syntax + import order check cho app/bootstrap.py sau fix UnboundLocalError.

Kiểm tra:
  1. File parse được (py_compile).
  2. `import asyncio as _asyncio` xuất hiện TRƯỚC lần dùng `_asyncio.create_task`.
  3. Không còn dòng `import asyncio as _asyncio` trùng lặp trong hàm.
"""
from __future__ import annotations

import py_compile
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "app" / "bootstrap.py"


def main() -> int:
    steps: list[tuple[str, str, bool]] = []

    # [1/3] Parse
    try:
        py_compile.compile(str(TARGET), doraise=True)
        steps.append(("TC-01", "py_compile bootstrap.py", True))
    except py_compile.PyCompileError as exc:
        steps.append(("TC-01", f"py_compile FAIL: {exc}", False))

    text = TARGET.read_text(encoding="utf-8")
    lines = text.splitlines()

    # [2/3] Vị trí import và vị trí dùng
    import_lines = [
        idx for idx, line in enumerate(lines, start=1)
        if line.strip() == "import asyncio as _asyncio"
    ]
    use_lines = [
        idx for idx, line in enumerate(lines, start=1)
        if "_asyncio.create_task(" in line
    ]

    ok_order = (
        len(import_lines) >= 1
        and len(use_lines) >= 1
        and import_lines[0] < use_lines[0]
    )
    steps.append((
        "TC-02",
        f"import@{import_lines} phải trước use@{use_lines}",
        ok_order,
    ))

    # [3/3] Không còn import trùng lặp
    ok_no_dup = len(import_lines) == 1
    steps.append((
        "TC-03",
        f"chỉ có 1 lần `import asyncio as _asyncio` (found={len(import_lines)})",
        ok_no_dup,
    ))

    all_ok = True
    for tc, desc, ok in steps:
        tag = "[PASS]" if ok else "[FAIL]"
        print(f"{tag} {tc} — {desc}", flush=True)
        all_ok = all_ok and ok

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
