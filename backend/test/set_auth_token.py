"""Set web.auth_token cho DB mặc định để web mode truy cập API được."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.bootstrap import bootstrap_services  # noqa: E402


TOKEN = "dev-local-token"


async def main() -> int:
    services = await bootstrap_services(
        db_path=ROOT / "runtime" / "ideal_qr_tool.db",
        bind_host="127.0.0.1",
        session_cache_dir=ROOT / "runtime" / "session_cache",
        qr_output_dir=ROOT / "runtime" / "qr",
        validate_startup=False,
    )
    try:
        await services.settings.set("web.auth_token", TOKEN)
        print(f"[setup] web.auth_token = {TOKEN!r}", flush=True)
        print(f"[usage] Frontend: nhập token vào Settings → Auth Token", flush=True)
        print(f"[usage] curl:  -H 'X-Auth-Token: {TOKEN}'", flush=True)
    finally:
        await services.db_engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
