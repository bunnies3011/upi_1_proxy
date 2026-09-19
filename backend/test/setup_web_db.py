"""Setup DB mặc định (`runtime/ideal_qr_tool.db`) cho web mode.

- ideal.default_issuer = RABONL2U (không cần vì đã bỏ bank ở confirm)
- ideal.device_profiles = 1 NL profile (không thực sự cần cho MVP nhưng
  giữ để tương thích spec cũ)
- web.auth_token = "" (loopback → cho phép rỗng)
- ideal.max_concurrent = 3
- ideal.refresh_poll_max_attempts = 2 (chỉ fallback, không dùng chính)
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
        db_path=ROOT / "runtime" / "ideal_qr_tool.db",
        bind_host="127.0.0.1",
        session_cache_dir=ROOT / "runtime" / "session_cache",
        qr_output_dir=ROOT / "runtime" / "qr",
        validate_startup=False,
    )
    try:
        pairs = {
            "ideal.default_issuer": "RABONL2U",
            "ideal.device_profiles": [DEVICE_PROFILE_NL],
            "ideal.max_concurrent": 3,
            "ideal.refresh_poll_max_attempts": 2,
            "ideal.refresh_poll_delay_seconds": 1.0,
        }
        for key, value in pairs.items():
            await services.settings.set(key, value)
            print(f"[setup] {key} = {value}", flush=True)
    finally:
        await services.db_engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
