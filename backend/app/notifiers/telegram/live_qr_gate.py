"""Live QR capacity gate — rolling-window per-chat cap on live Telegram QRs.

Caps concurrent "living" Push_Mode QR messages per enabled chat
(`max_per_chat`, default 5). Slot key is the live artifact
`job_id:message_id` so a job that reruns and emits a second QR counts twice.

Release policy (primary): Plus verified | plan-check timeout 5m | job
lifecycle end (stop/rerun/delete/error). Backstop: TTL sweep (~330s ≥ 5m
plan-check window). No "Tiếp tục" resume — free slot auto-wakes scheduler
and worker.

Storage is in-memory (restart frees all slots). Accepted trade-off: for one
TTL window after restart the gate can allow ~2× the configured cap on top of
QRs still live on Telegram.

Mutex with Success Wait is enforced at the settings route layer via
`normalize_push_mode` (auto-clear the other mode; never reject).

`is_blocked()` is a sync cached bool — no I/O, never raises — safe for the
fail-open JobManager pause checker.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from app.core.settings_store import SettingsRepository
    from app.core.sse import SseBroadcaster

logger = logging.getLogger(__name__)

KEY_LIVE_ENABLED = "telegram.push_mode.live_qr.enabled"
KEY_LIVE_MAX_PER_CHAT = "telegram.push_mode.live_qr.max_per_chat"
KEY_SUCCESS_WAIT_ENABLED = "telegram.push_mode.success_wait.enabled"
KEY_CHAT_TARGETS = "telegram.push_mode.chat_targets"

_DEFAULT_MAX_PER_CHAT = 5
_DEFAULT_QR_TTL_SECONDS = 330  # ≥ 5m plan-check timeout; sweep backstop


def _coerce_max_per_chat(raw: Any) -> int:
    """Coerce max_per_chat to int in 1..50. Bool rejected; invalid → default 5."""
    if type(raw) is int and 1 <= raw <= 50:  # noqa: E721 — reject bool
        return raw
    if type(raw) is int:  # noqa: E721
        logger.warning(
            "live_qr_gate: max_per_chat=%r out of 1..50 — fallback %d",
            raw,
            _DEFAULT_MAX_PER_CHAT,
        )
        return _DEFAULT_MAX_PER_CHAT
    logger.warning(
        "live_qr_gate: max_per_chat type=%s value=%r — fallback %d",
        type(raw).__name__,
        raw,
        _DEFAULT_MAX_PER_CHAT,
    )
    return _DEFAULT_MAX_PER_CHAT


def normalize_push_mode(patch: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Auto-clear mutex between Live QR Gate and Success Wait.

    Never rejects. Enabling one mode forces the other to false in the
    returned patch (route layer persists every key in the result).

    Rules:
      - patch enables live → also set success_wait.enabled=false
      - patch enables success_wait → also set live_qr.enabled=false
      - patch sets BOTH true → keep live, clear success_wait
    """
    out = dict(patch)
    live_in = KEY_LIVE_ENABLED in patch
    wait_in = KEY_SUCCESS_WAIT_ENABLED in patch
    live_on = bool(patch.get(KEY_LIVE_ENABLED)) if live_in else None
    wait_on = bool(patch.get(KEY_SUCCESS_WAIT_ENABLED)) if wait_in else None

    if live_in and wait_in and live_on and wait_on:
        out[KEY_LIVE_ENABLED] = True
        out[KEY_SUCCESS_WAIT_ENABLED] = False
        return out

    if live_in and live_on:
        out[KEY_SUCCESS_WAIT_ENABLED] = False
    elif wait_in and wait_on:
        out[KEY_LIVE_ENABLED] = False

    return out


def _unique_enabled_chat_ids(targets: Any) -> frozenset[str]:
    if not isinstance(targets, list):
        return frozenset()
    seen: list[str] = []
    for item in targets:
        if not isinstance(item, dict):
            continue
        if not item.get("enabled"):
            continue
        cid = item.get("chat_id")
        if cid is None or cid == "":
            continue
        s = str(cid)
        if s not in seen:
            seen.append(s)
    return frozenset(seen)


@dataclass
class SlotMeta:
    job_id: str
    chat_id: str
    message_id: int | str
    acquired_at: float


