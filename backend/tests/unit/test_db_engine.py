"""Unit test cho `core/db.py` — `DbEngine`.

Requirements: 11.1, 11.6.
"""

from __future__ import annotations

from pathlib import Path

from app.core.db import DbEngine


async def test_init_schema_creates_settings_table_and_index(tmp_path: Path) -> None:
    """init_schema() phải tạo đúng bảng `settings` (đủ cột) và index `idx_settings_key`."""
    db_path = tmp_path / "runtime" / "ideal_qr_tool.db"
    engine = DbEngine(db_path)

    await engine.init_schema()

    connection = await engine.get_connection()

    async with connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='settings';"
    ) as cursor:
        table_row = await cursor.fetchone()
    assert table_row is not None

    async with connection.execute("PRAGMA table_info(settings);") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    assert columns == {"id", "key", "value", "updated_at"}

    async with connection.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_settings_key';"
    ) as cursor:
        index_row = await cursor.fetchone()
    assert index_row is not None

    await engine.close()


async def test_init_schema_creates_parent_directory(tmp_path: Path) -> None:
    """Parent directory của db_path (ví dụ `runtime/`) phải được tạo tự động."""
    db_path = tmp_path / "nested" / "runtime" / "ideal_qr_tool.db"
    assert not db_path.parent.exists()

    engine = DbEngine(db_path)
    await engine.init_schema()

    assert db_path.parent.exists()
    assert db_path.exists()

    await engine.close()


async def test_init_schema_is_idempotent(tmp_path: Path) -> None:
    """Gọi init_schema() nhiều lần không raise và không phá dữ liệu đã có."""
    db_path = tmp_path / "ideal_qr_tool.db"
    engine = DbEngine(db_path)

    await engine.init_schema()
    connection = await engine.get_connection()
    await connection.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?);", ("web.auth_token", '"secret"')
    )
    await connection.commit()

    await engine.init_schema()

    async with connection.execute("SELECT key, value FROM settings;") as cursor:
        rows = await cursor.fetchall()
    assert rows == [("web.auth_token", '"secret"')]

    await engine.close()


async def test_get_connection_returns_same_instance(tmp_path: Path) -> None:
    """get_connection() phải trả về đúng 1 connection dùng lại (không mở nhiều lần)."""
    db_path = tmp_path / "ideal_qr_tool.db"
    engine = DbEngine(db_path)

    connection_a = await engine.get_connection()
    connection_b = await engine.get_connection()

    assert connection_a is connection_b

    await engine.close()


async def test_settings_persist_after_reopening_same_file(tmp_path: Path) -> None:
    """Requirement 11.6: dữ liệu ghi vào phải đọc lại đúng khi mở engine mới trên cùng file."""
    db_path = tmp_path / "ideal_qr_tool.db"

    engine_a = DbEngine(db_path)
    await engine_a.init_schema()
    connection_a = await engine_a.get_connection()
    await connection_a.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?);", ("ideal.max_concurrent", "5")
    )
    await connection_a.commit()
    await engine_a.close()

    engine_b = DbEngine(db_path)
    await engine_b.init_schema()
    connection_b = await engine_b.get_connection()
    async with connection_b.execute(
        "SELECT value FROM settings WHERE key = ?;", ("ideal.max_concurrent",)
    ) as cursor:
        row = await cursor.fetchone()
    assert row == ("5",)

    await engine_b.close()


async def test_journal_mode_and_foreign_keys_pragmas_applied(tmp_path: Path) -> None:
    """WAL + foreign_keys=ON + busy_timeout + synchronous=NORMAL được bật khi mở connection.

    `busy_timeout` PHẢI > 0 để writer chờ khi bị lock thay vì raise
    `OperationalError: database is locked` ngay lập tức (default SQLite
    là 0). Contention xảy ra khi có nhiều process cùng chạm 1 DB file
    (backend + CLI cùng lúc, hoặc tool bên ngoài mở DB).

    `synchronous=NORMAL` an toàn trong WAL mode (SQLite doc recommendation)
    và giảm số fsync/commit → writer lock giữ ngắn hơn.
    """
    db_path = tmp_path / "ideal_qr_tool.db"
    engine = DbEngine(db_path)

    connection = await engine.get_connection()

    async with connection.execute("PRAGMA journal_mode;") as cursor:
        journal_mode_row = await cursor.fetchone()
    assert journal_mode_row is not None
    assert journal_mode_row[0].lower() == "wal"

    async with connection.execute("PRAGMA foreign_keys;") as cursor:
        foreign_keys_row = await cursor.fetchone()
    assert foreign_keys_row == (1,)

    async with connection.execute("PRAGMA busy_timeout;") as cursor:
        busy_timeout_row = await cursor.fetchone()
    assert busy_timeout_row is not None
    # Không hard-code chính xác 5000 để test không fragile nếu điều chỉnh
    # giá trị tương lai — chỉ đảm bảo > 0 (nghĩa là writer sẽ CHỜ, không
    # raise `database is locked` tức thì).
    assert busy_timeout_row[0] > 0

    async with connection.execute("PRAGMA synchronous;") as cursor:
        synchronous_row = await cursor.fetchone()
    assert synchronous_row is not None
    # SQLite trả integer: 0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA.
    assert synchronous_row[0] == 1

    await engine.close()


async def test_close_is_idempotent(tmp_path: Path) -> None:
    """Gọi close() nhiều lần không raise."""
    db_path = tmp_path / "ideal_qr_tool.db"
    engine = DbEngine(db_path)

    await engine.get_connection()
    await engine.close()
    await engine.close()
