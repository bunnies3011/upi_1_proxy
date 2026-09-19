"""Unit tests for LiveQrGate (rolling-window live QR capacity gate).

Covers: message-keyed slots, per-chat cap, cached is_blocked, release_job,
sweep_expired, capacity display/dedupe, refresh_config edge, normalize_push_mode
mutex auto-clear, max_per_chat coerce.
"""
from __future__ import annotations

import time
from typing import Any

import pytest

from app.notifiers.telegram.live_qr_gate import (
    KEY_LIVE_ENABLED,
    KEY_LIVE_MAX_PER_CHAT,
    KEY_SUCCESS_WAIT_ENABLED,
    LiveQrGate,
    normalize_push_mode,
)


class _FakeSettings:
    def __init__(self, snap: dict[str, Any] | None = None) -> None:
        self._snap = dict(snap or {})

    async def get(self, key: str) -> Any:
        return self._snap.get(key)

    async def set(self, key: str, value: Any) -> None:
        self._snap[key] = value


class _FakeSse:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def broadcast_live_qr_gate_updated(self, snapshot: dict[str, Any]) -> None:
        self.events.append(snapshot)


def _gate(
    *,
    enabled: bool = True,
    max_per_chat: int = 2,
    chat_ids: list[str] | None = None,
    qr_ttl_seconds: int = 330,
) -> LiveQrGate:
    chats = chat_ids if chat_ids is not None else ["-1001", "-1002"]
    settings = _FakeSettings(
        {
            KEY_LIVE_ENABLED: enabled,
            KEY_LIVE_MAX_PER_CHAT: max_per_chat,
            "telegram.push_mode.chat_targets": [
                {"chat_id": cid, "label": cid, "enabled": True} for cid in chats
            ],
        }
    )
    g = LiveQrGate(settings=settings, sse=_FakeSse(), qr_ttl_seconds=qr_ttl_seconds)
    return g


@pytest.mark.asyncio
async def test_disabled_acquire_returns_none_not_blocked() -> None:
    g = _gate(enabled=False)
    await g.refresh_config()
    assert await g.acquire("j1", "-1001", 11) is None
    assert g.chat_live_count("-1001") == 0
    assert g.is_blocked() is False


@pytest.mark.asyncio
async def test_slot_key_same_job_two_messages_two_slots() -> None:
    g = _gate(max_per_chat=5)
    await g.refresh_config()
    s1 = await g.acquire("j1", "-1001", 11)
    s2 = await g.acquire("j1", "-1001", 12)
    assert s1 == "j1:11"
    assert s2 == "j1:12"
    assert s1 != s2
    assert g.chat_live_count("-1001") == 2


@pytest.mark.asyncio
async def test_per_chat_cap_is_chat_full() -> None:
    g = _gate(max_per_chat=2, chat_ids=["-1001", "-1002"])
    await g.refresh_config()
    await g.acquire("j1", "-1001", 1)
    await g.acquire("j2", "-1001", 2)
    assert g.is_chat_full("-1001") is True
    assert g.is_chat_full("-1002") is False
    # acquire still records (caller-side gates via is_chat_full)
    s3 = await g.acquire("j3", "-1001", 3)
    assert s3 == "j3:3"
    assert g.chat_live_count("-1001") == 3


@pytest.mark.asyncio
async def test_is_blocked_only_when_all_enabled_chats_full() -> None:
    g = _gate(max_per_chat=2, chat_ids=["-1001", "-1002"])
    await g.refresh_config()
    await g.acquire("j1", "-1001", 1)
    await g.acquire("j2", "-1001", 2)
    assert g.is_blocked() is False  # chat B still open
    await g.acquire("j3", "-1002", 3)
    await g.acquire("j4", "-1002", 4)
    assert g.is_blocked() is True


@pytest.mark.asyncio
async def test_zero_enabled_chats_gate_noop() -> None:
    g = _gate(enabled=True, max_per_chat=2, chat_ids=[])
    await g.refresh_config()
    assert g.is_blocked() is False
    snap = g.snapshot()
    assert snap["capacity"] == 0
    assert snap["blocked"] is False


