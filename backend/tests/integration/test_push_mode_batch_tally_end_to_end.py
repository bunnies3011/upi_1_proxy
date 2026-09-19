"""Push-mode batch tally end-to-end against real SQLite.

Boots real DbEngine / JobRepository / JobManager / TelegramNotifier.
Only the Telegram HTTP client is mocked. Covers:

- multi-chat Plus counts
- restart continues #N and edits the same tally message
- atomic claim-before-send (send failure keeps count, no double-count)
- period close decrements receipted amount (late Plus survives)
- failed receipt chat left un-closed / skipped
- rerun defers same-period recount; after reset can recount
- jobs.plan column never mutated by claim/count/reset
"""

from __future__ import annotations

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
from app.notifiers.telegram.notifier import TelegramNotifier

CHAT_A = "chat-A"
CHAT_B = "chat-B"

_PAYMENT_METHODS = ("ideal", "upi", "upi_nocdk")


class _FakeSettings:
    def __init__(self, snap: dict[str, Any] | None = None) -> None:
        self._snap = snap or {
            "telegram.push_mode.enabled": True,
            "telegram.bot_token": "1:ABC",
            "telegram.push_mode.send_photo": True,
            "telegram.push_mode.chat_targets": [
                {"chat_id": CHAT_A, "label": "WA", "enabled": True},
                {"chat_id": CHAT_B, "label": "WB", "enabled": True},
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


def _job_row(
    job_id: str,
    *,
    order_num: int = 1,
    payment_method: str = "ideal",
    plan: str | None = "free",
) -> JobRow:
    return JobRow(
        job_id=job_id,
        payment_method=payment_method,
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
        plan=plan,
    )


def _mk_record(
    job_id: str,
    *,
    payment_method: str = "ideal",
    chat_id: str = CHAT_A,
    message_id: int = 55,
    plan: str | None = "free",
) -> _JobRecord:
    job = Job(
        job_id=job_id,
        payment_method=payment_method,  # type: ignore[arg-type]
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
    payment_method: str = "ideal",
    chat_id: str = CHAT_A,
    message_id: int = 55,
    plan: str | None = "free",
) -> _JobRecord:
    await repo.upsert(
        _job_row(
            job_id,
            order_num=order_num,
            payment_method=payment_method,
            plan=plan,
        )
    )
    rec = _mk_record(
        job_id,
        payment_method=payment_method,
        chat_id=chat_id,
        message_id=message_id,
        plan=plan,
    )
    manager._jobs[job_id] = rec
    return rec


async def _read_plan(engine: DbEngine, job_id: str) -> str | None:
    connection = await engine.get_connection()
    cursor = await connection.execute(
        "SELECT plan FROM jobs WHERE job_id = ?",
        (job_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return None
    return row[0]


async def _read_flag(engine: DbEngine, job_id: str) -> int | None:
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


async def _assert_plans_untouched(
    engine: DbEngine,
    job_ids: list[str],
    expected: str | None = "free",
) -> None:
    for job_id in job_ids:
        assert await _read_plan(engine, job_id) == expected, (
            f"jobs.plan mutated for {job_id}"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("payment_method", _PAYMENT_METHODS)
async def test_batch_tally_full_path_across_payment_methods(
    sqlite_path: Path,
    payment_method: str,
) -> None:
    """Full outcome→tally→restart→reset path; mode-agnostic on plan/status."""
    engine = DbEngine(sqlite_path)
    await engine.init_schema()
    repo = JobRepository(engine)
    tracked_ids: list[str] = []
    try:
        manager = _make_manager(repo)

        # --- Seed: 3 Plus outcomes across 2 chats (A:2, B:1) ---
        await _seed_job(
            repo,
            manager,
            "a1",
            order_num=1,
            payment_method=payment_method,
            chat_id=CHAT_A,
            message_id=11,
        )
        await _seed_job(
            repo,
            manager,
            "a2",
            order_num=2,
            payment_method=payment_method,
            chat_id=CHAT_A,
            message_id=12,
        )
        await _seed_job(
            repo,
            manager,
            "b1",
            order_num=3,
            payment_method=payment_method,
            chat_id=CHAT_B,
            message_id=21,
        )
        tracked_ids.extend(["a1", "a2", "b1"])

        tally_mid_a = 500
        tally_mid_b = 600
        send_returns = {
            CHAT_A: {"message_id": tally_mid_a},
            CHAT_B: {"message_id": tally_mid_b},
        }

        async def _send_first(*, token, chat_id, text, **kwargs):  # noqa: ARG001
            return send_returns[chat_id]

        client1 = _make_client(
            send_message=AsyncMock(side_effect=_send_first)
        )
        n1 = _make_notifier(manager, client1)

        await n1.notify_plus_verified("a1")
        await n1.notify_plus_verified("a2")
        await n1.notify_plus_verified("b1")

        plus_a, expired_a, mid_a = await manager.read_batch_tally(CHAT_A)
        plus_b, expired_b, mid_b = await manager.read_batch_tally(CHAT_B)
        assert (plus_a, expired_a, mid_a) == (2, 0, tally_mid_a)
        assert (plus_b, expired_b, mid_b) == (1, 0, tally_mid_b)

        tag_texts = [
            c.kwargs["text"]
            for c in client1.send_message_with_reply_to.await_args_list
        ]
        assert any("#1" in t for t in tag_texts)
        assert any("#2" in t for t in tag_texts)
        # Chat B's sole Plus is #1 on that chat.
        b_tags = [
            c.kwargs["text"]
            for c in client1.send_message_with_reply_to.await_args_list
            if c.kwargs.get("chat_id") == CHAT_B
        ]
        assert len(b_tags) == 1 and "#1" in b_tags[0]

        await _assert_plans_untouched(engine, tracked_ids)

        # --- Atomic claim+count: send failure after claim keeps count ---
        await _seed_job(
            repo,
            manager,
            "a-fail-send",
            order_num=4,
            payment_method=payment_method,
            chat_id=CHAT_A,
            message_id=13,
        )
        tracked_ids.append("a-fail-send")
        client_fail = _make_client(
            send_message_with_reply_to=AsyncMock(
                side_effect=RuntimeError("tag send boom")
            )
        )
        n_fail = _make_notifier(manager, client_fail)
        await n_fail.notify_plus_verified("a-fail-send")

        plus_after_fail, _, mid_after_fail = await manager.read_batch_tally(
            CHAT_A
        )
        assert plus_after_fail == 3  # claimed+counted despite send failure
        assert mid_after_fail == tally_mid_a
        assert await _read_flag(engine, "a-fail-send") == 1

        # Retry same job: claim rejects, no second count / no tag send.
        client_retry = _make_client()
        n_retry = _make_notifier(manager, client_retry)
        await n_retry.notify_plus_verified("a-fail-send")
        plus_retry, _, _ = await manager.read_batch_tally(CHAT_A)
        assert plus_retry == 3
        client_retry.send_message_with_reply_to.assert_not_awaited()

        await _assert_plans_untouched(engine, tracked_ids)

        # --- Restart: fresh notifier over same DB continues #N + edits mid ---
        await _seed_job(
            repo,
            manager,
            "a3",
            order_num=5,
            payment_method=payment_method,
            chat_id=CHAT_A,
            message_id=14,
        )
        tracked_ids.append("a3")
        client2 = _make_client(
            send_message=AsyncMock(return_value={"message_id": 999})
        )
        n2 = _make_notifier(manager, client2)
        await n2.notify_plus_verified("a3")

        assert (
            "#4"
            in client2.send_message_with_reply_to.await_args.kwargs["text"]
        )
        client2.edit_message_text.assert_awaited()
        edit_kwargs = client2.edit_message_text.await_args.kwargs
        assert edit_kwargs["message_id"] == tally_mid_a
        assert "✅ 4" in edit_kwargs["text"]
        client2.send_message.assert_not_awaited()

        plus_a4, _, mid_a4 = await manager.read_batch_tally(CHAT_A)
        assert (plus_a4, mid_a4) == (4, tally_mid_a)

        await _assert_plans_untouched(engine, tracked_ids)

        # --- Rerun counted account: same-period no recount ---
        await repo.defer_plan_outcome_rearm("a1")
        assert await _read_flag(engine, "a1") == 2
        claimed_rerun, counts_rerun = await manager.claim_and_count_plan_outcome(
            CHAT_A, "a1", plus_delta=1
        )
        assert claimed_rerun is False
        assert counts_rerun is None
        plus_deferred, _, _ = await manager.read_batch_tally(CHAT_A)
        assert plus_deferred == 4  # no recount

        # --- Reset: late Plus on A survives; B receipt fails → skipped ---
        await _seed_job(
            repo,
            manager,
            "a-late",
            order_num=6,
            payment_method=payment_method,
            chat_id=CHAT_A,
            message_id=15,
        )
        tracked_ids.append("a-late")

        async def _reset_send(*, token, chat_id, text, **kwargs):  # noqa: ARG001
            if chat_id == CHAT_B:
                raise TelegramApiError(400, 400, "chat B boom")
            if chat_id == CHAT_A and "Chốt kỳ" in text:
                # Late Plus lands after list snapshot, before close.
                await manager.claim_and_count_plan_outcome(
                    CHAT_A, "a-late", plus_delta=1
                )
            return {"message_id": 900}

        client_reset = _make_client(
            send_message=AsyncMock(side_effect=_reset_send)
        )
        n_reset = _make_notifier(manager, client_reset)
        # Snapshot before reset: A=4, B=1. Late +1 during A's receipt.
        summary = await n_reset.reset_batch_tally()

        assert CHAT_A in summary["closed"]
        assert CHAT_B in summary["skipped"]
        assert summary["plus_total"] == 4  # receipted snap for A only
        assert summary["expired_total"] == 0

        plus_a_post, expired_a_post, mid_a_post = await manager.read_batch_tally(
            CHAT_A
        )
        # Receipted 4; late +1 survives.
        assert (plus_a_post, expired_a_post) == (1, 0)
        assert mid_a_post is None

        plus_b_post, expired_b_post, mid_b_post = await manager.read_batch_tally(
            CHAT_B
        )
        # Failed receipt → left un-closed with original mid.
        assert (plus_b_post, expired_b_post, mid_b_post) == (1, 0, tally_mid_b)

        # Partial skip must NOT rearm — deferred stays 2 (no same-period
        # recount on an un-closed chat's deferred jobs).
        assert await _read_flag(engine, "a1") == 2
        claimed_partial, _ = await manager.claim_and_count_plan_outcome(
            CHAT_A, "a1", plus_delta=1
        )
        assert claimed_partial is False

        await _assert_plans_untouched(engine, tracked_ids)

        # --- Retry close: B + residual A both receipt → rearm deferred ---
        client_b = _make_client(
            send_message=AsyncMock(return_value={"message_id": 901})
        )
        n_b = _make_notifier(manager, client_b)
        summary_b = await n_b.reset_batch_tally()
        assert CHAT_B in summary_b["closed"]
        assert CHAT_A in summary_b["closed"]  # residual late Plus receipted
        assert summary_b["skipped"] == []
        assert await _read_flag(engine, "a1") == 0
        plus_a_clean, _, mid_a_clean = await manager.read_batch_tally(CHAT_A)
        assert (plus_a_clean, mid_a_clean) == (0, None)

        # After rearm: previously-counted a1 can recount on a clean period.
        claimed_rearmed, counts_rearmed = (
            await manager.claim_and_count_plan_outcome(
                CHAT_A, "a1", plus_delta=1
            )
        )
        assert claimed_rearmed is True
        assert counts_rearmed == (1, 0)
        assert await _read_flag(engine, "a1") == 1

        # Clear the recount so the next Plus is #1 under a new tally mid.
        await manager.close_batch_period(
            CHAT_A, plus_receipted=1, expired_receipted=0
        )
        plus_clean, expired_clean, mid_clean = await manager.read_batch_tally(
            CHAT_A
        )
        assert (plus_clean, expired_clean, mid_clean) == (0, 0, None)

        await _seed_job(
            repo,
            manager,
            "a-next",
            order_num=7,
            payment_method=payment_method,
            chat_id=CHAT_A,
            message_id=16,
        )
        tracked_ids.append("a-next")
        client_next = _make_client(
            send_message=AsyncMock(return_value={"message_id": 777})
        )
        n_next = _make_notifier(manager, client_next)
        await n_next.notify_plus_verified("a-next")

        tag_next = client_next.send_message_with_reply_to.await_args.kwargs[
            "text"
        ]
        assert "#1" in tag_next
        plus_next, _, mid_next = await manager.read_batch_tally(CHAT_A)
        assert plus_next == 1
        assert mid_next == 777
        client_next.send_message.assert_awaited()

        await _assert_plans_untouched(engine, tracked_ids)
    finally:
        await engine.close()
