"""Setup Settings tối thiểu cho e2e_test.db trước khi chạy CLI với account thật.

Ghi:
- ideal.default_issuer = "RABONL2U" (Rabobank — phổ biến nhất NL)
- ideal.device_profiles = [1 profile NL desktop MacBook chuẩn]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.bootstrap import bootstrap_services  # noqa: E402


DEVICE_PROFILE_NL = {
    "language": "nl-NL",
    "timeZone": "Europe/Amsterdam",
    "screenWidth": 1920,
    "screenHeight": 1080,
    "screenAvailableWidth": 1920,
    "screenAvailableHeight": 1055,
    "colorDepth": 24,
}


async def main() -> int:
    services = await bootstrap_services(
        db_path=ROOT / "runtime" / "e2e_test.db",
        bind_host="127.0.0.1",
        session_cache_dir=ROOT / "runtime" / "session_cache",
        qr_output_dir=ROOT / "runtime" / "qr",
        validate_startup=False,
    )
    try:
        print("[setup] Ghi ideal.default_issuer=RABONL2U", flush=True)
        await services.settings.set("ideal.default_issuer", "RABONL2U")

        print("[setup] Ghi ideal.device_profiles = [1 NL profile]", flush=True)
        await services.settings.set(
            "ideal.device_profiles", [DEVICE_PROFILE_NL]
        )

        # Verify
        di = await services.settings.get("ideal.default_issuer")
        dp = await services.settings.get("ideal.device_profiles")
        print(f"[verify] ideal.default_issuer = {di!r}", flush=True)
        print(f"[verify] ideal.device_profiles = {len(dp)} items", flush=True)
    finally:
        await services.db_engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
