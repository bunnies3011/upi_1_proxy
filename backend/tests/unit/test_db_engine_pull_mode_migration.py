"""Unit test cho migration Pull_Mode + Plus_Verify trong `core/db.py`.

Requirements: 21.4 (migration idempotent — check PRAGMA table_info trước
khi ALTER, không lỗi "duplicate column name" khi cột đã tồn tại), 21.7
(mỗi cột/bảng được check TỒN TẠI ĐỘC LẬP — an toàn nếu DB đã có một phần
cột từ lần chạy trước bị crash giữa đường).
"""

from __future__ import annotations

from pathlib import Path

from app.core.db import DbEngine

# 6 cột Pull_Mode (Requirement 21.1) + 4 cột Plus_Verify — tổng 10 cột mới
# được migration thêm vào bảng `jobs` đã tồn tại từ trước.
_PULL_MODE_JOBS_COLUMNS = {
    "pull_assignment_state",
    "pull_assigned_telegram_user_id",
    "pull_origin_chat_id",
    "pull_outcome",
    "pull_assigned_username",
    "pull_assigned_first_name",
}
_PLUS_VERIFY_JOBS_COLUMNS = {
    "plus_check_state",
    "plus_check_attempts",
    "telegram_message_chat_id",
    "telegram_message_id",
}
_ALL_NEW_JOBS_COLUMNS = _PULL_MODE_JOBS_COLUMNS | _PLUS_VERIFY_JOBS_COLUMNS

_TELEGRAM_WORKER_STATS_COLUMNS = {
    "telegram_user_id",
    "username",
    "first_name",
    "success_count",
    "fail_count",
    "updated_at",
}


async def test_init_schema_called_twice_does_not_raise_and_has_full_schema(
    tmp_path: Path,
) -> None:
    """Gọi init_schema() 2 lần trên cùng file DB tạm không raise, schema đủ 10 cột mới."""
    db_path = tmp_path / "ideal_qr_tool.db"
    engine = DbEngine(db_path)

    await engine.init_schema()
    await engine.init_schema()

    connection = await engine.get_connection()

    async with connection.execute("PRAGMA table_info(jobs);") as cursor:
        jobs_columns = {row[1] for row in await cursor.fetchall()}
    assert _ALL_NEW_JOBS_COLUMNS <= jobs_columns

    async with connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='telegram_worker_stats';"
    ) as cursor:
        table_row = await cursor.fetchone()
    assert table_row is not None

    await engine.close()


async def test_init_schema_migrates_only_missing_columns_when_partial_exists(
    tmp_path: Path,
) -> None:
    """DB đã có 3/6 cột Pull_Mode (tạo thủ công) — init_schema() chỉ tạo phần thiếu, không lỗi."""
    db_path = tmp_path / "ideal_qr_tool.db"
    engine = DbEngine(db_path)

    # Tạo bảng `jobs` phiên bản CŨ (trước migration Pull_Mode/Plus_Verify)
    # bằng cách chạy init_schema() 1 lần rồi giả lập DB "đã có 3/6 cột"
    # bằng cách tự ALTER TABLE thêm sẵn 3 trong 6 cột Pull_Mode trước khi
    # gọi init_schema() lần tiếp theo.
    connection = await engine.get_connection()
    await connection.execute(
        """
        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY,
            payment_method TEXT NOT NULL,
            account_line TEXT NOT NULL,
            status TEXT NOT NULL,
            order_num INTEGER NOT NULL UNIQUE,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            dedup_key TEXT,
            artifact_path TEXT,
            error_code TEXT,
            error_message TEXT,
            payment_link TEXT,
            settings_snapshot_json TEXT NOT NULL DEFAULT '{}',
            retry_count INTEGER NOT NULL DEFAULT 0,
            started_at REAL,
            finished_at REAL,
            telegram_notifications_json TEXT NOT NULL DEFAULT '[]'
        );
        """
    )
    pre_existing_columns = [
        "pull_assignment_state",
        "pull_assigned_telegram_user_id",
        "pull_origin_chat_id",
    ]
    await connection.execute(
        "ALTER TABLE jobs ADD COLUMN pull_assignment_state TEXT NULL"
    )
    await connection.execute(
        "ALTER TABLE jobs ADD COLUMN pull_assigned_telegram_user_id TEXT NULL"
    )
    await connection.execute(
        "ALTER TABLE jobs ADD COLUMN pull_origin_chat_id TEXT NULL"
    )
    await connection.commit()

    async with connection.execute("PRAGMA table_info(jobs);") as cursor:
        columns_before = {row[1] for row in await cursor.fetchall()}
    assert set(pre_existing_columns) <= columns_before
    missing_before = _ALL_NEW_JOBS_COLUMNS - columns_before
    assert missing_before == (
        _ALL_NEW_JOBS_COLUMNS - set(pre_existing_columns)
    )

    # Gọi init_schema() không raise "duplicate column name" dù 3 cột đã có.
    await engine.init_schema()

    async with connection.execute("PRAGMA table_info(jobs);") as cursor:
        columns_after = {row[1] for row in await cursor.fetchall()}
    assert _ALL_NEW_JOBS_COLUMNS <= columns_after

    await engine.close()


async def test_telegram_worker_stats_table_created_with_exact_columns(
    tmp_path: Path,
) -> None:
    """`telegram_worker_stats` phải được tạo với đúng 6 cột (Requirement 15.1)."""
    db_path = tmp_path / "ideal_qr_tool.db"
    engine = DbEngine(db_path)

    await engine.init_schema()

    connection = await engine.get_connection()
    async with connection.execute(
        "PRAGMA table_info(telegram_worker_stats);"
    ) as cursor:
        columns = {row[1] for row in await cursor.fetchall()}

    assert columns == _TELEGRAM_WORKER_STATS_COLUMNS

    await engine.close()