class LiveQrGate:
    """In-memory per-chat live QR capacity gate."""

    def __init__(
        self,
        settings: "SettingsRepository",
        sse: "SseBroadcaster",
        *,
        qr_ttl_seconds: int = _DEFAULT_QR_TTL_SECONDS,
    ) -> None:
        self._settings = settings
        self._sse = sse
        self._qr_ttl_seconds = qr_ttl_seconds
        self._slots: dict[str, SlotMeta] = {}
        self._job_slots: dict[str, set[str]] = {}
        self._cfg_enabled: bool = False
        self._cfg_max_per_chat: int = _DEFAULT_MAX_PER_CHAT
        self._cfg_enabled_chat_ids: frozenset[str] = frozenset()
        self._blocked: bool = False
        self._lock = asyncio.Lock()
        self._wake_scheduler: Callable[[], None] | None = None
        self._wake_worker: Callable[[], None] | None = None
        self._pause_running_push_jobs: Callable[[], None] | None = None

    def set_wake_scheduler(self, callback: Callable[[], None]) -> None:
        self._wake_scheduler = callback

    def set_wake_worker_callback(self, callback: Callable[[], None]) -> None:
        self._wake_worker = callback

    def set_pause_running_push_jobs_callback(
        self, callback: Callable[[], None]
    ) -> None:
        self._pause_running_push_jobs = callback

    def is_blocked(self) -> bool:
        """Sync cached bool — no I/O, never raises (pause-checker contract)."""
        return self._blocked

    def is_chat_full(self, chat_id: str) -> bool:
        return self.chat_live_count(chat_id) >= self._cfg_max_per_chat

    def chat_live_count(self, chat_id: str) -> int:
        cid = str(chat_id)
        return sum(1 for m in self._slots.values() if m.chat_id == cid)

    def least_loaded_available_chat(
        self, enabled_chat_ids: list[str] | frozenset[str] | set[str]
    ) -> str | None:
        """Chat with fewest live slots that is still under max_per_chat."""
        best: str | None = None
        best_count = 10**9
        for raw in enabled_chat_ids:
            cid = str(raw)
            n = self.chat_live_count(cid)
            if n >= self._cfg_max_per_chat:
                continue
            if n < best_count:
                best_count = n
                best = cid
        return best

    async def reserve_available(
        self,
        job_id: str,
        enabled_chat_ids: list[str] | frozenset[str] | set[str],
        reservation_id: str,
    ) -> tuple[str, str] | None:
        """Atomically reserve one live QR slot before Telegram send.

        The send worker may be queued behind network/Telegram latency. Counting
        this reservation immediately prevents extra QR_READY jobs from piling
        up while the real Telegram message is still being sent.
        """
        async with self._lock:
            if not self._cfg_enabled:
                return None
            best: str | None = None
            best_count = 10**9
            for raw in enabled_chat_ids:
                cid = str(raw)
                n = sum(1 for m in self._slots.values() if m.chat_id == cid)
                if n >= self._cfg_max_per_chat:
                    continue
                if n < best_count:
                    best_count = n
                    best = cid
            if best is None:
                return None

            slot_id = f"{job_id}:queued:{reservation_id}"
            if slot_id in self._slots:
                return slot_id, best
            self._slots[slot_id] = SlotMeta(
                job_id=job_id,
                chat_id=best,
                message_id=f"queued:{reservation_id}",
                acquired_at=time.monotonic(),
            )
            self._job_slots.setdefault(job_id, set()).add(slot_id)
            edge = self._recompute_blocked()
            snap = self.snapshot()

        self._fire_pause_if_edge(edge)
        await self._broadcast(snap)
        return slot_id, best

    def snapshot(self) -> dict[str, Any]:
        per_chat = {cid: self.chat_live_count(cid) for cid in self._cfg_enabled_chat_ids}
        n_chats = len(self._cfg_enabled_chat_ids)
        return {
            "enabled": self._cfg_enabled,
            "max_per_chat": self._cfg_max_per_chat,
            "live": len(self._slots),
            "capacity": self._cfg_max_per_chat * n_chats,
            "blocked": self._blocked,
            "per_chat": per_chat,
        }

    def _recompute_blocked(self) -> bool:
        """Recompute cached _blocked. Returns True if False→True edge."""
        was = self._blocked
        if not self._cfg_enabled or not self._cfg_enabled_chat_ids:
            self._blocked = False
        else:
            self._blocked = all(
                self.chat_live_count(cid) >= self._cfg_max_per_chat
                for cid in self._cfg_enabled_chat_ids
            )
        return (not was) and self._blocked

    def _fire_pause_if_edge(self, edge: bool) -> None:
        if not edge or self._pause_running_push_jobs is None:
            return
        try:
            self._pause_running_push_jobs()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "live_qr_gate: pause_running callback raise: %s: %s",
                type(exc).__name__,
                exc,
            )

    def _fire_wake_if_unblocked(self, was_blocked: bool) -> None:
        if not was_blocked or self._blocked:
            return
        for name, cb in (
            ("wake_scheduler", self._wake_scheduler),
            ("wake_worker", self._wake_worker),
        ):
            if cb is None:
                continue
            try:
                cb()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "live_qr_gate: %s callback raise: %s: %s",
                    name,
                    type(exc).__name__,
                    exc,
                )

    async def refresh_config(self) -> bool:
        """Reload config from settings; recompute blocked.

        Returns True on blocked False→True edge (capacity shrink / enable).
        """
        try:
            enabled_raw = await self._settings.get(KEY_LIVE_ENABLED)
            max_raw = await self._settings.get(KEY_LIVE_MAX_PER_CHAT)
            targets = await self._settings.get(KEY_CHAT_TARGETS)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "live_qr_gate: refresh_config read fail: %s: %s",
                type(exc).__name__,
                exc,
            )
            return False

        async with self._lock:
            was_blocked = self._blocked
            self._cfg_enabled = bool(enabled_raw)
            self._cfg_max_per_chat = _coerce_max_per_chat(max_raw)
            self._cfg_enabled_chat_ids = _unique_enabled_chat_ids(targets)
            edge = self._recompute_blocked()
            snap = self.snapshot()

        self._fire_pause_if_edge(edge)
        self._fire_wake_if_unblocked(was_blocked)
        await self._broadcast(snap)
        return edge

    async def acquire(
        self,
        job_id: str,
        chat_id: str,
        message_id: int | str | None,
    ) -> str | None:
        """Record a live slot. Returns slot_id or None if disabled / no message_id."""
        if not self._cfg_enabled:
            return None
        if message_id is None or message_id == "" or message_id == 0:
            return None

        slot_id = f"{job_id}:{message_id}"
        cid = str(chat_id)
        async with self._lock:
            if slot_id in self._slots:
                return slot_id
            self._slots[slot_id] = SlotMeta(
                job_id=job_id,
                chat_id=cid,
                message_id=message_id,
                acquired_at=time.monotonic(),
            )
            self._job_slots.setdefault(job_id, set()).add(slot_id)
            edge = self._recompute_blocked()
            snap = self.snapshot()

        self._fire_pause_if_edge(edge)
        await self._broadcast(snap)
        return slot_id

    async def release(self, slot_id: str) -> bool:
        """Pop one slot. Idempotent off membership — no permanent released set."""
        async with self._lock:
            was_blocked = self._blocked
            meta = self._slots.pop(slot_id, None)
            if meta is None:
                return False
            job_set = self._job_slots.get(meta.job_id)
            if job_set is not None:
                job_set.discard(slot_id)
                if not job_set:
                    self._job_slots.pop(meta.job_id, None)
            self._recompute_blocked()
            snap = self.snapshot()

        self._fire_wake_if_unblocked(was_blocked)
        await self._broadcast(snap)
        return True

    async def release_job(self, job_id: str) -> int:
        """Free ALL slots for a job (lifecycle/stop/rerun/removed)."""
        async with self._lock:
            was_blocked = self._blocked
            slot_ids = list(self._job_slots.pop(job_id, set()))
            if not slot_ids:
                return 0
            for sid in slot_ids:
                self._slots.pop(sid, None)
            self._recompute_blocked()
            snap = self.snapshot()
            n = len(slot_ids)

        self._fire_wake_if_unblocked(was_blocked)
        await self._broadcast(snap)
        return n

    async def sweep_expired(self) -> int:
        """Release slots older than QR TTL. Returns freed count."""
        now = time.monotonic()
        async with self._lock:
            was_blocked = self._blocked
            stale = [
                sid
                for sid, meta in self._slots.items()
                if (now - meta.acquired_at) >= self._qr_ttl_seconds
            ]
            if not stale:
                return 0
            for sid in stale:
                meta = self._slots.pop(sid, None)
                if meta is None:
                    continue
                job_set = self._job_slots.get(meta.job_id)
                if job_set is not None:
                    job_set.discard(sid)
                    if not job_set:
                        self._job_slots.pop(meta.job_id, None)
            self._recompute_blocked()
            snap = self.snapshot()
            n = len(stale)

        self._fire_wake_if_unblocked(was_blocked)
        await self._broadcast(snap)
        return n

    async def _broadcast(self, snapshot: dict[str, Any]) -> None:
        try:
            broadcast = getattr(self._sse, "broadcast_live_qr_gate_updated", None)
            if broadcast is None:
                return
            await broadcast(snapshot)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "live_qr_gate: broadcast fail: %s: %s",
                type(exc).__name__,
                exc,
            )
