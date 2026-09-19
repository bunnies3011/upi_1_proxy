"""Batch tally restart safety, atomic claim-before-send, and period reset.

Uses real JobManager + temp-file JobRepository so claim/count/persist
survive across a simulated notifier restart.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core.db import DbEngine
from app.core.job_manager import JobManager, _JobRecord
from app.core.job_repo import JobRepository, JobRow
from app.core.payment_flow import Job, JobStatus, SimpleCancellationToken
from app.notifiers.telegram.client import TelegramApiError
from app.notifiers.telegram.distributor import RoundRobinDistributor
from app.notifiers.telegram.formatter import build_period_close_text
from app.notifiers.telegram.notifier import TelegramNotifier


class _FakeSettings:
    def __init__(self, snap: dict[str, Any] | None = None) -> None:
        self._snap = snap or {
            "telegram.push_mode.enabled": True,
            "telegram.bot_token": "1:ABC",
            "telegram.push_mode.send_photo": True,
            "telegram.push_mode.chat_targets": [
                {"chat_id": "-1001", "label": "W1", "enabled": True}
            ],
        }

    async def get(self, key: str) -> Any:
        return self._snap.get(key)

    async def list(self, prefix: str | None = None) -> dict[str, Any]:
        return dict(self._snap)


class _FakeSse:
    async def broadcast_job_status(self, *a, **k) -> None:
        return None

    async def broadcast_job_log(self, *a, **k) -> None:
        return None

    async def broadcast_job_notified(self, *a, **k) -> None:
        return None


class _FakeProxy:
    def __init__(self) -> None:
        from app.core.proxy_health import ProbeConfig

        self.probe_config = ProbeConfig(
            enabled=False, fallback_direct_on_exhausted=False
        )

    async def acquire(self, job_id: str, **kwargs):  # noqa: ARG002
        return None

    def release(self, lease) -> None:  # noqa: ARG002
        return None


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


def _mk_record(
    job_id: str,
    *,
    chat_id: str = "-1001",
    message_id: int = 55,
    plan: str | None = None,
) -> _JobRecord:
    job = Job(
        job_id=job_id,
        payment_method="ideal",
        account_line=f"{job_id}@x.com|pw",
        created_at=1.0,
        cancellation_token=SimpleCancellationToken(),
    )
    rec = _JobRecord(
        job=job,
        status=JobStatus.QR_READY,
        payment_link="https://pay.example/1",
        finished_at=1_700_000_000.0,
        plan=plan,
    )
    rec.telegram_message_chat_id = chat_id
    rec.telegram_message_id = message_id
    return rec


def _make_manager(repo: JobRepository) -> JobManager:
    return JobManager(
        settings=_FakeSettings(),  # type: ignore[arg-type]
        proxy_pool=_FakeProxy(),  # type: ignore[arg-type]
        sse=_FakeSse(),  # type: ignore[arg-type]
        job_repo=repo,
    )


def _make_client(**overrides: Any) -> AsyncMock:
    client = AsyncMock()
    client.send_message_with_reply_to = AsyncMock(
        return_value={"message_id": 9}
    )
    client.edit_message_caption = AsyncMock(return_value={})
    client.send_message = AsyncMock(return_value={"message_id": 100})
    client.edit_message_text = AsyncMock(return_value={})
    for key, value in overrides.items():
        setattr(client, key, value)
    return client


def _make_notifier(
    manager: JobManager, client: AsyncMock
) -> TelegramNotifier:
    return TelegramNotifier(
        settings=_FakeSettings(),  # type: ignore[arg-type]
        client=client,
        distributor=RoundRobinDistributor(),
        job_manager=manager,
    )


async def _seed_job(
    repo: JobRepository,
    manager: JobManager,
    job_id: str,
    *,
    order_num: int = 1,
    chat_id: str = "-1001",
    message_id: int = 55,
) -> _JobRecord:
    await repo.upsert(_job_row(job_id, order_num=order_num))
    rec = _mk_record(job_id, chat_id=chat_id, message_id=message_id)
    manager._jobs[job_id] = rec
    return rec


def test_build_period_close_text_is_pure() -> None:
    text = build_period_close_text(5, expired=2, reset_at="09:30:00 13/07/2026")
    assert "📋 Chốt kỳ" in text
    assert "✅ 5" in text
    assert "⌛ 2" in text
    assert "reset 09:30:00 13/07/2026" in text


@pytest.mark.asyncio
async def test_send_failure_after_claim_keeps_count_no_double_on_retry(
    sqlite_path: Path,
) -> None:
    """Claim commits before send; failed tag leaves count=1; retry dedupes."""
    engine = DbEngine(sqlite_path)
    await engine.init_schema()
    repo = JobRepository(engine)
    try:
        manager = _make_manager(repo)
        await _seed_job(repo, manager, "j1")

        client = _make_client(
            send_message_with_reply_to=AsyncMock(
                side_effect=RuntimeError("tag send boom")
            )
        )
        n = _make_notifier(manager, client)
        await n.notify_plus_verified("j1")

        plus, expired, _ = await manager.read_batch_tally("-1001")
        assert (plus, expired) == (1, 0)

        # Retry same job — claim rejects, no second count.
        client.send_message_with_reply_to = AsyncMock(
            return_value={"message_id": 9}
        )
        await n.notify_plus_verified("j1")
        plus2, expired2, _ = await manager.read_batch_tally("-1001")
        assert (plus2, expired2) == (1, 0)
        client.send_message_with_reply_to.assert_not_awaited()
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_restart_continues_count_and_edits_same_tally_message(
    sqlite_path: Path,
) -> None:
    """Second notifier over same DB continues #N and edits persisted mid."""
    engine = DbEngine(sqlite_path)
    await engine.init_schema()
    repo = JobRepository(engine)
    try:
        manager = _make_manager(repo)
        await _seed_job(repo, manager, "j1", order_num=1, message_id=11)
        await _seed_job(repo, manager, "j2", order_num=2, message_id=22)

        client1 = _make_client(
            send_message=AsyncMock(return_value={"message_id": 500})
        )
        n1 = _make_notifier(manager, client1)
        await n1.notify_plus_verified("j1")

        plus, expired, mid = await manager.read_batch_tally("-1001")
        assert (plus, expired, mid) == (1, 0, 500)
        assert client1.send_message.await_count == 1
        assert "#1" in client1.send_message_with_reply_to.await_args.kwargs["text"]

        # Simulated restart: fresh notifier, same JobManager/repo.
        client2 = _make_client(
            send_message=AsyncMock(return_value={"message_id": 999})
        )
        n2 = _make_notifier(manager, client2)
        await n2.notify_plus_verified("j2")

        assert "#2" in client2.send_message_with_reply_to.await_args.kwargs["text"]
        # Edits existing tally message — no new send for tally.
        client2.edit_message_text.assert_awaited()
        edit_kwargs = client2.edit_message_text.await_args.kwargs
        assert edit_kwargs["message_id"] == 500
        assert "✅ 2" in edit_kwargs["text"]
        client2.send_message.assert_not_awaited()

        plus2, expired2, mid2 = await manager.read_batch_tally("-1001")
        assert (plus2, expired2, mid2) == (2, 0, 500)
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_reset_decrements_receipted_skips_failed_defers_rearm(
    sqlite_path: Path,
) -> None:
    """Receipt success → decrement by snap; failed chat un-closed; no rearm."""
    engine = DbEngine(sqlite_path)
    await engine.init_schema()
    repo = JobRepository(engine)
    try:
        manager = _make_manager(repo)
        await _seed_job(
            repo, manager, "a1", order_num=1, chat_id="chat-A", message_id=1
        )
        await _seed_job(
            repo, manager, "a2", order_num=2, chat_id="chat-A", message_id=2
        )
        await _seed_job(
            repo, manager, "b1", order_num=3, chat_id="chat-B", message_id=3
        )
        await _seed_job(
            repo, manager, "b2", order_num=4, chat_id="chat-B", message_id=4
        )

        # Seed tallies via claim (plus on A, plus+expired on B).
        claimed, counts = await manager.claim_and_count_plan_outcome(
            "chat-A", "a1", plus_delta=1
        )
        assert claimed and counts == (1, 0)
        claimed, counts = await manager.claim_and_count_plan_outcome(
            "chat-A", "a2", plus_delta=1
        )
        assert claimed and counts == (2, 0)
        claimed, counts = await manager.claim_and_count_plan_outcome(
            "chat-B", "b1", plus_delta=1
        )
        assert claimed and counts == (1, 0)
        claimed, counts = await manager.claim_and_count_plan_outcome(
            "chat-B", "b2", expired_delta=1
        )
        assert claimed and counts == (1, 1)
        await manager.persist_tally_message_id("chat-A", 100, durable=True)
        await manager.persist_tally_message_id("chat-B", 200, durable=True)

        # Defer counted jobs on both chats — partial skip must not rearm
        # either, so a deferred job on the skipped chat cannot recount.
        await repo.defer_plan_outcome_rearm("a1")
        await repo.defer_plan_outcome_rearm("b1")
        connection = await engine.get_connection()
        for jid in ("a1", "b1"):
            cursor = await connection.execute(
                "SELECT plan_outcome_notified FROM jobs WHERE job_id = ?",
                (jid,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            assert row is not None and int(row[0]) == 2

        # chat-B receipt send fails; chat-A succeeds. Also inject a late
        # Plus on chat-A between list snapshot and close by claiming after
        # the list is taken — we simulate by claiming before reset but
        # after we've recorded the snap amounts that close will use...
        # Real path: list snapshots row, then send, then close by snap.
        # Seed one more job and claim it DURING send of chat-A receipt.
        await _seed_job(
            repo, manager, "a-late", order_num=5, chat_id="chat-A", message_id=5
        )

        async def _send_side_effect(*, token, chat_id, text, **kwargs):  # noqa: ARG001
            if chat_id == "chat-B":
                raise TelegramApiError(400, 400, "chat B boom")
            if chat_id == "chat-A" and "Chốt kỳ" in text:
                # Late Plus lands after snapshot, before close.
                await manager.claim_and_count_plan_outcome(
                    "chat-A", "a-late", plus_delta=1
                )
            return {"message_id": 900}

        client = _make_client(send_message=AsyncMock(side_effect=_send_side_effect))
        n = _make_notifier(manager, client)
        summary = await n.reset_batch_tally()

        assert "chat-A" in summary["closed"]
        assert "chat-B" in summary["skipped"]
        assert summary["plus_total"] == 2  # receipted snap for A only
        assert summary["expired_total"] == 0

        plus_a, expired_a, mid_a = await manager.read_batch_tally("chat-A")
        # Receipted 2 plus; late +1 survives → not zero.
        assert (plus_a, expired_a) == (1, 0)
        assert mid_a is None

        plus_b, expired_b, mid_b = await manager.read_batch_tally("chat-B")
        # Failed receipt → left un-closed.
        assert (plus_b, expired_b, mid_b) == (1, 1, 200)

        # Partial skip → no global rearm (deferred stays 2 on both chats).
        for jid in ("a1", "b1"):
            cursor = await connection.execute(
                "SELECT plan_outcome_notified FROM jobs WHERE job_id = ?",
                (jid,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            assert row is not None and int(row[0]) == 2

        # Skipped-chat deferred job still unclaimable this period.
        claimed_b, _ = await manager.claim_and_count_plan_outcome(
            "chat-B", "b1", plus_delta=1
        )
        assert claimed_b is False

        # Next Plus on chat-A is #1 under a new tally message (count was 1
        # residual late Plus, so next claim yields #2). Seed fresh job.
        await _seed_job(
            repo, manager, "a-next", order_num=6, chat_id="chat-A", message_id=6
        )
        client2 = _make_client(
            send_message=AsyncMock(return_value={"message_id": 777})
        )
        n2 = _make_notifier(manager, client2)
        await n2.notify_plus_verified("a-next")
        tag = client2.send_message_with_reply_to.await_args.kwargs["text"]
        # Residual late Plus (1) + this one → #2; new tally mid=777.
        assert "#2" in tag
        plus_next, _, mid_next = await manager.read_batch_tally("chat-A")
        assert plus_next == 2
        assert mid_next == 777
        client2.send_message.assert_awaited()
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_full_reset_success_rearms_deferred(
    sqlite_path: Path,
) -> None:
    """When every non-empty chat closes, deferred jobs rearm for next period."""
    engine = DbEngine(sqlite_path)
    await engine.init_schema()
    repo = JobRepository(engine)
    try:
        manager = _make_manager(repo)
        await _seed_job(
            repo, manager, "a1", order_num=1, chat_id="chat-A", message_id=1
        )
        await _seed_job(
            repo, manager, "b1", order_num=2, chat_id="chat-B", message_id=2
        )
        assert (
            await manager.claim_and_count_plan_outcome(
                "chat-A", "a1", plus_delta=1
            )
        )[0]
        assert (
            await manager.claim_and_count_plan_outcome(
                "chat-B", "b1", plus_delta=1
            )
        )[0]
        await repo.defer_plan_outcome_rearm("a1")
        await repo.defer_plan_outcome_rearm("b1")

        client = _make_client(
            send_message=AsyncMock(return_value={"message_id": 1})
        )
        n = _make_notifier(manager, client)
        summary = await n.reset_batch_tally()
        assert set(summary["closed"]) == {"chat-A", "chat-B"}
        assert summary["skipped"] == []

        connection = await engine.get_connection()
        for jid in ("a1", "b1"):
            cursor = await connection.execute(
                "SELECT plan_outcome_notified FROM jobs WHERE job_id = ?",
                (jid,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            assert row is not None and int(row[0]) == 0
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_close_fail_after_receipt_retries_then_skips(
    sqlite_path: Path,
) -> None:
    """Receipt sent but DB close fails twice → chat skipped, counters intact."""
    engine = DbEngine(sqlite_path)
    await engine.init_schema()
    repo = JobRepository(engine)
    try:
        manager = _make_manager(repo)
        await _seed_job(
            repo, manager, "a1", order_num=1, chat_id="chat-A", message_id=1
        )
        assert (
            await manager.claim_and_count_plan_outcome(
                "chat-A", "a1", plus_delta=1
            )
        )[0]
        await manager.persist_tally_message_id("chat-A", 50, durable=True)

        real_close = manager.close_batch_period
        calls = {"n": 0}

        async def _close_fail(*args, **kwargs):  # noqa: ANN002, ANN003
            calls["n"] += 1
            raise RuntimeError("db locked")

        manager.close_batch_period = _close_fail  # type: ignore[method-assign]
        client = _make_client(
            send_message=AsyncMock(return_value={"message_id": 9})
        )
        n = _make_notifier(manager, client)
        summary = await n.reset_batch_tally()

        assert summary["closed"] == []
        assert "chat-A" in summary["skipped"]
        assert calls["n"] == 2  # one retry after first fail
        plus, expired, mid = await manager.read_batch_tally("chat-A")
        assert (plus, expired, mid) == (1, 0, 50)

        # Recover and close successfully on retry path.
        manager.close_batch_period = real_close  # type: ignore[method-assign]
        summary2 = await n.reset_batch_tally()
        assert "chat-A" in summary2["closed"]
        plus2, expired2, mid2 = await manager.read_batch_tally("chat-A")
        assert (plus2, expired2, mid2) == (0, 0, None)
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_concurrent_outcomes_single_tally_message(
    sqlite_path: Path,
) -> None:
    """Concurrent Plus outcomes for one chat → one tally mid, counts sum."""
    engine = DbEngine(sqlite_path)
    await engine.init_schema()
    repo = JobRepository(engine)
    try:
        manager = _make_manager(repo)
        await _seed_job(repo, manager, "c1", order_num=1, message_id=1)
        await _seed_job(repo, manager, "c2", order_num=2, message_id=2)

        send_count = 0
        send_lock = asyncio.Lock()

        async def _send(*, token, chat_id, text, **kwargs):  # noqa: ARG001
            nonlocal send_count
            async with send_lock:
                send_count += 1
                mid = 1000 + send_count
            # Yield so concurrent path can race without the lock.
            await asyncio.sleep(0.01)
            return {"message_id": mid}

        client = _make_client(send_message=AsyncMock(side_effect=_send))
        n = _make_notifier(manager, client)

        await asyncio.gather(
            n.notify_plus_verified("c1"),
            n.notify_plus_verified("c2"),
        )

        plus, expired, mid = await manager.read_batch_tally("-1001")
        assert (plus, expired) == (2, 0)
        assert mid is not None
        # Per-chat lock: only one first-send; the other edits.
        assert client.send_message.await_count == 1
        assert client.edit_message_text.await_count == 1
        assert client.edit_message_text.await_args.kwargs["message_id"] == mid
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_edit_deleted_tally_resends_and_persists_new_mid(
    sqlite_path: Path,
) -> None:
    """TelegramApiError on edit → resend + durable persist of new mid."""
    engine = DbEngine(sqlite_path)
    await engine.init_schema()
    repo = JobRepository(engine)
    try:
        manager = _make_manager(repo)
        await _seed_job(repo, manager, "d1", order_num=1, message_id=1)
        await _seed_job(repo, manager, "d2", order_num=2, message_id=2)

        client = _make_client(
            send_message=AsyncMock(return_value={"message_id": 111})
        )
        n = _make_notifier(manager, client)
        await n.notify_plus_verified("d1")
        assert (await manager.read_batch_tally("-1001"))[2] == 111

        client.edit_message_text = AsyncMock(
            side_effect=TelegramApiError(400, 400, "message to edit not found")
        )
        client.send_message = AsyncMock(return_value={"message_id": 222})
        await n.notify_plus_verified("d2")

        plus, _, mid = await manager.read_batch_tally("-1001")
        assert plus == 2
        assert mid == 222
    finally:
        await engine.close()