@pytest.mark.asyncio
async def test_least_loaded_available_chat() -> None:
    g = _gate(max_per_chat=2, chat_ids=["-1001", "-1002"])
    await g.refresh_config()
    await g.acquire("j1", "-1001", 1)
    picked = g.least_loaded_available_chat(["-1001", "-1002"])
    assert picked == "-1002"
    await g.acquire("j2", "-1002", 2)
    await g.acquire("j3", "-1002", 3)
    # B full (2), A has 1 → A
    assert g.least_loaded_available_chat(["-1001", "-1002"]) == "-1001"
    await g.acquire("j4", "-1001", 4)
    assert g.least_loaded_available_chat(["-1001", "-1002"]) is None


@pytest.mark.asyncio
async def test_release_idempotent_no_permanent_set() -> None:
    g = _gate(max_per_chat=5)
    await g.refresh_config()
    slot = await g.acquire("j1", "-1001", 11)
    assert await g.release(slot) is True
    assert await g.release(slot) is False
    # reused job_id can acquire again after free
    slot2 = await g.acquire("j1", "-1001", 11)
    assert slot2 == "j1:11"
    assert g.chat_live_count("-1001") == 1


@pytest.mark.asyncio
async def test_release_job_frees_all_slots() -> None:
    g = _gate(max_per_chat=5)
    await g.refresh_config()
    await g.acquire("j1", "-1001", 1)
    await g.acquire("j1", "-1001", 2)
    await g.acquire("j2", "-1001", 3)
    n = await g.release_job("j1")
    assert n == 2
    assert g.chat_live_count("-1001") == 1
    assert await g.release_job("j1") == 0


@pytest.mark.asyncio
async def test_sweep_expired_frees_stale_keeps_fresh() -> None:
    g = _gate(max_per_chat=5, qr_ttl_seconds=10)
    await g.refresh_config()
    s1 = await g.acquire("j1", "-1001", 1)
    s2 = await g.acquire("j2", "-1001", 2)
    assert s1 and s2
    # age s1 beyond TTL
    g._slots[s1].acquired_at = time.monotonic() - 20  # noqa: SLF001
    freed = await g.sweep_expired()
    assert freed == 1
    assert g.chat_live_count("-1001") == 1
    assert s2 in g._slots  # noqa: SLF001


@pytest.mark.asyncio
async def test_capacity_display_dedupes_chat_ids() -> None:
    settings = _FakeSettings(
        {
            KEY_LIVE_ENABLED: True,
            KEY_LIVE_MAX_PER_CHAT: 5,
            "telegram.push_mode.chat_targets": [
                {"chat_id": "-1001", "label": "A", "enabled": True},
                {"chat_id": "-1001", "label": "A-dup", "enabled": True},
                {"chat_id": "-1002", "label": "B", "enabled": True},
                {"chat_id": "-1003", "label": "C", "enabled": False},
            ],
        }
    )
    g = LiveQrGate(settings=settings, sse=_FakeSse())
    await g.refresh_config()
    snap = g.snapshot()
    # unique enabled = 2 × max 5 = 10
    assert snap["capacity"] == 10
    assert set(snap["per_chat"].keys()) == {"-1001", "-1002"}


@pytest.mark.asyncio
async def test_refresh_config_edge_returns_true_on_block() -> None:
    g = _gate(max_per_chat=2, chat_ids=["-1001"])
    await g.refresh_config()
    await g.acquire("j1", "-1001", 1)
    await g.acquire("j2", "-1001", 2)
    assert g.is_blocked() is True

    # raise max → unblocked
    g._settings._snap[KEY_LIVE_MAX_PER_CHAT] = 5  # type: ignore[attr-defined]
    edge = await g.refresh_config()
    assert edge is False  # True only on False→True blocked edge
    assert g.is_blocked() is False

    # lower max back so both slots exceed → blocked edge
    g._settings._snap[KEY_LIVE_MAX_PER_CHAT] = 1  # type: ignore[attr-defined]
    edge2 = await g.refresh_config()
    assert edge2 is True
    assert g.is_blocked() is True


def test_normalize_push_mode_auto_clear_live_enables() -> None:
    out = normalize_push_mode(
        {KEY_LIVE_ENABLED: True},
        {KEY_LIVE_ENABLED: False, KEY_SUCCESS_WAIT_ENABLED: True},
    )
    assert out[KEY_LIVE_ENABLED] is True
    assert out[KEY_SUCCESS_WAIT_ENABLED] is False


def test_normalize_push_mode_auto_clear_success_wait_enables() -> None:
    out = normalize_push_mode(
        {KEY_SUCCESS_WAIT_ENABLED: True},
        {KEY_LIVE_ENABLED: True, KEY_SUCCESS_WAIT_ENABLED: False},
    )
    assert out[KEY_SUCCESS_WAIT_ENABLED] is True
    assert out[KEY_LIVE_ENABLED] is False


