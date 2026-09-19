"""Unit tests for batch-tally persistence on `JobRepository`.

Covers claim-and-increment atomicity, deferred re-arm, receipted close,
message-id round-trip, and that normal job upsert leaves the dedup flag
untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from app.core.db import DbEngine
from app.core.job_repo import BatchTallyRow, JobRepository, JobRow


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


async def test_try_claim_and_increment_first_claim_then_repeat_rejected(
    sqlite_path: Path,
) -> None:
    """First claim counts; second claim for same job is rejected."""
    engine, repo = await _make_repo(sqlite_path)
    try:
        await repo.upsert(_job_row("job-a", 1))

        claimed, counts = await repo.try_claim_and_increment_tally(
            "chat-1", "job-a", plus_delta=1
        )
        assert claimed is True
        assert counts == (1, 0)
        assert await _flag(engine, "job-a") == 1

        claimed2, counts2 = await repo.try_claim_and_increment_tally(
            "chat-1", "job-a", plus_delta=1
        )
        assert claimed2 is False
        assert counts2 is None
        assert await _flag(engine, "job-a") == 1

        plus, expired, msg_id = await repo.read_batch_tally("chat-1")
        assert (plus, expired, msg_id) == (1, 0, None)
    finally:
        await engine.close()


async def test_try_claim_and_increment_accumulates_across_jobs_and_deltas(
    sqlite_path: Path,
) -> None:
    """Two jobs on one chat accumulate; plus/expired deltas are independent."""
    engine, repo = await _make_repo(sqlite_path)
    try:
        await repo.upsert(_job_row("job-1", 1))
        await repo.upsert(_job_row("job-2", 2))
        await repo.upsert(_job_row("job-3", 3))

        claimed1, counts1 = await repo.try_claim_and_increment_tally(
            "chat-1", "job-1", plus_delta=1
        )
        assert claimed1 is True
        assert counts1 == (1, 0)

        claimed2, counts2 = await repo.try_claim_and_increment_tally(
            "chat-1", "job-2", plus_delta=1
        )
        assert claimed2 is True
        assert counts2 == (2, 0)

        claimed3, counts3 = await repo.try_claim_and_increment_tally(
            "chat-1", "job-3", expired_delta=1
        )
        assert claimed3 is True
        assert counts3 == (2, 1)

        plus, expired, _ = await repo.read_batch_tally("chat-1")
        assert (plus, expired) == (2, 1)
    finally:
        await engine.close()


async def test_try_claim_rolls_back_when_increment_fails(
    sqlite_path: Path,
) -> None:
    """If tally upsert raises, claim is not persisted (single transaction)."""
    engine, repo = await _make_repo(sqlite_path)
    try:
        await repo.upsert(_job_row("job-a", 1))
        # Claim path chạy trên write connection riêng — inject trên connection đó.
        connection = await engine.get_write_connection()
        original_execute = connection.execute

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

        with patch.object(connection, "execute", side_effect=_fail_on_tally_insert):
            with pytest.raises(RuntimeError, match="injected increment failure"):
                await repo.try_claim_and_increment_tally(
                    "chat-1", "job-a", plus_delta=1
                )

        assert await _flag(engine, "job-a") == 0
        plus, expired, msg_id = await repo.read_batch_tally("chat-1")
        assert (plus, expired, msg_id) == (0, 0, None)

        # After rollback the job remains claimable exactly once.
        claimed, counts = await repo.try_claim_and_increment_tally(
            "chat-1", "job-a", plus_delta=1
        )
        assert claimed is True
        assert counts == (1, 0)
    finally:
        await engine.close()


async def test_try_claim_absent_job_returns_false_and_logs(
    sqlite_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing job row returns (False, None) and logs a warning."""
    engine, repo = await _make_repo(sqlite_path)
    try:
        with caplog.at_level("WARNING", logger="app.core.job_repo"):
            claimed, counts = await repo.try_claim_and_increment_tally(
                "chat-1", "missing-job", plus_delta=1
            )
        assert claimed is False
        assert counts is None
        assert any(
            "missing-job" in record.getMessage()
            and "absent" in record.getMessage().lower()
            for record in caplog.records
        )
        plus, expired, msg_id = await repo.read_batch_tally("chat-1")
        assert (plus, expired, msg_id) == (0, 0, None)
    finally:
        await engine.close()


