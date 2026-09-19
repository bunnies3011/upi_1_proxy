"""Unit tests for batch-tally schema migration in `core/db.py`.

Covers:
- `telegram_batch_tally` table columns
- `jobs.plan_outcome_notified` column (default 0 on existing rows)
- idempotent `init_schema` (safe to call twice)
- upgrade path from a pre-migration `jobs` table
"""

from __future__ import annotations

from pathlib import Path

from app.core.db import DbEngine

_BATCH_TALLY_COLUMNS = {
    "chat_id",
    "plus_count",
    "expired_count",
    "tally_message_id",
    "updated_at",
}


async def test_init_schema_creates_batch_tally_table_and_plan_outcome_column(
    tmp_path: Path,
) -> None:
    """Fresh DB: table + column exist with expected columns after init_schema."""
    db_path = tmp_path / "ideal_qr_tool.db"
    engine = DbEngine(db_path)

    await engine.init_schema()

    connection = await engine.get_connection()

    async with connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='telegram_batch_tally';"
    ) as cursor:
        table_row = await cursor.fetchone()
    assert table_row is not None

    async with connection.execute(
        "PRAGMA table_info(telegram_batch_tally);"
    ) as cursor:
        tally_columns = {row[1] for row in await cursor.fetchall()}
    assert tally_columns == _BATCH_TALLY_COLUMNS

    async with connection.execute("PRAGMA table_info(jobs);") as cursor:
        jobs_columns = {row[1] for row in await cursor.fetchall()}
    assert "plan_outcome_notified" in jobs_columns

    await engine.close()


async def test_init_schema_is_idempotent_for_batch_tally(
    tmp_path: Path,
) -> None:
    """Calling init_schema twice does not raise; schema remains complete."""
    db_path = tmp_path / "ideal_qr_tool.db"
    engine = DbEngine(db_path)

    await engine.init_schema()
    await engine.init_schema()

    connection = await engine.get_connection()
    async with connection.execute(
        "PRAGMA table_info(telegram_batch_tally);"
    ) as cursor:
        tally_columns = {row[1] for row in await cursor.fetchall()}
    assert tally_columns == _BATCH_TALLY_COLUMNS

    async with connection.execute("PRAGMA table_info(jobs);") as cursor:
        jobs_columns = {row[1] for row in await cursor.fetchall()}
    assert "plan_outcome_notified" in jobs_columns

    await engine.close()


async def test_init_schema_adds_plan_outcome_notified_to_pre_migration_jobs(
    tmp_path: Path,
) -> None:
    """Pre-migration jobs table upgrades; existing rows default to 0."""
    db_path = tmp_path / "ideal_qr_tool.db"
    engine = DbEngine(db_path)
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
            telegram_notifications_json TEXT NOT NULL DEFAULT '[]',
            held INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    await connection.execute(
        """
        INSERT INTO jobs (
            job_id, payment_method, account_line, status, order_num,
            created_at, updated_at
        ) VALUES ('job-old', 'ideal', 'acc|pw|totp', 'pending', 1, 1.0, 1.0)
        """
    )
    await connection.commit()

    async with connection.execute("PRAGMA table_info(jobs);") as cursor:
        columns_before = {row[1] for row in await cursor.fetchall()}
    assert "plan_outcome_notified" not in columns_before

    await engine.init_schema()

    async with connection.execute("PRAGMA table_info(jobs);") as cursor:
        columns_after = {row[1] for row in await cursor.fetchall()}
    assert "plan_outcome_notified" in columns_after

    async with connection.execute(
        "SELECT plan_outcome_notified FROM jobs WHERE job_id = 'job-old'"
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert int(row[0]) == 0

    async with connection.execute(
        "PRAGMA table_info(telegram_batch_tally);"
    ) as cursor:
        tally_columns = {row[1] for row in await cursor.fetchall()}
    assert tally_columns == _BATCH_TALLY_COLUMNS

    await engine.close()
