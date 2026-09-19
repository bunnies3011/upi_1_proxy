"""UpiLicensePool — RAM-cached pool of `PK-XXXX` license codes for the UPI flow.

This mirrors `core/proxy_pool.py`: where `ProxyPool` leases a proxy per job and
returns it on completion, `UpiLicensePool` **reserves** one unit of license
credit per job (`acquire`) and either commits or restores it on completion
(`release`). The reserve is done *under the lock, before the lock is released*
— the same lease-at-acquire discipline as `ProxyPool.acquire` — so two
concurrent acquires of the last remaining credit can never both win it.

`remaining` per code is cached in RAM and treated as authoritative between
verifications. It is refreshed lazily from the vendor `key/verify` endpoint:
a code is (re)verified when it has never been checked or when its last check is
older than `refresh_interval_seconds`. A code the vendor reports with no
remaining credit is pruned; a code that fails on a vendor credit error is pruned
by the handler via `mark_exhausted`. When the pool empties, `acquire` fails fast
with `LicenseExhaustedError` (`error_code="upi_no_license_credit"`, not
auto-retryable) — the operator re-lists codes to refill.

Egress: `key/verify` runs outside a job (direct from the server IP), driven only
by the lazy + refresh cadence, so it is low-frequency and not proxied.

Payment_Module_Boundary: this module imports only `app.payments.upi.*` —
NOTHING from `app.core.*` or `app.payments.ideal`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from app.payments.upi.errors import LicenseExhaustedError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.settings_store import SettingsRepository
    from app.payments.upi.vendor_client import CapybaraClient

#: Default rotation strategy — round-robin across codes, mirroring `ProxyPool`.
_DEFAULT_ROTATION_MODE = "round_robin"
#: Channel passed to `key/verify` for this pool.
_LICENSE_CHANNEL = "upi"

#: A factory that yields a ready-to-use vendor client. Injected (settings-driven,
#: like the proxy pool) so the pool needn't own the HTTP session lifecycle.
VendorClientFactory = Callable[[], "CapybaraClient"]


@dataclass
class UpiLicenseState:
    """Mutable RAM state of one PK code in the pool — NOT public.

    Attributes:
        code: The `PK-XXXX` code exactly as listed in `upi.license_codes`.
        remaining: Locally-authoritative remaining credit. Decremented on
            `acquire` (reserve), restored on `release(committed=False)`, and
            overwritten by the vendor value on each verification.
        total: Lifetime credit the vendor reports for this code (informational).
        valid: Whether the vendor considers the code usable. A code is only
            eligible for `acquire` when `valid and remaining > 0`.
        in_flight: Number of reservations currently held (mirrors
            `_ProxyState.lease_count`); used by the least-used picker.
        last_verified_at: `time.time()` of the last successful `key/verify`, or
            `None` if the code has never been verified (freshly listed).
    """

    code: str
    remaining: int = 0
    total: int = 0
    valid: bool = False
    in_flight: int = 0
    last_verified_at: float | None = None


class UpiLicensePool:
    """Lease-based license credit pool, `round_robin` | `least_used`.

    Starts empty (safe default) so `acquire()` behaves correctly before the
    first `apply_settings()` at startup — an empty pool raises
    `LicenseExhaustedError`, never silently succeeds.
    """

    def __init__(
        self,
        settings: "SettingsRepository",
        vendor_client_factory: VendorClientFactory,
        *,
        refresh_interval_seconds: float,
    ) -> None:
        self._settings = settings
        self._vendor_client_factory = vendor_client_factory
        self._refresh_interval_seconds = float(refresh_interval_seconds)
        self._codes: dict[str, UpiLicenseState] = {}
        self._rotation_mode: str = _DEFAULT_ROTATION_MODE
        self._round_robin_cursor: int = 0
        # `acquire` is async — the lock makes "verify + pick + reserve" a single
        # atomic step even when several job coroutines call `acquire` at once.
        # The sync methods (`release`/`mark_exhausted`) mutate one dict entry and
        # do not need the lock (single-op, GIL-safe) — a deliberate simplicity
        # tradeoff matching `ProxyPool`.
        self._lock: asyncio.Lock = asyncio.Lock()
        self._logger = logging.getLogger("app.payments.upi.license_pool")

    def apply_settings(self, snapshot: dict) -> None:
        """Hydrate the pool from a settings `snapshot` dict.

        Does NOT query the DB — `snapshot` is a `{key: value}` dict read from
        outside (e.g. `settings_repo.bulk_get([...])`). Rebuilds `_codes`:
        KEEP the live state (remaining / in_flight / verification) of any code
        still present in the new `upi.license_codes`, DROP codes no longer
        listed, and ADD newly listed codes unverified. Re-adds a previously
        pruned code if the operator re-lists it.
        """
        codes = snapshot.get("upi.license_codes") or []
        self._rotation_mode = snapshot.get("upi.rotation_mode") or _DEFAULT_ROTATION_MODE

        rebuilt: dict[str, UpiLicenseState] = {}
        for raw in codes:
            if not isinstance(raw, str):
                continue
            code = raw.strip()
            if not code:
                continue
            existing = self._codes.get(code)
            rebuilt[code] = existing if existing is not None else UpiLicenseState(code=code)
        self._codes = rebuilt
        self._round_robin_cursor = 0

    async def acquire(self) -> str:
        """Reserve one unit of credit and return the PK code that owns it.

        Under the lock: lazily (re)verify unverified/stale codes via
        `key/verify`, prune any the vendor reports exhausted, pick the first
        eligible code (`valid and remaining > 0`) per the rotation mode, and
        **decrement its `remaining` before releasing the lock** (reserve). No
        eligible code → raise `LicenseExhaustedError`.

        Raises:
            LicenseExhaustedError: No code has remaining credit (pool empty or
                every code exhausted). `error_code="upi_no_license_credit"`,
                a durable failure that is NOT auto-retried.
        """
        async with self._lock:
            if not self._codes:
                raise LicenseExhaustedError(checked_codes=0)

            await self._refresh_stale_locked()

            checked = len(self._codes)
            code = self._pick()
            if code is None:
                raise LicenseExhaustedError(checked_codes=checked)

            state = self._codes[code]
            # RESERVE under the lock, before it is released — this is what makes
            # "exactly one of two concurrent acquires wins the last credit" hold.
            state.remaining -= 1
            state.in_flight += 1
            return code

    def release(self, code: str, *, committed: bool) -> None:
        """Return a reserved credit to the pool.

        `committed=True` (vendor confirmed the spend) keeps the decrement.
        `committed=False` (ineligible / challenge fail / stream error / cancel /
        timeout) restores `remaining += 1`. No-op if `code` is no longer in the
        pool (pruned via `mark_exhausted`/verify while the job was running) —
        there is nothing to return to.
        """
        state = self._codes.get(code)
        if state is None:
            return

        if state.in_flight > 0:
            state.in_flight -= 1
        if not committed:
            state.remaining += 1

    def mark_exhausted(self, code: str) -> None:
        """Remove `code` from the active pool immediately (prune).

        The handler calls this on a vendor credit-specific failure. No-op if the
        code is already gone.
        """
        self._remove(code)

    def total_remaining(self) -> int:
        """Sum of locally-cached `remaining` across all live codes."""
        return sum(state.remaining for state in self._codes.values())

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _refresh_stale_locked(self) -> None:
        """Re-verify unverified/stale codes in one batched `key/verify`, write
        the vendor result back into local state, and prune exhausted codes.

        Runs under `self._lock` (caller holds it). Codes the vendor omits from
        its response are marked invalid + zero and pruned — a listed code the
        vendor no longer recognises is not usable.
        """
        now = time.time()
        stale = [code for code, state in self._codes.items() if self._needs_verify(state, now)]
        if not stale:
            return

        client = self._vendor_client_factory()
        result = await client.key_verify(stale, channel=_LICENSE_CHANNEL)
        verified_at = time.time()
        by_code = {item.code: item for item in result.items if item.code}

        for code in stale:
            state = self._codes.get(code)
            if state is None:
                continue
            item = self._match_verify_item(code, by_code)
            if item is None:
                state.valid = False
                state.remaining = 0
                state.last_verified_at = verified_at
                continue
            state.remaining = item.remaining
            state.total = item.total
            state.valid = item.valid
            state.last_verified_at = verified_at

        # Prune any code the vendor now reports with no remaining credit.
        for code in stale:
            state = self._codes.get(code)
            if state is not None and state.remaining <= 0:
                self._remove(code)

    @staticmethod
    def _match_verify_item(code: str, by_code: dict) -> object | None:
        """Map a pool-listed code to a vendor `key/verify` item.

        Exact match first. The vendor may echo a *truncated* form of the CDK
        (drop trailing hyphen segments) while still accepting the full code on
        verify/challenge/run — e.g. pool holds `PK-AAAA-BBBB-CCCC-DDDD-EEEE`,
        response has `code=PK-AAAA-BBBB-CCCC` with remaining credit. Without
        prefix matching every full code looks "omitted", gets `remaining=0`,
        and is pruned → `upi_no_license_credit (checked 0)` despite live credit.

        Only accept a unique prefix/suffix hit so two listed codes that share a
        prefix never cross-wire.
        """
        item = by_code.get(code)
        if item is not None:
            return item

        hits = [
            candidate
            for echoed, candidate in by_code.items()
            if code.startswith(f"{echoed}-") or echoed.startswith(f"{code}-")
        ]
        if len(hits) == 1:
            return hits[0]
        return None

    def _needs_verify(self, state: UpiLicenseState, now: float) -> bool:
        if state.last_verified_at is None:
            return True
        if self._refresh_interval_seconds <= 0:
            return True
        return (now - state.last_verified_at) >= self._refresh_interval_seconds

    def _pick(self) -> str | None:
        if self._rotation_mode == "least_used":
            return self._pick_least_used()
        return self._pick_round_robin()

    def _pick_round_robin(self) -> str | None:
        """Pick the next eligible code cyclically from the current cursor."""
        codes = list(self._codes.keys())
        total = len(codes)
        for offset in range(total):
            index = (self._round_robin_cursor + offset) % total
            code = codes[index]
            state = self._codes[code]
            if state.valid and state.remaining > 0:
                self._round_robin_cursor = (index + 1) % total
                return code
        return None

    def _pick_least_used(self) -> str | None:
        """Pick the eligible code with the fewest in-flight reservations
        (tie-break: listing order)."""
        best_code: str | None = None
        best_in_flight: int | None = None
        for code, state in self._codes.items():
            if not (state.valid and state.remaining > 0):
                continue
            if best_in_flight is None or state.in_flight < best_in_flight:
                best_code = code
                best_in_flight = state.in_flight
        return best_code

    def _remove(self, code: str) -> None:
        if code not in self._codes:
            return
        del self._codes[code]
        self._logger.warning(
            "upi license removed %s; %d codes / %d credits remain",
            code,
            len(self._codes),
            self.total_remaining(),
        )


__all__ = ["UpiLicensePool", "UpiLicenseState", "VendorClientFactory"]