async def test_defer_blocks_same_period_recount_and_rearm_restores_claim(
    sqlite_path: Path,
) -> None:
    """defer 1→2 blocks recount; rearm 2→0 makes job claimable again."""
    engine, repo = await _make_repo(sqlite_path)
    try:
        await repo.upsert(_job_row("job-a", 1))
        await repo.upsert(_job_row("job-b", 2))

        claimed, _ = await repo.try_claim_and_increment_tally(
            "chat-1", "job-a", plus_delta=1
        )
        assert claimed is True
        assert await _flag(engine, "job-a") == 1

        await repo.defer_plan_outcome_rearm("job-a")
        assert await _flag(engine, "job-a") == 2

        claimed_again, counts_again = await repo.try_claim_and_increment_tally(
            "chat-1", "job-a", plus_delta=1
        )
        assert claimed_again is False
        assert counts_again is None
        assert await _flag(engine, "job-a") == 2

        # Never-counted job stays at 0 through defer (no-op).
        await repo.defer_plan_outcome_rearm("job-b")
        assert await _flag(engine, "job-b") == 0
        claimed_b, counts_b = await repo.try_claim_and_increment_tally(
            "chat-1", "job-b", plus_delta=1
        )
        assert claimed_b is True
        assert counts_b == (2, 0)

        await repo.rearm_deferred_plan_outcomes()
        assert await _flag(engine, "job-a") == 0
        assert await _flag(engine, "job-b") == 1  # still counted, not deferred

        claimed_rearmed, counts_rearmed = await repo.try_claim_and_increment_tally(
            "chat-1", "job-a", plus_delta=1
        )
        assert claimed_rearmed is True
        assert counts_rearmed == (3, 0)
    finally:
        await engine.close()


async def test_read_and_set_batch_tally_message_id_round_trip(
    sqlite_path: Path,
) -> None:
    """Message id round-trips; unknown chat reads as (0, 0, None)."""
    engine, repo = await _make_repo(sqlite_path)
    try:
        assert await repo.read_batch_tally("unknown") == (0, 0, None)

        await repo.upsert(_job_row("job-a", 1))
        await repo.try_claim_and_increment_tally(
            "chat-1", "job-a", plus_delta=1
        )
        await repo.set_batch_tally_message_id("chat-1", 4242)

        plus, expired, msg_id = await repo.read_batch_tally("chat-1")
        assert (plus, expired, msg_id) == (1, 0, 4242)

        # Upsert message id alone for a chat without prior counts.
        await repo.set_batch_tally_message_id("chat-new", 7)
        plus2, expired2, msg_id2 = await repo.read_batch_tally("chat-new")
        assert (plus2, expired2, msg_id2) == (0, 0, 7)
    finally:
        await engine.close()


async def test_close_batch_period_decrements_receipted_amount_per_chat(
    sqlite_path: Path,
) -> None:
    """Close decrements by receipted amount so late plus survives; other chat intact."""
    engine, repo = await _make_repo(sqlite_path)
    try:
        for i in range(1, 12):
            await repo.upsert(_job_row(f"job-{i}", i))
        for i in range(1, 11):
            claimed, _ = await repo.try_claim_and_increment_tally(
                "chat-1", f"job-{i}", plus_delta=1
            )
            assert claimed is True
        # Concurrent late plus after snapshot of 10.
        claimed_late, counts_late = await repo.try_claim_and_increment_tally(
            "chat-1", "job-11", plus_delta=1
        )
        assert claimed_late is True
        assert counts_late == (11, 0)

        await repo.set_batch_tally_message_id("chat-1", 999)

        # Second chat stays untouched by close of chat-1.
        await repo.upsert(_job_row("job-other", 100))
        await repo.try_claim_and_increment_tally(
            "chat-2", "job-other", plus_delta=1, expired_delta=2
        )
        await repo.set_batch_tally_message_id("chat-2", 111)

        await repo.close_batch_period(
            "chat-1", plus_receipted=10, expired_receipted=0
        )

        plus1, expired1, msg1 = await repo.read_batch_tally("chat-1")
        assert (plus1, expired1, msg1) == (1, 0, None)

        plus2, expired2, msg2 = await repo.read_batch_tally("chat-2")
        assert (plus2, expired2, msg2) == (1, 2, 111)
    finally:
        await engine.close()


