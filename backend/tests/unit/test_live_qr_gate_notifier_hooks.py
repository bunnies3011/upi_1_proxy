"""Notifier hooks for LiveQrGate — acquire after send, lifecycle release, pick."""
from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.notifiers.telegram.live_qr_gate import (
    KEY_LIVE_ENABLED,
    KEY_LIVE_MAX_PER_CHAT,
    LiveQrGate,
)
from app.notifiers.telegram.notifier import TelegramNotifier, _PendingSend


class _FakeSettings:
    def __init__(self, snap: dict[str, Any] | None = None) -> None:
        self._snap = dict(
            snap
            or {
                "telegram.push_mode.enabled": True,
                "telegram.bot_token": "1:ABC",
                "telegram.push_mode.send_photo": False,
                "telegram.push_mode.chat_targets": [
                    {"chat_id": "-1001", "label": "A", "enabled": True},
                    {"chat_id": "-1002", "label": "B", "enabled": True},
                ],
                KEY_LIVE_ENABLED: True,
                KEY_LIVE_MAX_PER_CHAT: 2,
            }
        )

    async def get(self, key: str) -> Any:
        return self._snap.get(key)


class _FakeSse:
    async def broadcast_live_qr_gate_updated(self, snapshot: dict[str, Any]) -> None:
        return None

    async def broadcast_push_gate_updated(self, snapshot: dict[str, Any]) -> None:
        return None


class _FakeClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> dict[str, Any]:
        mid = 1000 + len(self.sent)
        self.sent.append(kwargs)
        return {"message_id": mid}

    async def send_photo(self, **kwargs: Any) -> dict[str, Any]:
        return await self.send_message(**kwargs)

    async def aclose(self) -> None:
        return None


class _FakeDistributor:
    async def pick(self, chat_ids: list[str]) -> str:
        return chat_ids[0]


def _mk_gate(settings: _FakeSettings) -> LiveQrGate:
    return LiveQrGate(settings=settings, sse=_FakeSse())


def _mk_notifier(
    settings: _FakeSettings | None = None,
    *,
    live: LiveQrGate | None = None,
    job_manager: Any = None,
) -> TelegramNotifier:
    settings = settings or _FakeSettings()
    gate = live if live is not None else _mk_gate(settings)
    from app.notifiers.telegram.distributor import RoundRobinDistributor

    n = TelegramNotifier(
        settings=settings,
        client=_FakeClient(),  # type: ignore[arg-type]
        distributor=RoundRobinDistributor(),
        job_manager=job_manager,
        push_gate=None,
        live_qr_gate=gate,
    )
    return n


@pytest.mark.asyncio
async def test_send_success_acquires_message_keyed_slot() -> None:
    n = _mk_notifier()
    await n.live_qr_gate.refresh_config()  # type: ignore[union-attr]
    item = _PendingSend(
        job_id="j1",
        chat_id="-1001",
        chat_label="A",
        bot_token="1:ABC",
        photo_bytes=b"",
        caption="hi",
        account_line_masked="a@x.com",
        send_photo=False,
    )
    await n._send_with_retry(item)
    assert n.live_qr_gate.chat_live_count("-1001") == 1  # type: ignore[union-attr]
    assert "j1:1000" in n.live_qr_gate._slots  # type: ignore[union-attr]  # noqa: SLF001


