"""Unit test cho migration Settings namespace `telegram.*` (legacy key).

Requirements: 2.6 (copy giá trị cũ `telegram.enabled`/`telegram.chat_targets`
sang `telegram.push_mode.enabled`/`telegram.push_mode.chat_targets` khi key
mới chưa từng được set), 2.7 (xoá row cũ khỏi bảng `settings` sau khi key
mới đã có giá trị hợp lệ — do copy HOẶC đã set từ trước), 2.8 (KHÔNG ghi đè
giá trị key mới nếu đã có giá trị từ trước, chỉ xoá row cũ).
"""

from __future__ import annotations

import json
from pathlib import Path

from app.core.db import DbEngine
from app.core.settings_store import SettingsRepository
from app.notifiers.telegram import register_telegram_namespace

_LEGACY_CHAT_TARGETS = [{"chat_id": "-1001234567890", "label": "Nhóm ops", "enabled": True}]

_UPSERT_RAW_SQL = """
INSERT INTO settings (key, value, updated_at)
VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
ON CONFLICT(key) DO UPDATE SET
    value = excluded.value,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now');
"""


async def _seed_raw_setting(engine: DbEngine, key: str, value: object) -> None:
    """Seed 1 row vào bảng `settings` bằng raw SQL, giả lập DB cũ trước migration."""
    connection = await engine.get_connection()
    await connection.execute(_UPSERT_RAW_SQL, (key, json.dumps(value)))
    await connection.commit()


async def _fetch_raw_setting_keys(engine: DbEngine, keys: list[str]) -> set[str]:
    """Trả về subset của `keys` hiện còn tồn tại như row trong bảng `settings`."""
    connection = await engine.get_connection()
    placeholders = ",".join("?" for _ in keys)
    async with connection.execute(
        f"SELECT key FROM settings WHERE key IN ({placeholders});", keys
    ) as cursor:
        rows = await cursor.fetchall()
    return {row[0] for row in rows}


async def test_migration_copies_legacy_values_and_deletes_old_rows(
    tmp_path: Path,
) -> None:
    """Key cũ tồn tại, key mới chưa từng set → copy giá trị + xoá row cũ (R2.6, R2.7)."""
    db_path = tmp_path / "ideal_qr_tool.db"
    engine = DbEngine(db_path)
    await engine.init_schema()

    await _seed_raw_setting(engine, "telegram.enabled", True)
    await _seed_raw_setting(engine, "telegram.chat_targets", _LEGACY_CHAT_TARGETS)

    settings = SettingsRepository(engine)
    await register_telegram_namespace(settings, engine)

    assert await settings.get("telegram.push_mode.enabled") is True
    assert await settings.get("telegram.push_mode.chat_targets") == _LEGACY_CHAT_TARGETS

    remaining_legacy_keys = await _fetch_raw_setting_keys(
        engine, ["telegram.enabled", "telegram.chat_targets"]
    )
    assert remaining_legacy_keys == set()

    await engine.close()


async def test_migration_idempotent_when_run_twice(tmp_path: Path) -> None:
    """Chạy migration lần 2 (trên DB đã migrate, qua instance SettingsRepository mới)
    không raise, giá trị không đổi (R2.7 idempotent)."""
    db_path = tmp_path / "ideal_qr_tool.db"
    engine = DbEngine(db_path)
    await engine.init_schema()

    await _seed_raw_setting(engine, "telegram.enabled", True)
    await _seed_raw_setting(engine, "telegram.chat_targets", _LEGACY_CHAT_TARGETS)

    settings_first = SettingsRepository(engine)
    await register_telegram_namespace(settings_first, engine)

    # Instance MỚI vì `register_namespace` raise NamespaceAlreadyRegisteredError
    # nếu gọi 2 lần trên CÙNG 1 instance — DB state (đã migrate) persist qua
    # engine dùng chung, không phụ thuộc instance SettingsRepository nào.
    settings_second = SettingsRepository(engine)
    await register_telegram_namespace(settings_second, engine)

    assert await settings_second.get("telegram.push_mode.enabled") is True
    assert (
        await settings_second.get("telegram.push_mode.chat_targets")
        == _LEGACY_CHAT_TARGETS
    )

    remaining_legacy_keys = await _fetch_raw_setting_keys(
        engine, ["telegram.enabled", "telegram.chat_targets"]
    )
    assert remaining_legacy_keys == set()

    await engine.close()


async def test_migration_does_not_overwrite_existing_new_key_but_deletes_old_row(
    tmp_path: Path,
) -> None:
    """Key mới đã có giá trị từ trước → KHÔNG bị ghi đè bởi giá trị cũ,
    nhưng row cũ vẫn bị xoá (R2.8)."""
    db_path = tmp_path / "ideal_qr_tool.db"
    engine = DbEngine(db_path)
    await engine.init_schema()

    await _seed_raw_setting(engine, "telegram.enabled", True)
    # Seed key mới bằng raw SQL (bypass SettingsRepository.set vì namespace
    # `telegram` chưa được register_namespace tại thời điểm này).
    await _seed_raw_setting(engine, "telegram.push_mode.enabled", False)

    settings = SettingsRepository(engine)
    await register_telegram_namespace(settings, engine)

    # Giá trị mới giữ nguyên `False` (pre-existing), KHÔNG bị copy đè bởi `True` (cũ).
    assert await settings.get("telegram.push_mode.enabled") is False

    remaining_legacy_keys = await _fetch_raw_setting_keys(engine, ["telegram.enabled"])
    assert remaining_legacy_keys == set()

    await engine.close()
