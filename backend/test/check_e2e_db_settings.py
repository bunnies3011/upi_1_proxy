"""Kiểm tra DB e2e_test.db hiện trạng: có bao nhiêu key Settings được set?

Chỉ đọc, không ghi. In danh sách key + type trước để biết setup gì cần bổ sung.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Add backend/ to sys.path
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
        all_values = await services.settings.list()
        print(f"[settings] Tổng số key: {len(all_values)}", flush=True)
        for key in sorted(all_values.keys()):
            value = all_values[key]
            if isinstance(value, list):
                summary = f"list({len(value)} items)"
            elif isinstance(value, str):
                summary = f'"{value[:80]}"' if len(value) <= 80 else f'"{value[:80]}..."'
            else:
                summary = repr(value)
            print(f"  {key} = {summary}", flush=True)

        # Đặc biệt in default_issuer + device_profiles
        di = await services.settings.get("ideal.default_issuer")
        dp = await services.settings.get("ideal.device_profiles")
        print(f"[key] ideal.default_issuer = {di!r}", flush=True)
        print(
            f"[key] ideal.device_profiles = {'[]' if not dp else f'list ({len(dp)} items)'}",
            flush=True,
        )
    finally:
        await services.db_engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
