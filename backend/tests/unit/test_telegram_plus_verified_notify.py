"""Plus-verified notify: tag reply + caption + batch tally (Push_Mode).

Port pattern từ gpt_signup_hybrid: khi plan → plus, worker group thấy
QR nào thành công (#N) + tally Batch Plus.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core.job_manager import JobManager, _JobRecord
from app.core.payment_flow import Job, JobStatus, SimpleCancellationToken
from app.notifiers.telegram.formatter import (
    append_plus_status,
    build_batch_tally_text,
    build_period_close_text,
    expired_tag_text,
    plus_tag_text,
)
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


def _mk_record(
    job_id: str = "j1",
    *,
    chat_id: str | None = "-1001",
    message_id: int | None = 55,
    plan: str | None = None,
) -> _JobRecord:
    job = Job(
        job_id=job_id,
        payment_method="ideal",
        account_line="ab@x.com|pw",
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


class _TallyFakeJM:
    """Stateful in-memory stand-in for JobManager batch-tally APIs.

    Implements claim-and-count, read/persist tally, close/list/rearm, and
    optional notification tracking so unit tests do not need SQLite.
    """

    def __init__(
        self,
        jobs: dict[str, _JobRecord] | None = None,
        *,
        track_notifications: bool = False,
    ) -> None:
        self._jobs: dict[str, _JobRecord] = jobs or {}
        self._claimed: set[str] = set()
        self._plus: dict[str, int] = {}
        self._expired: dict[str, int] = {}
        self._tally_mid: dict[str, int | None] = {}
        self._deferred: set[str] = set()
        self.tracked: list[dict[str, Any]] = []
        self._track_notifications = track_notifications
        self.rearm_calls = 0
        self.close_calls: list[tuple[str, int, int]] = []

    def get_job(self, job_id: str) -> _JobRecord | None:
        return self._jobs.get(job_id)

    async def claim_and_count_plan_outcome(
        self,
        chat_id: str,
        job_id: str,
        *,
        plus_delta: int = 0,
        expired_delta: int = 0,
    ) -> tuple[bool, tuple[int, int] | None]:
        if job_id in self._claimed or job_id in self._deferred:
            return (False, None)
        self._claimed.add(job_id)
        self._plus[chat_id] = self._plus.get(chat_id, 0) + int(plus_delta)
        self._expired[chat_id] = self._expired.get(chat_id, 0) + int(
            expired_delta
        )
        return (True, (self._plus[chat_id], self._expired[chat_id]))

    async def read_batch_tally(
        self, chat_id: str
    ) -> tuple[int, int, int | None]:
        return (
            self._plus.get(chat_id, 0),
            self._expired.get(chat_id, 0),
            self._tally_mid.get(chat_id),
        )

    async def persist_tally_message_id(
        self,
        chat_id: str,
        message_id: int,
        *,
        durable: bool = False,  # noqa: ARG002
    ) -> None:
        self._tally_mid[chat_id] = int(message_id)

    async def list_batch_tally_chats(self) -> list[Any]:
        from app.core.job_repo import BatchTallyRow

        chat_ids = set(self._plus) | set(self._expired) | set(self._tally_mid)
        rows: list[BatchTallyRow] = []
        for cid in sorted(chat_ids):
            rows.append(
                BatchTallyRow(
                    chat_id=cid,
                    plus_count=self._plus.get(cid, 0),
                    expired_count=self._expired.get(cid, 0),
                    tally_message_id=self._tally_mid.get(cid),
                    updated_at="2026-01-01T00:00:00.000Z",
                )
            )
        return rows

    async def close_batch_period(
        self,
        chat_id: str,
        *,
        plus_receipted: int,
        expired_receipted: int,
    ) -> None:
        self.close_calls.append((chat_id, plus_receipted, expired_receipted))
        self._plus[chat_id] = self._plus.get(chat_id, 0) - int(plus_receipted)
        self._expired[chat_id] = self._expired.get(chat_id, 0) - int(
            expired_receipted
        )
        self._tally_mid[chat_id] = None

    async def rearm_deferred_plan_outcomes(self) -> None:
        self.rearm_calls += 1
        for job_id in list(self._deferred):
            self._deferred.discard(job_id)
            self._claimed.discard(job_id)

    def defer_job(self, job_id: str) -> None:
        """Mark a counted job deferred (simulates force_rerun)."""
        if job_id in self._claimed:
            self._deferred.add(job_id)

    async def record_telegram_notification(
        self, job_id: str, entry: dict[str, Any]
    ) -> None:  # noqa: ARG002
        if self._track_notifications:
            self.tracked.append(entry)


def test_plus_tag_text_and_tally_format() -> None:
    assert "#3" in plus_tag_text(plus_n=3)
    assert "Đã lên Plus" in plus_tag_text(plus_n=3)
    assert "#" not in plus_tag_text()
    assert "Batch Plus" in build_batch_tally_text(2)
    assert "✅ 2" in build_batch_tally_text(2)
    assert "⌛ 1" in build_batch_tally_text(2, expired=1)
    assert "chưa lên Plus" in expired_tag_text()
    cap = append_plus_status("✅ <b>iDEAL QR ready</b>", plus_n=4)
    assert "Đã lên Plus (#4)" in cap
    close = build_period_close_text(3, expired=1, reset_at="12:00:00 13/07/2026")
    assert "Chốt kỳ" in close
    assert "✅ 3" in close
    assert "⌛ 1" in close
    assert "12:00:00 13/07/2026" in close


@pytest.mark.asyncio
async def test_notify_plus_verified_tags_and_tallies() -> None:
    client = AsyncMock()
    client.send_message_with_reply_to = AsyncMock(return_value={"message_id": 9})
    client.edit_message_caption = AsyncMock(return_value={})
    client.send_message = AsyncMock(return_value={"message_id": 100})
    client.edit_message_text = AsyncMock(return_value={})

    jm = _TallyFakeJM({"j1": _mk_record("j1")})

    from app.notifiers.telegram.distributor import RoundRobinDistributor

    n = TelegramNotifier(
        settings=_FakeSettings(),  # type: ignore[arg-type]
        client=client,
        distributor=RoundRobinDistributor(),
        job_manager=jm,  # type: ignore[arg-type]
    )
    n._sent_captions["j1"] = (
        "✅ <b>iDEAL QR ready</b>\n👤 Email: <code>ab***@x.com</code>"
    )

    await n.notify_plus_verified("j1")
    await n.notify_plus_verified("j1")  # dedupe via claim

    client.send_message_with_reply_to.assert_awaited_once()
    args = client.send_message_with_reply_to.await_args
    assert args.kwargs["chat_id"] == "-1001"
    assert args.kwargs["reply_to_message_id"] == 55
    assert "#1" in args.kwargs["text"]

    client.edit_message_caption.assert_awaited_once()
    cap = client.edit_message_caption.await_args.kwargs["caption"]
    assert "Đã lên Plus (#1)" in cap

    client.send_message.assert_awaited_once()
    tally = client.send_message.await_args.kwargs["text"]
    assert "Batch Plus" in tally and "✅ 1" in tally

    plus, expired, mid = await jm.read_batch_tally("-1001")
    assert (plus, expired, mid) == (1, 0, 100)


@pytest.mark.asyncio
async def test_notify_plus_verified_increments_n() -> None:
    client = AsyncMock()
    client.send_message_with_reply_to = AsyncMock(return_value={})
    client.edit_message_caption = AsyncMock(return_value={})
    client.send_message = AsyncMock(return_value={"message_id": 1})
    client.edit_message_text = AsyncMock(return_value={})

    jm = _TallyFakeJM(
        {
            "a": _mk_record("a", message_id=1),
            "b": _mk_record("b", message_id=2),
        }
    )

    from app.notifiers.telegram.distributor import RoundRobinDistributor

    n = TelegramNotifier(
        settings=_FakeSettings(),  # type: ignore[arg-type]
        client=client,
        distributor=RoundRobinDistributor(),
        job_manager=jm,  # type: ignore[arg-type]
    )
    await n.notify_plus_verified("a")
    await n.notify_plus_verified("b")
    texts = [
        c.kwargs["text"] for c in client.send_message_with_reply_to.await_args_list
    ]
    assert "#1" in texts[0] and "#2" in texts[1]
    plus, expired, _ = await jm.read_batch_tally("-1001")
    assert (plus, expired) == (2, 0)


@pytest.mark.asyncio
async def test_check_plan_invokes_plus_hook() -> None:
    """JobManager.check_plan_status → plan=plus → plus-verified hook."""
    called: list[str] = []

    class _Handler:
        def get_max_concurrent_key(self) -> str:
            return "ideal.max_concurrent"

        def parse_account_line(self, line: str):
            from app.core.payment_flow import ParsedAccount

            return ParsedAccount(raw_line=line)

        async def run(self, job, proxy_lease):
            from app.core.payment_flow import JobResult

            return JobResult(status=JobStatus.QR_READY)

        async def check_plan_status(self, job):
            return {"plan": "plus", "email": "a@b.c"}

    class _Proxy:
        def __init__(self) -> None:
            from app.core.proxy_health import ProbeConfig

            self.probe_config = ProbeConfig(
                enabled=False, fallback_direct_on_exhausted=False
            )

        async def acquire(self, job_id: str, **kwargs):
            return None

        def release(self, lease) -> None:
            return None

    mgr = JobManager(
        settings=_FakeSettings({"ideal.max_concurrent": 2}),  # type: ignore[arg-type]
        proxy_pool=_Proxy(),  # type: ignore[arg-type]
        sse=_FakeSse(),  # type: ignore[arg-type]
    )
    mgr.register_handler("ideal", _Handler())  # type: ignore[arg-type]

    async def _hook(jid: str) -> None:
        called.append(jid)

    mgr.register_plus_verified_hook(_hook)

    # Seed 1 job QR_READY in memory.
    job = Job(
        job_id="x1",
        payment_method="ideal",
        account_line="a@b.c|pw",
        created_at=1.0,
        cancellation_token=SimpleCancellationToken(),
    )
    rec = _JobRecord(job=job, status=JobStatus.QR_READY)
    mgr._jobs["x1"] = rec

    result = await mgr.check_plan_status("x1")
    assert result is not None and result.get("plan") == "plus"
    assert called == ["x1"]
    # Second check same plan — no re-fire.
    await mgr.check_plan_status("x1")
    assert called == ["x1"]


@pytest.mark.asyncio
async def test_browser_signal_invokes_plus_hook_once() -> None:
    called: list[str] = []

    class _Proxy:
        def __init__(self) -> None:
            from app.core.proxy_health import ProbeConfig

            self.probe_config = ProbeConfig(
                enabled=False, fallback_direct_on_exhausted=False
            )

        async def acquire(self, job_id: str, **kwargs):
            return None

        def release(self, lease) -> None:
            return None

    mgr = JobManager(
        settings=_FakeSettings({"ideal.max_concurrent": 2}),  # type: ignore[arg-type]
        proxy_pool=_Proxy(),  # type: ignore[arg-type]
        sse=_FakeSse(),  # type: ignore[arg-type]
    )

    async def _hook(jid: str) -> None:
        called.append(jid)

    mgr.register_plus_verified_hook(_hook)
    job = Job(
        job_id="x2",
        payment_method="gcash_direct",
        account_line="a@b.c|pw",
        created_at=1.0,
        cancellation_token=SimpleCancellationToken(),
    )
    mgr._jobs["x2"] = _JobRecord(job=job, status=JobStatus.QR_READY)

    first = await mgr.mark_plus_verified_from_signal(
        "x2",
        source="gcash_browser",
        detail="https://chatgpt.com/payments/success",
    )
    second = await mgr.mark_plus_verified_from_signal(
        "x2",
        source="gcash_browser",
        detail="https://chatgpt.com/#plus_onboarding",
    )

    assert first is True
    assert second is False
    assert mgr._jobs["x2"].plan == "plus"
    assert called == ["x2"]


@pytest.mark.asyncio
async def test_notify_timeout_expired_tags() -> None:
    client = AsyncMock()
    client.send_message_with_reply_to = AsyncMock(return_value={})
    client.edit_message_caption = AsyncMock(return_value={})
    client.send_message = AsyncMock(return_value={"message_id": 50})
    client.edit_message_text = AsyncMock(return_value={})

    jm = _TallyFakeJM({"j1": _mk_record("j1", plan="free")})

    from app.notifiers.telegram.distributor import RoundRobinDistributor

    n = TelegramNotifier(
        settings=_FakeSettings(),  # type: ignore[arg-type]
        client=client,
        distributor=RoundRobinDistributor(),
        job_manager=jm,  # type: ignore[arg-type]
    )
    await n.notify_plan_check_timeout("j1")
    text = client.send_message_with_reply_to.await_args.kwargs["text"]
    assert "chưa lên Plus" in text
    plus, expired, mid = await jm.read_batch_tally("-1001")
    assert (plus, expired, mid) == (0, 1, 50)
    tally = client.send_message.await_args.kwargs["text"]
    assert "⌛ 1" in tally


@pytest.mark.asyncio
async def test_send_tracks_message_id() -> None:
    """Worker send success → track entry includes message_id."""
    client = AsyncMock()
    client.send_photo = AsyncMock(return_value={"message_id": 777})

    jm = _TallyFakeJM(track_notifications=True)

    from app.notifiers.telegram.distributor import RoundRobinDistributor
    from app.notifiers.telegram.notifier import _PendingSend

    n = TelegramNotifier(
        settings=_FakeSettings(),  # type: ignore[arg-type]
        client=client,
        distributor=RoundRobinDistributor(),
        job_manager=jm,  # type: ignore[arg-type]
    )
    item = _PendingSend(
        send_key="j9:-1001",
        job_id="j9",
        chat_id="-1001",
        chat_label="W1",
        bot_token="1:ABC",
        photo_bytes=b"png",
        caption="cap",
        account_line_masked="ab***@x.com",
        send_photo=True,
        account_line="ab@x.com|pass",
        finished_at=100.0,
        payment_link=None,
        payment_method="upi",
        order=1,
        qr_expires_at=None,
        sent_at=100.0,
    )
    await n._send_with_retry(item)
    assert jm.tracked and jm.tracked[0]["message_id"] == 777
    assert jm.tracked[0]["success"] is True
    assert n._sent_captions["j9"] == "cap"