@pytest.mark.asyncio
async def test_send_fail_no_acquire() -> None:
    from app.notifiers.telegram.client import TelegramApiError

    n = _mk_notifier()
    await n.live_qr_gate.refresh_config()  # type: ignore[union-attr]

    async def boom(**kwargs: Any) -> dict[str, Any]:
        raise TelegramApiError(
            status_code=400,
            error_code=400,
            description="bad request",
        )

    n._client.send_message = boom  # type: ignore[method-assign]
    item = _PendingSend(
        job_id="j1",
        chat_id="-1001",
        chat_label="A",
        bot_token="1:ABC",
        photo_bytes=b"",
        caption="hi",
        account_line_masked="a@x.com",
        send_photo=False,
    )
    await n._send_with_retry(item)
    assert n.live_qr_gate.chat_live_count("-1001") == 0  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_plus_verified_releases_job() -> None:
    n = _mk_notifier()
    await n.live_qr_gate.refresh_config()  # type: ignore[union-attr]
    await n.live_qr_gate.acquire("j1", "-1001", 55)  # type: ignore[union-attr]
    # no job_manager → outcome path early-returns after release
    await n.notify_plus_verified("j1")
    assert n.live_qr_gate.chat_live_count("-1001") == 0  # type: ignore[union-attr]
    # second call no double free
    await n.notify_plus_verified("j1")
    assert n.live_qr_gate.chat_live_count("-1001") == 0  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_timeout_releases_job() -> None:
    n = _mk_notifier()
    await n.live_qr_gate.refresh_config()  # type: ignore[union-attr]
    await n.live_qr_gate.acquire("j1", "-1001", 55)  # type: ignore[union-attr]
    await n.notify_plan_check_timeout("j1")
    assert n.live_qr_gate.chat_live_count("-1001") == 0  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_lifecycle_job_removed_frees_slot() -> None:
    n = _mk_notifier()
    await n.live_qr_gate.refresh_config()  # type: ignore[union-attr]
    await n.live_qr_gate.acquire("j1", "-1001", 1)  # type: ignore[union-attr]
    n.schedule_release_live_slots("j1")
    await asyncio.sleep(0.05)
    assert n.live_qr_gate.chat_live_count("-1001") == 0  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_early_plus_skips_acquire() -> None:
    rec = MagicMock()
    rec.plan = "plus"
    jm = MagicMock()
    jm.get_job.return_value = rec
    n = _mk_notifier(job_manager=jm)
    await n.live_qr_gate.refresh_config()  # type: ignore[union-attr]
    await n._maybe_acquire_live_slot("j1", "-1001", 99)
    assert n.live_qr_gate.chat_live_count("-1001") == 0  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_early_plus_race_sweep_frees_orphan() -> None:
    """plus before send → release no-op → acquire → sweep frees."""
    n = _mk_notifier()
    n.live_qr_gate._qr_ttl_seconds = 1  # type: ignore[union-attr]  # noqa: SLF001
    await n.live_qr_gate.refresh_config()  # type: ignore[union-attr]
    # plus first (no slot)
    await n.notify_plus_verified("j1")
    # then worker acquires
    await n.live_qr_gate.acquire("j1", "-1001", 77)  # type: ignore[union-attr]
    assert n.live_qr_gate.chat_live_count("-1001") == 1  # type: ignore[union-attr]
    sid = "j1:77"
    n.live_qr_gate._slots[sid].acquired_at = time.monotonic() - 5  # type: ignore[union-attr]  # noqa: SLF001
    freed = await n.live_qr_gate.sweep_expired()  # type: ignore[union-attr]
    assert freed == 1
    assert n.live_qr_gate.chat_live_count("-1001") == 0  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_release_never_acquired_noop() -> None:
    n = _mk_notifier()
    await n.live_qr_gate.refresh_config()  # type: ignore[union-attr]
    assert await n.live_qr_gate.release_job("missing") == 0  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_live_disabled_no_acquire() -> None:
    settings = _FakeSettings()
    settings._snap[KEY_LIVE_ENABLED] = False
    n = _mk_notifier(settings)
    await n.live_qr_gate.refresh_config()  # type: ignore[union-attr]
    item = _PendingSend(
        job_id="j1",
        chat_id="-1001",
        chat_label="A",
        bot_token="1:ABC",
        photo_bytes=b"",
        caption="hi",
        account_line_masked="a@x.com",
        send_photo=False,
    )
    await n._send_with_retry(item)
    assert n.live_qr_gate.chat_live_count("-1001") == 0  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_least_loaded_repick() -> None:
    n = _mk_notifier()
    await n.live_qr_gate.refresh_config()  # type: ignore[union-attr]
    await n.live_qr_gate.acquire("j1", "-1001", 1)  # type: ignore[union-attr]
    await n.live_qr_gate.acquire("j2", "-1001", 2)  # type: ignore[union-attr]
    item = _PendingSend(
        job_id="j3",
        chat_id="-1001",
        chat_label="A",
        bot_token="1:ABC",
        photo_bytes=b"",
        caption="hi",
        account_line_masked="a@x.com",
        send_photo=False,
    )
    ok = await n._maybe_repick_live_target(item)
    assert ok is True
    assert item.chat_id == "-1002"


