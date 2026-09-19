"""Syntax + import check cho chatgpt_login.py + sentinel.py + chatgpt_client.py."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FILES = [
    ROOT / "app" / "payments" / "ideal" / "sentinel.py",
    ROOT / "app" / "payments" / "ideal" / "chatgpt_login.py",
    ROOT / "app" / "payments" / "ideal" / "chatgpt_client.py",
]


def main() -> int:
    all_ok = True

    # 1. Parse AST (syntax check)
    for f in FILES:
        try:
            ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            print(f"[AST-OK] {f.relative_to(ROOT)}", flush=True)
        except SyntaxError as ex:
            print(f"[AST-FAIL] {f.relative_to(ROOT)} -> {ex}", flush=True)
            all_ok = False

    # 2. Import test
    sys.path.insert(0, str(ROOT))
    try:
        from app.payments.ideal import sentinel  # noqa: F401
        print("[IMPORT-OK] app.payments.ideal.sentinel", flush=True)
    except Exception as ex:  # noqa: BLE001
        print(f"[IMPORT-FAIL] sentinel: {ex}", flush=True)
        all_ok = False

    try:
        from app.payments.ideal import chatgpt_login  # noqa: F401
        print("[IMPORT-OK] app.payments.ideal.chatgpt_login", flush=True)
    except Exception as ex:  # noqa: BLE001
        print(f"[IMPORT-FAIL] chatgpt_login: {ex}", flush=True)
        all_ok = False

    try:
        from app.payments.ideal import chatgpt_client  # noqa: F401
        print("[IMPORT-OK] app.payments.ideal.chatgpt_client", flush=True)
    except Exception as ex:  # noqa: BLE001
        print(f"[IMPORT-FAIL] chatgpt_client: {ex}", flush=True)
        all_ok = False

    # 3. Check ChatgptClient still has expected API
    try:
        from app.payments.ideal.chatgpt_client import ChatgptClient
        required = ["login", "revalidate", "create_checkout", "approve"]
        for name in required:
            if not hasattr(ChatgptClient, name):
                print(f"[API-FAIL] ChatgptClient thiếu method {name}", flush=True)
                all_ok = False
            else:
                print(f"[API-OK] ChatgptClient.{name}", flush=True)
    except Exception as ex:  # noqa: BLE001
        print(f"[API-CHECK-FAIL] {ex}", flush=True)
        all_ok = False

    print(f"\n[SUMMARY] {'ALL-OK' if all_ok else 'HAS-ERRORS'}", flush=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
