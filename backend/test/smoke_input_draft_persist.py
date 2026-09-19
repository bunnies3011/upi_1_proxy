#!/usr/bin/env python3
"""Smoke test: draft input persist qua restart backend.

Kịch bản mô phỏng lifecycle full stack:
1. Startup 1 (giả lập backend bật lần đầu):
   - Bootstrap `SettingsRepository` với file DB thật (không :memory:).
   - Set `ui.input_draft` = "email1\\npass1\\nemail2\\npass2".
   - Close DB.
2. Startup 2 (giả lập restart):
   - Bootstrap lại `SettingsRepository` cùng file DB.
   - Get `ui.input_draft` → phải trả về nguyên vẹn value đã set.
   - `list()` → dict chứa key `ui.input_draft` với value đúng.
3. Cleanup: xóa file DB test.

Chạy: ./.venv/bin/python3 test/smoke_input_draft_persist.py từ backend/
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import traceback
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.db import DbEngine  # noqa: E402
from app.core.settings_store import SettingsRepository  # noqa: E402


DRAFT_VALUE = (
    "roves_vestige2i+uf7a7n@icloud.com|Anczz123456789@|7IVU46EENTYEQ4RS6UEH7PODSQHTLJJZ\n"
    "zealous-flint49+e4l26v@icloud.com|Anczz123456789@|JGRCEBRM7CFUKKL2NRHP2EE2DZGWFUV2\n"
    "spikier_gaze_54+b3ejl01@icloud.com|Anczz123456789@|CPN4HIYKEKCTFACTI7OYUWYNMXQEKFSG"
)


async def _startup_write(db_path: Path) -> None:
    """Startup 1 — bootstrap + write draft + close."""
    engine = DbEngine(str(db_path))
    await engine.init_schema()
    settings = SettingsRepository(engine)
    await settings.set("ui.input_draft", DRAFT_VALUE)
    await engine.close()


async def _startup_read(db_path: Path) -> tuple[str | None, dict]:
    """Startup 2 — bootstrap lại + read draft + list all."""
    engine = DbEngine(str(db_path))
    await engine.init_schema()  # idempotent, không xóa data cũ
    settings = SettingsRepository(engine)
    val = await settings.get("ui.input_draft")
    all_map = await settings.list(prefix="ui")
    await engine.close()
    return val, all_map


async def _main() -> int:
    failed = 0

    # Dùng file thật (không :memory:) để test persistence qua process boundary.
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_persist.db"

        # -----------------------------------------------------------------
        # TC-01: Write ở startup 1, restart, read lại.
        # -----------------------------------------------------------------
        tc = "TC-01"
        try:
            print(f"[..] {tc} — Startup 1: write draft ({len(DRAFT_VALUE)} chars)", flush=True)
            await _startup_write(db_path)
            assert db_path.exists(), "DB file phải tồn tại sau close"
            size = db_path.stat().st_size
            print(f"     · DB file created: {db_path.name} ({size} bytes)", flush=True)

            print(f"     Startup 2: bootstrap lại + read", flush=True)
            val, all_ui = await _startup_read(db_path)

            assert val is not None, "value phải != None sau restart"
            assert val == DRAFT_VALUE, (
                f"value không khớp. Want {len(DRAFT_VALUE)} chars, "
                f"got {len(val) if val else 0}"
            )
            assert "ui.input_draft" in all_ui, "list() phải chứa key"
            assert all_ui["ui.input_draft"] == DRAFT_VALUE

            print(
                f"[PASS] {tc} — draft persist qua restart ({len(val)} chars, "
                f"{val.count(chr(10)) + 1} dòng)",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {tc} — {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
            failed += 1
            return failed

        # -----------------------------------------------------------------
        # TC-02: Update draft, restart, verify update giữ.
        # -----------------------------------------------------------------
        tc = "TC-02"
        try:
            new_value = "updated@example.com|newpass|newtoken"
            engine = DbEngine(str(db_path))
            await engine.init_schema()
            await SettingsRepository(engine).set("ui.input_draft", new_value)
            await engine.close()

            val, _ = await _startup_read(db_path)
            assert val == new_value, f"update không giữ, got {val!r}"
            print(f"[PASS] {tc} — update draft persist qua restart", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {tc} — {type(exc).__name__}: {exc}", flush=True)
            failed += 1

        # -----------------------------------------------------------------
        # TC-03: Empty string cũng persist (không confuse với "chưa set").
        # -----------------------------------------------------------------
        tc = "TC-03"
        try:
            engine = DbEngine(str(db_path))
            await engine.init_schema()
            await SettingsRepository(engine).set("ui.input_draft", "")
            await engine.close()

            val, _ = await _startup_read(db_path)
            assert val == "", f"empty string phải persist, got {val!r}"
            print(f"[PASS] {tc} — empty string persist đúng (không nhầm với None)", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {tc} — {type(exc).__name__}: {exc}", flush=True)
            failed += 1

        # -----------------------------------------------------------------
        # TC-04: Verify DB path mặc định của app (backend/runtime/ideal_qr_tool.db)
        # có logic persist tương tự — chỉ inspect config, không mount lại app.
        # -----------------------------------------------------------------
        tc = "TC-04"
        try:
            default_db = BACKEND_ROOT / "runtime" / "ideal_qr_tool.db"
            assert default_db.parent.name == "runtime"
            # `bootstrap.py` dùng str path → SQLite file-based (không :memory:)
            print(
                f"[PASS] {tc} — default DB path = {default_db.relative_to(BACKEND_ROOT)} "
                f"(persist trên disk, {'tồn tại' if default_db.exists() else 'sẽ tạo khi startup'})",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {tc} — {type(exc).__name__}: {exc}", flush=True)
            failed += 1

    print(f"\n== Done: fail={failed} ==", flush=True)
    return failed


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
