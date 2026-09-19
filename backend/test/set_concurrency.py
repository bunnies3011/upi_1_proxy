"""Set ideal.max_concurrent = 3 để chạy song song 3 job."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.bootstrap import bootstrap_services  # noqa: E402


async def main() -> int:
    services = await bootstrap_services(
        db_path=ROOT / "runtime" / "e2e_test.db",
        bind_host="127.0.0.1",
        session_cache_dir=ROOT / "runtime" / "session_cache",
        qr_output_dir=ROOT / "runtime" / "qr",
        validate_startup=False,
    )
    try:
        await services.settings.set("ideal.max_concurrent", 3)
        print("[setup] ideal.max_concurrent = 3", flush=True)
    finally:
        await services.db_engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
