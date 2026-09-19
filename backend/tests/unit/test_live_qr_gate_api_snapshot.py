"""API snapshot for Live QR Gate."""
from __future__ import annotations

from typing import Any

import pytest

from app.api import routes_notifications
from app.notifiers.telegram.live_qr_gate import (
    KEY_LIVE_ENABLED,
    KEY_LIVE_MAX_PER_CHAT,
    LiveQrGate,
)
from app.notifiers.telegram.notifier import TelegramNotifier


class _FakeSettings:
    def __init__(self) -> None:
        self._snap: dict[str, Any] = {
            KEY_LIVE_ENABLED: True,
            KEY_LIVE_MAX_PER_CHAT: 5,
            "telegram.push_mode.chat_targets": [
                {"chat_id": "-1001", "label": "A", "enabled": True},
                {"chat_id": "-1002", "label": "B", "enabled": True},
            ],
            "telegram.push_mode.enabled": True,
            "telegram.bot_token": "1:x",
            "telegram.push_mode.send_photo": False,
        }

    async def get(self, key: str) -> Any:
        return self._snap.get(key)


class _FakeSse:
    def __init__(self) -> None:
        self.live_events: list[dict[str, Any]] = []

    async def broadcast_live_qr_gate_updated(self, snapshot: dict[str, Any]) -> None:
        self.live_events.append(snapshot)


class _FakeClient:
    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_get_live_qr_gate_snapshot() -> None:
    from app.notifiers.telegram.distributor import RoundRobinDistributor

    settings = _FakeSettings()
    sse = _FakeSse()
    gate = LiveQrGate(settings=settings, sse=sse)  # type: ignore[arg-type]
    notifier = TelegramNotifier(
        settings=settings,  # type: ignore[arg-type]
        client=_FakeClient(),  # type: ignore[arg-type]
        distributor=RoundRobinDistributor(),
        live_qr_gate=gate,
    )
    routes_notifications.configure_telegram_notifier(notifier)
    await gate.refresh_config()
    await gate.acquire("j1", "-1001", 1)
    snap = await routes_notifications.get_live_qr_gate()
    assert snap["enabled"] is True
    assert snap["max_per_chat"] == 5
    assert snap["capacity"] == 10
    assert snap["live"] == 1
    assert snap["blocked"] is False
    assert snap["per_chat"]["-1001"] == 1
