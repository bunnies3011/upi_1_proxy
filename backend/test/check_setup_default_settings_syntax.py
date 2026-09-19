"""Syntax + import shape check cho setup_default_settings.py.

Chạy: python3 backend/test/check_setup_default_settings_syntax.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE / "setup_default_settings.py"


def main() -> int:
    if not TARGET.exists():
        print(f"[FAIL] not found: {TARGET}", flush=True)
        return 1

    src = TARGET.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(TARGET))
    except SyntaxError as exc:
        print(f"[FAIL] syntax error: {exc}", flush=True)
        return 1
    print("[PASS] ast.parse OK", flush=True)

    # Verify import bootstrap_services + main + _resolve_db_path.
    top_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            top_names.add(node.name)
        elif isinstance(node, ast.AsyncFunctionDef):
            top_names.add(node.name)

    required = {"main", "_resolve_db_path"}
    missing = required - top_names
    if missing:
        print(f"[FAIL] missing top-level defs: {missing}", flush=True)
        return 1
    print(f"[PASS] top-level defs present: {sorted(required)}", flush=True)

    if "from app.bootstrap import bootstrap_services" not in src:
        print("[FAIL] missing bootstrap import", flush=True)
        return 1
    print("[PASS] bootstrap import present", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
