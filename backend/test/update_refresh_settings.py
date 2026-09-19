"""Update refresh_poll settings — 10 attempts × 2s = 20s tổng chờ Stripe
async webhook create intent sau approve.
"""

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
        await services.settings.set("ideal.refresh_poll_max_attempts", 10)
        await services.settings.set("ideal.refresh_poll_delay_seconds", 2.0)
        print("[setup] refresh_poll: 10 attempts × 2s = 20s total", flush=True)
    finally:
        await services.db_engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
