"""Seed Settings tối thiểu cho DB mặc định trước khi bật Backend_Service.

Khác `setup_e2e_settings.py` (hardcode `runtime/e2e_test.db`), script này
đọc DB path từ env `IDEAL_QR_TOOL_DB_PATH` — khớp CHÍNH XÁC với logic
`app/main.py._resolve_db_path()` để `setup.sh` / `setup.bat` seed đúng
file DB mà uvicorn sẽ mở ngay sau đó.

Behavior:
- Nếu `ideal.default_issuer` đã có giá trị hợp lệ → SKIP toàn bộ (idempotent,
  chạy lại `setup.sh` không phá state user đã tinh chỉnh qua UI).
- Nếu chưa có → ghi seed tối thiểu: 1 issuer NL phổ biến + 1 device profile
  NL desktop. Mọi setting khác (proxy list, concurrency, TTL…) user tự
  chỉnh qua UI hoặc script riêng.

Design note (Fail_Fast, SRP):
    Script này KHÔNG gọi `bootstrap_services()`. Full bootstrap sẽ:
        - `JobManager.load_from_db()` → nếu DB có pending job, spawn
          `_scheduler_loop` + `_cleanup_loop` + dispatch `_run_handler`
          → seed sẽ vô tình khởi động job (chạy song song với uvicorn
          sắp bind ở dòng lệnh kế tiếp trong `setup.sh` → race đôi khi
          gây corrupt state).
        - Spawn `probe_pool_batch` preflight orphan task với timeout 30s
          — task này KHÔNG được track qua `BootstrappedServices` nên
          caller không có cách cancel; `asyncio.run()` phải chờ mọi task
          orphan xong hoặc respect cancel → có thể treo 30s+ sau khi
          seed logic đã hoàn tất (đây là root cause `setup.sh` bị stuck
          sau `[7/7]`).
    Seed chỉ cần đọc/ghi Settings → minimal init: DbEngine → init_schema →
    SettingsRepository → register_ideal_namespace. Không background task
    nào → `asyncio.run` thoát ngay khi `main()` return.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


def _resolve_backend_root() -> Path:
    """Tìm thư mục `backend/` dựa trên marker `pyproject.toml`.

    Script này chạy được ở 2 layout:
    - Dev: `backend/test/setup_default_settings.py` → `parents[1]` = `backend/`.
    - Release: `backend/seed_settings.py` (build.sh copy) → `parents[0]` = `backend/`.

    Thay vì hardcode index parents, walk up từ `__file__` cho tới khi gặp
    `pyproject.toml` — cách này miễn nhiễm layout, không sợ copy sai chỗ.
    """
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").exists() and (candidate / "app").is_dir():
            return candidate
    raise RuntimeError(
        f"Không tìm thấy backend/ (pyproject.toml + app/) từ {here}"
    )


ROOT = _resolve_backend_root()
sys.path.insert(0, str(ROOT))

from app.core.db import DbEngine  # noqa: E402
from app.core.settings_store import SettingsRepository  # noqa: E402
from app.payments.ideal import register_ideal_namespace  # noqa: E402


_ENV_DB_PATH = "IDEAL_QR_TOOL_DB_PATH"
_DEFAULT_ISSUER = "RABONL2U"

_DEVICE_PROFILE_NL = {
    "language": "nl-NL",
    "timeZone": "Europe/Amsterdam",
    "screenWidth": 1920,
    "screenHeight": 1080,
    "screenAvailableWidth": 1920,
    "screenAvailableHeight": 1055,
    "colorDepth": 24,
}


def _resolve_db_path() -> Path:
    """Sao chép chính xác logic `app.main._resolve_db_path` (anchor `backend/`)."""
    raw = os.environ.get(_ENV_DB_PATH, "").strip()
    if not raw:
        return ROOT / "runtime" / "ideal_qr_tool.db"
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    return ROOT / candidate


async def main() -> int:
    db_path = _resolve_db_path()
    print(f"[seed] db_path = {db_path}", flush=True)

    # Ensure thư mục chứa DB tồn tại — bootstrap_services trước đây làm
    # bước này; giờ tự làm vì không dùng bootstrap.
    db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = DbEngine(db_path)
    settings = SettingsRepository(engine)
    await engine.init_schema()
    # Đăng ký namespace `ideal.*` để 2 key `ideal.default_issuer` +
    # `ideal.device_profiles` pass type constraint khi get/set. Namespace
    # `telegram.*`, `proxy.*`, `session_cache.*` KHÔNG cần cho seed.
    await register_ideal_namespace(settings)

    try:
        existing_issuer = await settings.get("ideal.default_issuer")
        existing_profiles = await settings.get("ideal.device_profiles")
        if existing_issuer and existing_profiles:
            print(
                f"[seed] SKIP — DB đã có ideal.default_issuer={existing_issuer!r}, "
                f"device_profiles={len(existing_profiles)} item(s). "
                "Chỉnh thêm qua UI (nút Cài đặt) nếu cần.",
                flush=True,
            )
            return 0

        if not existing_issuer:
            print(f"[seed] Ghi ideal.default_issuer={_DEFAULT_ISSUER}", flush=True)
            await settings.set("ideal.default_issuer", _DEFAULT_ISSUER)
        if not existing_profiles:
            print("[seed] Ghi ideal.device_profiles = [1 NL desktop profile]", flush=True)
            await settings.set("ideal.device_profiles", [_DEVICE_PROFILE_NL])

        di = await settings.get("ideal.default_issuer")
        dp = await settings.get("ideal.device_profiles")
        print(f"[verify] ideal.default_issuer = {di!r}", flush=True)
        print(f"[verify] ideal.device_profiles = {len(dp)} item(s)", flush=True)
    finally:
        await engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
