"""Isolation tests for atomic claim+increment on a dedicated write connection.

Proves:
  (a) A failed increment rolls back only the claim transaction — a write
      already committed on the shared connection stays intact, and the job
      stays countable (`plan_outcome_notified = 0`). The claim path must not
      touch the shared connection (inject targets the write connection only).
  (b) Concurrent claims on different jobs each increment exactly once.

These assertions fail if the claim path were reverted to the shared
connection: the write-connection inject would not fire (claim would succeed
and leave the job claimed), and/or a shared `get_connection` guard would
trip.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from app.core.db import DbEngine
from app.core.job_repo import JobRepository, JobRow


async def _make_repo(sqlite_path: Path) -> tuple[DbEngine, JobRepository]:
    engine = DbEngine(sqlite_path)
    await engine.init_schema()
    return engine, JobRepository(engine)


def _job_row(job_id: str, order_num: int = 1) -> JobRow:
    return JobRow(
        job_id=job_id,
        payment_method="ideal",
        account_line=f"acc-{job_id}|pw|totp",
        status="qr_ready",
        order_num=order_num,
        created_at=1000.0,
        updated_at=1000.0,
        dedup_key=None,
        artifact_path=None,
        error_code=None,
        error_message=None,
        payment_link=None,
        settings_snapshot={},
    )


async def _flag(engine: DbEngine, job_id: str) -> int | None:
    connection = await engine.get_connection()
    cursor = await connection.execute(
        "SELECT plan_outcome_notified FROM jobs WHERE job_id = ?",
        (job_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return None
    return int(row[0])


async def test_failed_claim_rolls_back_without_touching_shared_writes(
    sqlite_path: Path,
) -> None:
    """Increment failure rolls back claim only; shared committed write survives.

    Sequence:
      1. Commit a marker on the shared connection just before claim.
      2. Inject failure on the dedicated write connection during tally upsert.
      3. Guard `get_connection` so claim must not touch the shared path.
      4. Claim raises → dedicated rollback → job stays unclaimed (flag=0).
      5. Shared marker remains committed.

    If claim used the shared connection, the write-only inject would not fire
    (job would be claimed) and/or the get_connection guard would trip.
    """
    engine, repo = await _make_repo(sqlite_path)
    try:
        await repo.upsert(_job_row("job-a", 1))

        shared = await engine.get_connection()
        write = await engine.get_write_connection()
        assert write is not shared

        # Sibling write đã commit trên shared ngay trước claim.
        await shared.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            ("tally.isolation.marker_committed", '"ok"'),
        )
        await shared.commit()

        original_execute = write.execute

        async def _fail_on_tally_insert(
            sql: str, parameters: Any = (), /
        ) -> Any:
            sql_upper = sql.upper()
            if (
                "TELEGRAM_BATCH_TALLY" in sql_upper
                and "INSERT" in sql_upper
            ):
                raise RuntimeError("injected increment failure")
            return await original_execute(sql, parameters)

        async def _shared_must_not_be_used() -> Any:
            raise AssertionError(
                "try_claim_and_increment_tally must not call get_connection()"
            )

        with patch.object(engine, "get_connection", side_effect=_shared_must_not_be_used):
            with patch.object(write, "execute", side_effect=_fail_on_tally_insert):
                with pytest.raises(
                    RuntimeError, match="injected increment failure"
                ):
                    await repo.try_claim_and_increment_tally(
                        "chat-1", "job-a", plus_delta=1
                    )

        # Claim rolled back — job vẫn countable, chưa stranded.
        assert await _flag(engine, "job-a") == 0
        plus, expired, msg_id = await repo.read_batch_tally("chat-1")
        assert (plus, expired, msg_id) == (0, 0, None)

        # Write đã commit trên shared không bị claim rollback.
        cursor = await shared.execute(
            "SELECT value FROM settings WHERE key = ?",
            ("tally.isolation.marker_committed",),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        assert row[0] == '"ok"'

        # Job vẫn claim được đúng 1 lần sau lỗi.
        claimed, counts = await repo.try_claim_and_increment_tally(
            "chat-1", "job-a", plus_delta=1
        )
        assert claimed is True
        assert counts == (1, 0)
        assert await _flag(engine, "job-a") == 1
    finally:
        await engine.close()


async def test_concurrent_claims_each_increment_exactly_once(
    sqlite_path: Path,
) -> None:
    """N concurrent claims on distinct jobs each increment plus_count once."""
    engine, repo = await _make_repo(sqlite_path)
    n_jobs = 8
    try:
        for i in range(n_jobs):
            await repo.upsert(_job_row(f"job-{i}", order_num=i + 1))

        results = await asyncio.gather(
            *[
                repo.try_claim_and_increment_tally(
                    "chat-concurrent", f"job-{i}", plus_delta=1
                )
                for i in range(n_jobs)
            ]
        )

        successes = [r for r in results if r[0] is True]
        assert len(successes) == n_jobs
        # Mỗi claim thành công trả count sau khi tăng; count cuối = n_jobs.
        final_counts = {counts for _, counts in successes}
        assert (n_jobs, 0) in final_counts

        plus, expired, _ = await repo.read_batch_tally("chat-concurrent")
        assert (plus, expired) == (n_jobs, 0)

        for i in range(n_jobs):
            assert await _flag(engine, f"job-{i}") == 1

        # Claim lại không tăng thêm.
        claimed_again, counts_again = await repo.try_claim_and_increment_tally(
            "chat-concurrent", "job-0", plus_delta=1
        )
        assert claimed_again is False
        assert counts_again is None
        plus2, expired2, _ = await repo.read_batch_tally("chat-concurrent")
        assert (plus2, expired2) == (n_jobs, 0)
    finally:
        await engine.close()
