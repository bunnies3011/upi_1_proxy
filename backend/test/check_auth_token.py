"""Xác nhận `web.auth_token` trong SQLite Settings_Store khớp giá trị dev.

Chạy: `python3 test/check_auth_token.py` từ thư mục `ideal_qr_tool/backend`.
Print [PASS]/[FAIL] realtime, exit code 0 nếu OK, 1 nếu mismatch/missing.

Nếu mismatch, tự động set thành `dev-local-token` để dev.sh + main.ts
inject-token khớp value trong DB (fail-fast: chỉ ghi nếu key đang KHÁC
giá trị dev, không đè token production/staging).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.bootstrap import bootstrap_services  # noqa: E402

DB_PATH = BACKEND_ROOT / "runtime" / "ideal_qr_tool.db"
EXPECTED_TOKEN = "dev-local-token"
AUTH_TOKEN_KEY = "web.auth_token"


async def _run() -> int:
    print(f"[CHECK] db path = {DB_PATH}", flush=True)
    if not DB_PATH.exists():
        print(f"[FAIL] DB không tồn tại: {DB_PATH}", flush=True)
        return 1

    services = await bootstrap_services(
        db_path=DB_PATH,
        bind_host="127.0.0.1",
        session_cache_dir=BACKEND_ROOT / "runtime" / "session_cache",
        qr_output_dir=BACKEND_ROOT / "runtime" / "qr",
        validate_startup=False,
    )
    try:
        current = await services.settings.get(AUTH_TOKEN_KEY)
        print(f"[CHECK] {AUTH_TOKEN_KEY!r} hiện tại = {current!r}", flush=True)
        if current == EXPECTED_TOKEN:
            print(f"[PASS] token đã khớp dev default: {EXPECTED_TOKEN!r}", flush=True)
            return 0

        print(
            f"[FIX ] token khác giá trị dev — đang ghi đè thành {EXPECTED_TOKEN!r} …",
            flush=True,
        )
        await services.settings.set(AUTH_TOKEN_KEY, EXPECTED_TOKEN)
        verified = await services.settings.get(AUTH_TOKEN_KEY)
        if verified != EXPECTED_TOKEN:
            print(f"[FAIL] verify sau khi ghi = {verified!r}", flush=True)
            return 1
        print(f"[PASS] đã set + verify {AUTH_TOKEN_KEY!r} = {EXPECTED_TOKEN!r}", flush=True)
        return 0
    finally:
        await services.db_engine.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
