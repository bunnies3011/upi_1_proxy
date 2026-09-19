"""JobManager composite pause checker + LiveQrGate edge pause/wake."""
from __future__ import annotations

from typing import Any

import pytest

from app.notifiers.telegram.live_qr_gate import (
    KEY_LIVE_ENABLED,
    KEY_LIVE_MAX_PER_CHAT,
    LiveQrGate,
)
from app.notifiers.telegram.push_gate import PushSuccessGate


class _FakeSettings:
    def __init__(self, snap: dict[str, Any] | None = None) -> None:
        self._snap = dict(
            snap
            or {
                KEY_LIVE_ENABLED: True,
                KEY_LIVE_MAX_PER_CHAT: 2,
                "telegram.push_mode.chat_targets": [
                    {"chat_id": "-1001", "label": "A", "enabled": True},
                ],
                "telegram.push_mode.success_wait.enabled": False,
                "telegram.push_mode.success_wait.threshold": 10,
            }
        )

    async def get(self, key: str) -> Any:
        return self._snap.get(key)


class _FakeSse:
    async def broadcast_live_qr_gate_updated(self, s: dict[str, Any]) -> None:
        return None

    async def broadcast_push_gate_updated(self, s: dict[str, Any]) -> None:
        return None


def _composite(push: PushSuccessGate | None, live: LiveQrGate | None) -> bool:
    paused = push is not None and push.is_paused()
    blocked = live is not None and live.is_blocked()
    return paused or blocked


@pytest.mark.asyncio
async def test_composite_live_blocked_true() -> None:
    live = LiveQrGate(settings=_FakeSettings(), sse=_FakeSse())
    await live.refresh_config()
    await live.acquire("j1", "-1001", 1)
    await live.acquire("j2", "-1001", 2)
    assert live.is_blocked() is True
    assert _composite(None, live) is True


@pytest.mark.asyncio
async def test_composite_success_wait_paused() -> None:
    push = PushSuccessGate(settings=_FakeSettings(), sse=_FakeSse())
    push._paused = True  # noqa: SLF001
    live = LiveQrGate(settings=_FakeSettings(), sse=_FakeSse())
    await live.refresh_config()
    assert _composite(push, live) is True


@pytest.mark.asyncio
async def test_composite_both_false() -> None:
    push = PushSuccessGate(settings=_FakeSettings(), sse=_FakeSse())
    live = LiveQrGate(settings=_FakeSettings(), sse=_FakeSse())
    await live.refresh_config()
    assert _composite(push, live) is False


@pytest.mark.asyncio
async def test_acquire_fill_edge_calls_pause_once() -> None:
    calls: list[int] = []
    live = LiveQrGate(settings=_FakeSettings(), sse=_FakeSse())
    live.set_pause_running_push_jobs_callback(lambda: calls.append(1))
    await live.refresh_config()
    await live.acquire("j1", "-1001", 1)
    assert calls == []
    await live.acquire("j2", "-1001", 2)
    assert calls == [1]
    # further acquire while full — no extra pause
    await live.acquire("j3", "-1001", 3)
    assert calls == [1]


@pytest.mark.asyncio
async def test_capacity_shrink_refresh_fires_pause_edge() -> None:
    calls: list[int] = []
    settings = _FakeSettings(
        {
            KEY_LIVE_ENABLED: True,
            KEY_LIVE_MAX_PER_CHAT: 5,
            "telegram.push_mode.chat_targets": [
                {"chat_id": "-1001", "label": "A", "enabled": True},
            ],
        }
    )
    live = LiveQrGate(settings=settings, sse=_FakeSse())
    live.set_pause_running_push_jobs_callback(lambda: calls.append(1))
    await live.refresh_config()
    await live.acquire("j1", "-1001", 1)
    await live.acquire("j2", "-1001", 2)
    assert live.is_blocked() is False
    assert calls == []
    settings._snap[KEY_LIVE_MAX_PER_CHAT] = 1
    edge = await live.refresh_config()
    assert edge is True
    assert live.is_blocked() is True
    assert calls == [1]


@pytest.mark.asyncio
async def test_release_wakes_scheduler() -> None:
    wakes: list[int] = []
    live = LiveQrGate(settings=_FakeSettings(), sse=_FakeSse())
    live.set_wake_scheduler(lambda: wakes.append(1))
    await live.refresh_config()
    await live.acquire("j1", "-1001", 1)
    await live.acquire("j2", "-1001", 2)
    assert live.is_blocked() is True
    await live.release("j1:1")
    assert live.is_blocked() is False
    assert wakes == [1]


@pytest.mark.asyncio
async def test_refresh_config_raise_max_wakes_scheduler() -> None:
    """C1: unblocking via settings (raise max) must wake scheduler — no Resume."""
    wakes: list[int] = []
    settings = _FakeSettings(
        {
            KEY_LIVE_ENABLED: True,
            KEY_LIVE_MAX_PER_CHAT: 1,
            "telegram.push_mode.chat_targets": [
                {"chat_id": "-1001", "label": "A", "enabled": True},
            ],
        }
    )
    live = LiveQrGate(settings=settings, sse=_FakeSse())
    live.set_wake_scheduler(lambda: wakes.append(1))
    await live.refresh_config()
    await live.acquire("j1", "-1001", 1)
    assert live.is_blocked() is True
    settings._snap[KEY_LIVE_MAX_PER_CHAT] = 5
    edge = await live.refresh_config()
    assert edge is False
    assert live.is_blocked() is False
    assert wakes == [1]


@pytest.mark.asyncio
async def test_is_blocked_never_raises_for_checker() -> None:
    live = LiveQrGate(settings=_FakeSettings({}), sse=_FakeSse())
    # empty cfg — still a safe cached bool
    assert live.is_blocked() is False
    # composite with fail-open style: if is_blocked raised, checker would
    # swallow — but is_blocked must never raise by contract
    assert _composite(None, live) is False


def test_handle_pause_requested_doc_resume_from_cache() -> None:
    """F10: paused job resumes from session cache, not full re-login.

    Documented on JobManager._handle_pause_requested docstring.
    """
    import inspect

    from app.core.job_manager import JobManager

    doc = inspect.getdoc(JobManager._handle_pause_requested) or ""
    assert "cache" in doc.lower()
    assert "login" in doc.lower()
