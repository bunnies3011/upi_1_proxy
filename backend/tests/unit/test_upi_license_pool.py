"""Unit tests for `payments/upi/license_pool.py` — `UpiLicensePool`.

Mirrors the shape of `tests/unit/test_proxy_pool.py`: the pool is exercised
through its public surface with a fake vendor client whose `key_verify`
returns canned `KeyVerifyItem`s. The reserve-at-acquire concurrency case
(no double-spend of the last credit) is the load-bearing regression here.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.payments.upi.errors import LicenseExhaustedError
from app.payments.upi.license_pool import UpiLicensePool, UpiLicenseState
from app.payments.upi.models import KeyVerify, KeyVerifyItem


def _item(code: str, remaining: int, total: int = 10, valid: bool = True) -> KeyVerifyItem:
    return KeyVerifyItem(code=code, remaining=remaining, total=total, valid=valid)


class _FakeVendorClient:
    """Stand-in for `CapybaraClient` exposing only `key_verify`.

    Records every batch of codes it is asked to verify (shared `calls` list,
    so re-verification across factory instantiations is observable) and yields
    control once per call so the pool's lock is genuinely contended in the
    concurrency test.
    """

    def __init__(self, items_by_code: dict[str, KeyVerifyItem], calls: list[list[str]]) -> None:
        self._items_by_code = items_by_code
        self._calls = calls

    async def key_verify(self, codes, channel: str = "upi") -> KeyVerify:
        requested = list(codes)
        self._calls.append(list(requested))
        # Force a suspension point *while the pool holds its lock* so a second
        # concurrent acquire cannot slip in and reserve the same last credit.
        await asyncio.sleep(0)
        items = tuple(self._items_by_code[c] for c in requested if c in self._items_by_code)
        total = sum(i.remaining for i in items)
        valid = bool(items) and all(i.valid for i in items)
        return KeyVerify(channel=channel, items=items, total=total, valid=valid)


def _make_pool(
    codes: list[str],
    items_by_code: dict[str, KeyVerifyItem],
    *,
    rotation_mode: str = "round_robin",
    refresh_interval_seconds: float = 300.0,
) -> tuple[UpiLicensePool, list[list[str]]]:
    calls: list[list[str]] = []
    pool = UpiLicensePool(
        settings=None,  # type: ignore[arg-type] — apply_settings does not query DB
        vendor_client_factory=lambda: _FakeVendorClient(items_by_code, calls),
        refresh_interval_seconds=refresh_interval_seconds,
    )
    pool.apply_settings({"upi.license_codes": codes, "upi.rotation_mode": rotation_mode})
    return pool, calls


async def test_acquire_reserves_credited_code_and_prunes_zero_credit() -> None:
    """acquire() decrements a code with remaining>0; a remaining=0 code is
    pruned on verify and never returned."""
    items = {"PK-1": _item("PK-1", 0), "PK-2": _item("PK-2", 5)}
    pool, _ = _make_pool(["PK-1", "PK-2"], items)

    code = await pool.acquire()

    assert code == "PK-2"
    # PK-2 reserved: 5 -> 4. PK-1 pruned (remaining=0), so total = 4.
    assert pool.total_remaining() == 4
    # A pruned code is never handed out again.
    for _ in range(3):
        assert await pool.acquire() == "PK-2"


async def test_release_uncommitted_restores_committed_keeps_decrement() -> None:
    """release(committed=False) restores the reserved credit; committed=True
    keeps the decrement (vendor confirmed the spend)."""
    items = {"PK-1": _item("PK-1", 5)}
    pool, _ = _make_pool(["PK-1"], items)

    code = await pool.acquire()  # 5 -> 4
    assert pool.total_remaining() == 4
    pool.release(code, committed=False)  # restore -> 5
    assert pool.total_remaining() == 5

    code2 = await pool.acquire()  # 5 -> 4
    pool.release(code2, committed=True)  # keep 4
    assert pool.total_remaining() == 4


async def test_acquire_raises_when_no_code_has_credit() -> None:
    """Every code at remaining=0 → acquire() raises LicenseExhaustedError with
    the stable non-retryable error code."""
    items = {"PK-1": _item("PK-1", 0), "PK-2": _item("PK-2", 0)}
    pool, _ = _make_pool(["PK-1", "PK-2"], items)

    with pytest.raises(LicenseExhaustedError) as exc_info:
        await pool.acquire()

    assert exc_info.value.error_code == "upi_no_license_credit"


async def test_apply_settings_preserves_remaining_drops_and_adds_unverified() -> None:
    """apply_settings keeps live `remaining` for surviving codes, drops removed
    ones, and adds new codes unverified."""
    items = {"PK-1": _item("PK-1", 5), "PK-2": _item("PK-2", 3), "PK-3": _item("PK-3", 7)}
    pool, _ = _make_pool(["PK-1", "PK-2"], items)

    code = await pool.acquire()  # round-robin verifies both, reserves PK-1: 5 -> 4
    assert code == "PK-1"

    pool.apply_settings({"upi.license_codes": ["PK-1", "PK-3"]})

    assert pool._codes["PK-1"].remaining == 4  # preserved live remaining
    assert "PK-2" not in pool._codes  # dropped
    assert "PK-3" in pool._codes  # added
    assert pool._codes["PK-3"].last_verified_at is None  # unverified
    assert pool._codes["PK-3"].remaining == 0


async def test_stale_code_reverified_on_next_acquire() -> None:
    """A code past the refresh interval is re-verified on the next acquire; a
    fresh code is not."""
    items = {"PK-1": _item("PK-1", 5)}
    pool, calls = _make_pool(["PK-1"], items, refresh_interval_seconds=300.0)

    await pool.acquire()  # verify #1
    assert len(calls) == 1

    # Fresh code: no re-verify on the very next acquire.
    await pool.acquire()
    assert len(calls) == 1

    # Age the code past the refresh interval → re-verify.
    pool._codes["PK-1"].last_verified_at = time.time() - 10_000
    await pool.acquire()
    assert len(calls) == 2


async def test_mark_exhausted_removes_code_from_pool() -> None:
    """mark_exhausted removes the code immediately; subsequent acquires never
    return it and total_remaining drops accordingly."""
    items = {"PK-1": _item("PK-1", 5), "PK-2": _item("PK-2", 4)}
    pool, _ = _make_pool(["PK-1", "PK-2"], items)

    first = await pool.acquire()  # both verified, PK-1 reserved 5 -> 4
    assert first == "PK-1"
    assert pool.total_remaining() == 8  # 4 (PK-1) + 4 (PK-2)

    pool.mark_exhausted("PK-1")

    assert "PK-1" not in pool._codes
    assert pool.total_remaining() == 4  # only PK-2 remains
    for _ in range(4):
        assert await pool.acquire() == "PK-2"


async def test_removing_last_code_raises_on_next_acquire() -> None:
    """When the last code is removed, acquire() raises LicenseExhaustedError."""
    items = {"PK-1": _item("PK-1", 5)}
    pool, _ = _make_pool(["PK-1"], items)

    await pool.acquire()
    pool.mark_exhausted("PK-1")

    with pytest.raises(LicenseExhaustedError):
        await pool.acquire()


async def test_least_used_spreads_reservations_across_codes() -> None:
    """least_used picks the available code with the fewest in-flight
    reservations (mirrors ProxyPool._pick_least_used)."""
    items = {"PK-1": _item("PK-1", 5), "PK-2": _item("PK-2", 5)}
    pool, _ = _make_pool(["PK-1", "PK-2"], items, rotation_mode="least_used")

    c1 = await pool.acquire()
    c2 = await pool.acquire()

    assert {c1, c2} == {"PK-1", "PK-2"}


async def test_concurrent_acquire_of_last_credit_no_double_spend() -> None:
    """Two concurrent acquires on a pool whose only code has remaining=1:
    exactly one wins the credit, the other raises LicenseExhaustedError.

    Regression for reserve-at-acquire — the decrement happens under the lock
    before it is released, so no post-hoc bookkeeping can double-spend.
    """
    items = {"PK-1": _item("PK-1", 1)}
    pool, _ = _make_pool(["PK-1"], items)

    results = await asyncio.gather(
        pool.acquire(), pool.acquire(), return_exceptions=True
    )

    successes = [r for r in results if r == "PK-1"]
    failures = [r for r in results if isinstance(r, LicenseExhaustedError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert pool.total_remaining() == 0


def test_license_state_defaults_are_unverified() -> None:
    """A freshly-listed code starts unverified (no credit, invalid) until the
    first key_verify establishes its state."""
    state = UpiLicenseState(code="PK-9")

    assert state.remaining == 0
    assert state.total == 0
    assert state.valid is False
    assert state.in_flight == 0
    assert state.last_verified_at is None


async def test_vendor_truncated_code_echo_maps_to_full_pool_code() -> None:
    """pix.capybara.cv key/verify echoes a shortened CDK (drops trailing
    segments) while accepting the full code. Pool must map the truncated
    echo back to the full listed code — otherwise every code looks omitted,
    is pruned, and acquire fails with upi_no_license_credit (checked 0)."""
    full = "PK-4TWD-PJNM-5KX2-QW22-ZXVE"
    short = "PK-4TWD-PJNM-5KX2"

    class _TruncatingVendor:
        def __init__(self, calls: list[list[str]]) -> None:
            self._calls = calls

        async def key_verify(self, codes, channel: str = "upi") -> KeyVerify:
            requested = list(codes)
            self._calls.append(list(requested))
            await asyncio.sleep(0)
            return KeyVerify(
                channel=channel,
                items=(_item(short, 45, total=45, valid=True),),
                total=45,
                valid=True,
            )

    calls: list[list[str]] = []
    pool = UpiLicensePool(
        settings=None,  # type: ignore[arg-type]
        vendor_client_factory=lambda: _TruncatingVendor(calls),
        refresh_interval_seconds=300.0,
    )
    pool.apply_settings({"upi.license_codes": [full]})

    code = await pool.acquire()

    assert code == full
    assert calls == [[full]]
    assert full in pool._codes
    assert short not in pool._codes
    assert pool._codes[full].remaining == 44
    assert pool._codes[full].valid is True
    assert pool.total_remaining() == 44
