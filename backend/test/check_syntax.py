#!/usr/bin/env python3
"""Kiểm tra syntax + import cho các module đã sửa (không chạy code).

Chạy: python3 test/check_syntax.py từ backend/.
Print [PASS]/[FAIL] từng file để định vị chỗ hỏng nhanh (theo project rule).
"""
from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

# Đảm bảo backend/ ở sys.path để `app.*` import được.
BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

MODULES = [
    "app.core.job_manager",
    "app.core.payment_flow",
    "app.core.settings_store",
    "app.payments.ideal.flow",
    "app.payments.ideal.models",
    "app.payments.ideal",
    "app.api.routes_jobs",
    "app.api.schemas",
]


def main() -> int:
    failed = 0
    for i, mod in enumerate(MODULES, start=1):
        label = f"[{i}/{len(MODULES)}] {mod}"
        try:
            importlib.import_module(mod)
            print(f"[PASS] {label}", flush=True)
        except Exception as exc:  # noqa: BLE001 — smoke import check
            print(f"[FAIL] {label} :: {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
            failed += 1
    print(f"\n== Done: {len(MODULES) - failed}/{len(MODULES)} PASS ==", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