async def test_close_batch_period_clamps_counts_at_zero(
    sqlite_path: Path,
) -> None:
    """Over-decrement from concurrent close must not drive counters negative."""
    engine, repo = await _make_repo(sqlite_path)
    try:
        await repo.upsert(_job_row("job-1", 1))
        claimed, counts = await repo.try_claim_and_increment_tally(
            "chat-1", "job-1", plus_delta=1, expired_delta=1
        )
        assert claimed is True
        assert counts == (1, 1)

        await repo.close_batch_period(
            "chat-1", plus_receipted=5, expired_receipted=9
        )
        plus, expired, mid = await repo.read_batch_tally("chat-1")
        assert (plus, expired, mid) == (0, 0, None)
    finally:
        await engine.close()


async def test_list_batch_tally_returns_all_rows(
    sqlite_path: Path,
) -> None:
    """list_batch_tally returns every chat row as BatchTallyRow."""
    engine, repo = await _make_repo(sqlite_path)
    try:
        await repo.upsert(_job_row("job-a", 1))
        await repo.upsert(_job_row("job-b", 2))
        await repo.try_claim_and_increment_tally(
            "chat-a", "job-a", plus_delta=1
        )
        await repo.try_claim_and_increment_tally(
            "chat-b", "job-b", expired_delta=1
        )
        await repo.set_batch_tally_message_id("chat-a", 10)

        rows = await repo.list_batch_tally()
        assert len(rows) == 2
        assert all(isinstance(r, BatchTallyRow) for r in rows)
        by_chat = {r.chat_id: r for r in rows}
        assert by_chat["chat-a"].plus_count == 1
        assert by_chat["chat-a"].expired_count == 0
        assert by_chat["chat-a"].tally_message_id == 10
        assert by_chat["chat-b"].plus_count == 0
        assert by_chat["chat-b"].expired_count == 1
        assert by_chat["chat-b"].tally_message_id is None
        assert isinstance(by_chat["chat-a"].updated_at, str)
        assert by_chat["chat-a"].updated_at
    finally:
        await engine.close()


async def test_claimed_flag_survives_normal_job_upsert(
    sqlite_path: Path,
) -> None:
    """Normal jobs upsert must not reset plan_outcome_notified after claim."""
    engine, repo = await _make_repo(sqlite_path)
    try:
        row = _job_row("job-a", 1)
        await repo.upsert(row)

        claimed, counts = await repo.try_claim_and_increment_tally(
            "chat-1", "job-a", plus_delta=1
        )
        assert claimed is True
        assert counts == (1, 0)
        assert await _flag(engine, "job-a") == 1

        # Re-persist the job the way JobManager does on every transition.
        row.status = "qr_ready"
        row.plan = "plus"
        row.updated_at = 2000.0
        await repo.upsert(row)

        assert await _flag(engine, "job-a") == 1
        claimed2, counts2 = await repo.try_claim_and_increment_tally(
            "chat-1", "job-a", plus_delta=1
        )
        assert claimed2 is False
        assert counts2 is None
        plus, expired, _ = await repo.read_batch_tally("chat-1")
        assert (plus, expired) == (1, 0)
    finally:
        await engine.close()