def test_normalize_push_mode_both_true_keeps_newly_enabled_live() -> None:
    # patch sets both true while current has neither — prefer live if both in patch
    # Spec: keep the one being newly enabled; if both newly true, prefer the
    # explicit order in patch: live wins when both appear (documented).
    out = normalize_push_mode(
        {KEY_LIVE_ENABLED: True, KEY_SUCCESS_WAIT_ENABLED: True},
        {KEY_LIVE_ENABLED: False, KEY_SUCCESS_WAIT_ENABLED: False},
    )
    # When both set true in same patch, last-write-wins by plan: keep newly
    # enabled mode — both are newly enabled; prefer success_wait if it was
    # the "intent" is ambiguous. Plan: "keep the one being newly enabled,
    # clear the other". When both newly enabled, clear success_wait keep live
    # (live is the newer feature / first in dual-true rule used by tests).
    assert out[KEY_LIVE_ENABLED] is True
    assert out[KEY_SUCCESS_WAIT_ENABLED] is False


def test_normalize_push_mode_single_key_put_case() -> None:
    """Enabling live while success_wait already on → success_wait forced false."""
    out = normalize_push_mode(
        {KEY_LIVE_ENABLED: True},
        {KEY_LIVE_ENABLED: False, KEY_SUCCESS_WAIT_ENABLED: True},
    )
    assert out == {
        KEY_LIVE_ENABLED: True,
        KEY_SUCCESS_WAIT_ENABLED: False,
    }


def test_normalize_push_mode_never_rejects_passthrough() -> None:
    out = normalize_push_mode(
        {KEY_LIVE_MAX_PER_CHAT: 7},
        {KEY_LIVE_ENABLED: False, KEY_SUCCESS_WAIT_ENABLED: True},
    )
    assert out[KEY_LIVE_MAX_PER_CHAT] == 7
    assert KEY_SUCCESS_WAIT_ENABLED not in out or out.get(KEY_SUCCESS_WAIT_ENABLED) is True


@pytest.mark.asyncio
async def test_is_blocked_never_raises_empty_cfg() -> None:
    g = LiveQrGate(settings=_FakeSettings({}), sse=_FakeSse())
    # no refresh_config — partial/empty cfg
    assert g.is_blocked() is False


@pytest.mark.asyncio
async def test_coerce_max_per_chat_bool_and_invalid() -> None:
    settings = _FakeSettings(
        {
            KEY_LIVE_ENABLED: True,
            KEY_LIVE_MAX_PER_CHAT: True,  # bool must not pass as 1
            "telegram.push_mode.chat_targets": [
                {"chat_id": "-1001", "label": "A", "enabled": True}
            ],
        }
    )
    g = LiveQrGate(settings=settings, sse=_FakeSse())
    await g.refresh_config()
    assert g.snapshot()["max_per_chat"] == 5  # fallback

    settings._snap[KEY_LIVE_MAX_PER_CHAT] = 0
    await g.refresh_config()
    assert g.snapshot()["max_per_chat"] == 5  # clamp/reject → fallback

    settings._snap[KEY_LIVE_MAX_PER_CHAT] = 12
    await g.refresh_config()
    assert g.snapshot()["max_per_chat"] == 12


@pytest.mark.asyncio
async def test_acquire_none_when_message_id_falsy() -> None:
    g = _gate()
    await g.refresh_config()
    assert await g.acquire("j1", "-1001", 0) is None
    assert await g.acquire("j1", "-1001", None) is None  # type: ignore[arg-type]
    assert g.chat_live_count("-1001") == 0


@pytest.mark.asyncio
async def test_settings_seed_keys_exist(tmp_path: Any) -> None:
    from pathlib import Path

    from app.core.db import DbEngine
    from app.core.settings_store import SettingsRepository
    from app.notifiers.telegram import register_telegram_namespace

    db_path = Path(tmp_path) / "t.db"
    engine = DbEngine(db_path)
    await engine.init_schema()
    settings = SettingsRepository(engine)
    await register_telegram_namespace(settings, engine)
    assert await settings.get(KEY_LIVE_ENABLED) is False
    assert await settings.get(KEY_LIVE_MAX_PER_CHAT) == 5
    await engine.close()