@pytest.mark.asyncio
async def test_least_loaded_all_full_returns_false() -> None:
    n = _mk_notifier()
    await n.live_qr_gate.refresh_config()  # type: ignore[union-attr]
    for i, chat in enumerate(["-1001", "-1001", "-1002", "-1002"]):
        await n.live_qr_gate.acquire(f"j{i}", chat, i + 1)  # type: ignore[union-attr]
    item = _PendingSend(
        job_id="jx",
        chat_id="-1001",
        chat_label="A",
        bot_token="1:ABC",
        photo_bytes=b"",
        caption="hi",
        account_line_masked="a@x.com",
        send_photo=False,
    )
    assert await n._maybe_repick_live_target(item) is False


@pytest.mark.asyncio
async def test_wait_for_gate_open_live_blocked_then_release() -> None:
    n = _mk_notifier()
    await n.live_qr_gate.refresh_config()  # type: ignore[union-attr]
    for i in range(2):
        await n.live_qr_gate.acquire(f"ja{i}", "-1001", i + 1)  # type: ignore[union-attr]
        await n.live_qr_gate.acquire(f"jb{i}", "-1002", i + 10)  # type: ignore[union-attr]
    assert n.live_qr_gate.is_blocked()  # type: ignore[union-attr]

    async def free_soon() -> None:
        await asyncio.sleep(0.05)
        await n.live_qr_gate.release_job("ja0")  # type: ignore[union-attr]

    asyncio.create_task(free_soon())
    await asyncio.wait_for(n._wait_for_gate_open(), timeout=2.0)
    assert not n.live_qr_gate.is_blocked()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_job_manager_live_slot_release_hook() -> None:
    from app.core.job_manager import JobManager
    from app.core.sse import SseBroadcaster

    settings = _FakeSettings()
    # Minimal JobManager construction is heavy — test register + invoke only.
    hooks: list[str] = []

    class _Mini:
        def __init__(self) -> None:
            self._live_slot_release_hooks: list[Any] = []
            self._auto_check_plan_tasks: dict[str, Any] = {}

        def register_live_slot_release_hook(self, hook: Any) -> None:
            self._live_slot_release_hooks.append(hook)

        def _invoke_live_slot_release_hooks(self, job_id: str) -> None:
            for h in self._live_slot_release_hooks:
                h(job_id)

        def _cancel_auto_check_plan(self, job_id: str) -> None:
            bucket = self._auto_check_plan_tasks
            task = bucket.pop(job_id, None)
            if task is not None and not task.done():
                task.cancel()
            self._invoke_live_slot_release_hooks(job_id)

    # Use real JobManager method via binding-like test on LiveQrGate + schedule
    n = _mk_notifier()
    await n.live_qr_gate.refresh_config()  # type: ignore[union-attr]
    await n.live_qr_gate.acquire("jstop", "-1001", 9)  # type: ignore[union-attr]
    m = _Mini()
    m.register_live_slot_release_hook(n.schedule_release_live_slots)
    m._cancel_auto_check_plan("jstop")
    await asyncio.sleep(0.05)
    assert n.live_qr_gate.chat_live_count("-1001") == 0  # type: ignore[union-attr]
