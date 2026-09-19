"""Unit tests for JobManager batch-tally delegation wrappers.

Covers fail-safe vs surface-error contracts (mirroring worker-stat
wrappers) and force_rerun defer of counted plan outcomes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.core.db import DbEngine
from app.core.job_manager import JobManager
from app.core.job_repo import BatchTallyRow, JobRepository
from app.core.payment_flow import JobResult, JobStatus, ParsedAccount

from tests.unit.test_job_manager import (
    FakeHandler,
    FakeProxyPool,
    FakeSettings,
    FakeSse,
)


class DedupFakeHandler(FakeHandler):
    """FakeHandler with dedup so force_rerun reuses the same job_id."""

    def get_account_dedup_key(self, parsed: ParsedAccount) -> str:
        return parsed.raw_line


class FakeBatchTallyRepo:
    """Records batch-tally calls and returns canned values / raises."""

    def __init__(self) -> None:
        self.claim_calls: list[tuple[str, str, int, int]] = []
        self.read_calls: list[str] = []
        self.persist_calls: list[tuple[str, int]] = []
        self.list_calls: int = 0
        self.close_calls: list[tuple[str, int, int]] = []
        self.rearm_calls: int = 0
        self.defer_calls: list[str] = []

        self.claim_result: tuple[bool, tuple[int, int] | None] = (
            True,
            (3, 1),
        )
        self.read_result: tuple[int, int, int | None] = (2, 1, 99)
        self.list_result: list[BatchTallyRow] = [
            BatchTallyRow(
                chat_id="c1",
                plus_count=1,
                expired_count=0,
                tally_message_id=None,
                updated_at="2026-01-01T00:00:00.000Z",
            )
        ]

        self.claim_error: Exception | None = None
        self.read_error: Exception | None = None
        self.persist_error: Exception | None = None
        self.list_error: Exception | None = None
        self.close_error: Exception | None = None
        self.rearm_error: Exception | None = None

    async def try_claim_and_increment_tally(
        self,
        chat_id: str,
        job_id: str,
        *,
        plus_delta: int = 0,
        expired_delta: int = 0,
    ) -> tuple[bool, tuple[int, int] | None]:
        self.claim_calls.append((chat_id, job_id, plus_delta, expired_delta))
        if self.claim_error is not None:
            raise self.claim_error
        return self.claim_result

    async def read_batch_tally(
        self, chat_id: str
    ) -> tuple[int, int, int | None]:
        self.read_calls.append(chat_id)
        if self.read_error is not None:
            raise self.read_error
        return self.read_result

    async def set_batch_tally_message_id(
        self, chat_id: str, message_id: int
    ) -> None:
        self.persist_calls.append((chat_id, message_id))
        if self.persist_error is not None:
            raise self.persist_error

    async def list_batch_tally(self) -> list[BatchTallyRow]:
        self.list_calls += 1
        if self.list_error is not None:
            raise self.list_error
        return list(self.list_result)

    async def close_batch_period(
        self,
        chat_id: str,
        *,
        plus_receipted: int,
        expired_receipted: int,
    ) -> None:
        self.close_calls.append((chat_id, plus_receipted, expired_receipted))
        if self.close_error is not None:
            raise self.close_error

    async def rearm_deferred_plan_outcomes(self) -> None:
        self.rearm_calls += 1
        if self.rearm_error is not None:
            raise self.rearm_error

    async def defer_plan_outcome_rearm(self, job_id: str) -> None:
        self.defer_calls.append(job_id)

    async def upsert(self, row: Any) -> None:  # noqa: ARG002
        pass


def _make_manager_with_repo(
    repo: Any | None,
    *,
    snapshot: dict[str, Any] | None = None,
) -> JobManager:
    settings = FakeSettings(snapshot=snapshot or {})
    proxy_pool = FakeProxyPool()
    sse = FakeSse()
    return JobManager(
        settings=settings,  # type: ignore[arg-type]
        proxy_pool=proxy_pool,  # type: ignore[arg-type]
        sse=sse,  # type: ignore[arg-type]
        job_repo=repo,
    )


# ---------------------------------------------------------------------------
# Delegation + fail-safe contracts (fake repo)
# ---------------------------------------------------------------------------


async def test_claim_and_count_forwards_deltas_and_returns_repo_result() -> None:
    repo = FakeBatchTallyRepo()
    repo.claim_result = (True, (5, 2))
    manager = _make_manager_with_repo(repo)

    claimed, counts = await manager.claim_and_count_plan_outcome(
        "chat-1", "job-a", plus_delta=1, expired_delta=0
    )

    assert claimed is True
    assert counts == (5, 2)
    assert repo.claim_calls == [("chat-1", "job-a", 1, 0)]


async def test_claim_and_count_repo_error_returns_false_none_without_raise() -> None:
    repo = FakeBatchTallyRepo()
    repo.claim_error = RuntimeError("db down")
    manager = _make_manager_with_repo(repo)

    claimed, counts = await manager.claim_and_count_plan_outcome(
        "chat-1", "job-a", plus_delta=1
    )

    assert claimed is False
    assert counts is None


async def test_claim_and_count_missing_repo_returns_false_none() -> None:
    manager = _make_manager_with_repo(None)

    claimed, counts = await manager.claim_and_count_plan_outcome(
        "chat-1", "job-a", plus_delta=1
    )

    assert claimed is False
    assert counts is None


async def test_read_batch_tally_missing_repo_defaults_to_zeros() -> None:
    manager = _make_manager_with_repo(None)
    assert await manager.read_batch_tally("chat-1") == (0, 0, None)


async def test_read_batch_tally_forwards_and_returns_repo_result() -> None:
    repo = FakeBatchTallyRepo()
    repo.read_result = (4, 3, 42)
    manager = _make_manager_with_repo(repo)

    assert await manager.read_batch_tally("chat-9") == (4, 3, 42)
    assert repo.read_calls == ["chat-9"]


async def test_list_batch_tally_chats_missing_repo_returns_empty() -> None:
    manager = _make_manager_with_repo(None)
    assert await manager.list_batch_tally_chats() == []


async def test_list_batch_tally_chats_forwards_repo_rows() -> None:
    repo = FakeBatchTallyRepo()
    manager = _make_manager_with_repo(repo)

    rows = await manager.list_batch_tally_chats()
    assert len(rows) == 1
    assert rows[0].chat_id == "c1"
    assert repo.list_calls == 1


async def test_persist_tally_message_id_default_swallows_repo_error() -> None:
    repo = FakeBatchTallyRepo()
    repo.persist_error = RuntimeError("write failed")
    manager = _make_manager_with_repo(repo)

    await manager.persist_tally_message_id("chat-1", 100)
    assert repo.persist_calls == [("chat-1", 100)]


async def test_persist_tally_message_id_durable_propagates_repo_error() -> None:
    repo = FakeBatchTallyRepo()
    repo.persist_error = RuntimeError("write failed")
    manager = _make_manager_with_repo(repo)

    with pytest.raises(RuntimeError, match="write failed"):
        await manager.persist_tally_message_id(
            "chat-1", 100, durable=True
        )


async def test_close_batch_period_propagates_repo_error() -> None:
    repo = FakeBatchTallyRepo()
    repo.close_error = RuntimeError("close failed")
    manager = _make_manager_with_repo(repo)

    with pytest.raises(RuntimeError, match="close failed"):
        await manager.close_batch_period(
            "chat-1", plus_receipted=1, expired_receipted=0
        )


async def test_rearm_deferred_plan_outcomes_propagates_repo_error() -> None:
    repo = FakeBatchTallyRepo()
    repo.rearm_error = RuntimeError("rearm failed")
    manager = _make_manager_with_repo(repo)

    with pytest.raises(RuntimeError, match="rearm failed"):
        await manager.rearm_deferred_plan_outcomes()


async def test_close_and_rearm_no_repo_are_noop() -> None:
    manager = _make_manager_with_repo(None)
    await manager.close_batch_period(
        "chat-1", plus_receipted=1, expired_receipted=0
    )
    await manager.rearm_deferred_plan_outcomes()


# ---------------------------------------------------------------------------
# force_rerun defers counted outcomes (real temp repo)
# ---------------------------------------------------------------------------


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


async def _mark_terminal(manager: JobManager, job_id: str) -> None:
    record = manager.get_job(job_id)
    assert record is not None
    record.status = JobStatus.ERROR
    record.plan = "plus"
    await manager._persist_record(record)  # noqa: SLF001


async def test_force_rerun_defers_counted_outcome_not_same_period_recount(
    sqlite_path: Path,
) -> None:
    """Counted account: force_rerun clears plan, defers flag, no recount
    until rearm for the next period."""
    engine = DbEngine(sqlite_path)
    await engine.init_schema()
    repo = JobRepository(engine)
    try:
        manager = _make_manager_with_repo(repo)
        manager.register_handler(
            "ideal",
            DedupFakeHandler(run_result=JobResult(status=JobStatus.QR_READY)),
        )

        result = await manager.submit_batch(
            "ideal", ["a@x|p"], start=False
        )
        job_id = result.created_job_ids[0]
        await _mark_terminal(manager, job_id)

        claimed, counts = await manager.claim_and_count_plan_outcome(
            "chat-1", job_id, plus_delta=1
        )
        assert claimed is True
        assert counts == (1, 0)
        assert await _flag(engine, job_id) == 1

        rerun = await manager.submit_batch(
            "ideal", ["a@x|p"], force_rerun=True, start=False
        )
        assert rerun.created_job_ids == [job_id]
        record = manager.get_job(job_id)
        assert record is not None
        assert record.plan is None
        assert await _flag(engine, job_id) == 2

        claimed_again, counts_again = await manager.claim_and_count_plan_outcome(
            "chat-1", job_id, plus_delta=1
        )
        assert claimed_again is False
        assert counts_again is None
        assert await _flag(engine, job_id) == 2

        await manager.rearm_deferred_plan_outcomes()
        assert await _flag(engine, job_id) == 0

        claimed_rearmed, counts_rearmed = (
            await manager.claim_and_count_plan_outcome(
                "chat-1", job_id, plus_delta=1
            )
        )
        assert claimed_rearmed is True
        assert counts_rearmed == (2, 0)
    finally:
        await engine.close()


async def test_force_rerun_never_counted_job_stays_claimable(
    sqlite_path: Path,
) -> None:
    """Never-counted account stays claimable through force_rerun."""
    engine = DbEngine(sqlite_path)
    await engine.init_schema()
    repo = JobRepository(engine)
    try:
        manager = _make_manager_with_repo(repo)
        manager.register_handler(
            "ideal",
            DedupFakeHandler(run_result=JobResult(status=JobStatus.QR_READY)),
        )

        result = await manager.submit_batch(
            "ideal", ["b@x|p"], start=False
        )
        job_id = result.created_job_ids[0]
        await _mark_terminal(manager, job_id)
        assert await _flag(engine, job_id) == 0

        await manager.submit_batch(
            "ideal", ["b@x|p"], force_rerun=True, start=False
        )
        assert await _flag(engine, job_id) == 0

        claimed, counts = await manager.claim_and_count_plan_outcome(
            "chat-1", job_id, plus_delta=1
        )
        assert claimed is True
        assert counts == (1, 0)
        assert await _flag(engine, job_id) == 1
    finally:
        await engine.close()


async def test_force_rerun_multi_account_defers_only_counted_jobs(
    sqlite_path: Path,
) -> None:
    """Multi-account force_rerun defers counted jobs; leaves flag 0 alone."""
    engine = DbEngine(sqlite_path)
    await engine.init_schema()
    repo = JobRepository(engine)
    try:
        manager = _make_manager_with_repo(repo)
        manager.register_handler(
            "ideal",
            DedupFakeHandler(run_result=JobResult(status=JobStatus.QR_READY)),
        )

        created = await manager.submit_batch(
            "ideal",
            ["c1@x|p", "c2@x|p"],
            start=False,
        )
        job_a, job_b = created.created_job_ids
        await _mark_terminal(manager, job_a)
        await _mark_terminal(manager, job_b)

        claimed_a, _ = await manager.claim_and_count_plan_outcome(
            "chat-1", job_a, plus_delta=1
        )
        assert claimed_a is True
        assert await _flag(engine, job_a) == 1
        assert await _flag(engine, job_b) == 0

        await manager.submit_batch(
            "ideal",
            ["c1@x|p", "c2@x|p"],
            force_rerun=True,
            start=False,
        )
        assert await _flag(engine, job_a) == 2
        assert await _flag(engine, job_b) == 0
    finally:
        await engine.close()
