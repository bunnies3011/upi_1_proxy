"""One-shot patch: bổ sung `stripe_confirm_transport` +
`stripe_follow_redirect_transport` vào setting `ideal.auto_retry_blocked_codes`
hiện có trong DB (không đè, chỉ append).

Chạy: `./.venv/bin/python test/patch_auto_retry_codes.py` từ backend/.

Idempotent — chạy nhiều lần chỉ append 1 lần. Không xử lý transaction phức
tạp (setting ít đổi, script chạy 1 shot).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "runtime" / "ideal_qr_tool.db"
KEY = "ideal.auto_retry_blocked_codes"
NEW_CODES = ("stripe_confirm_transport", "stripe_follow_redirect_transport")


def main() -> int:
    if not DB_PATH.is_file():
        print(f"[SKIP] DB not found: {DB_PATH}", flush=True)
        return 0

    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (KEY,)
        ).fetchone()
        if row is None:
            print(
                f"[SKIP] '{KEY}' chưa được set — default trong "
                f"payments/ideal/__init__.py sẽ bao gồm 2 code mới ở lần "
                f"startup tiếp theo.",
                flush=True,
            )
            return 0

        codes = json.loads(row[0])
        if not isinstance(codes, list):
            print(f"[FAIL] value không phải list: {codes!r}", flush=True)
            return 1

        added: list[str] = []
        for code in NEW_CODES:
            if code not in codes:
                codes.append(code)
                added.append(code)

        if not added:
            print(
                f"[NOOP] Cả 2 code đã có sẵn trong list ({len(codes)} codes)",
                flush=True,
            )
            return 0

        conn.execute(
            """
            UPDATE settings
            SET value = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE key = ?
            """,
            (json.dumps(codes, ensure_ascii=False), KEY),
        )
        conn.commit()
        print(
            f"[OK] Đã thêm {len(added)} code mới: {added}. "
            f"Tổng {len(codes)} codes.",
            flush=True,
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
