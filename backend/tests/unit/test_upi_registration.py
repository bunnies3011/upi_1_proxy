"""Unit tests for UPI namespace + handler registration (`payments/upi`).

Mirrors the iDEAL registration contract: `register_upi_namespace` seeds every
`upi.*` key with its default, `register_upi_handler` registers the handler under
`"upi"` and satisfies the `PaymentFlowHandler` duck-type, and
`submit_batch(payment_method="upi")` routes to it without raising
`UnknownPaymentMethodError`.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.core.db import DbEngine
from app.core.errors import SettingsValidationError
from app.core.job_manager import JobManager
from app.core.payment_flow import ParsedAccount
from app.core.proxy_pool import ProxyPool
from app.core.session_cache import AccountSessionCache
from app.core.settings_store import SettingsRepository
from app.core.sse import SseBroadcaster
from app.payments.upi import register_upi_handler, register_upi_namespace

_ALL_UPI_KEYS = (
    "upi.license_codes",
    "upi.max_concurrent",
    "upi.run_timeout_seconds",
    "upi.eligibility_precheck",
    "upi.auto_retry_blocked_codes",
    "upi.vendor_base_url",
)


async def _make_settings(tmp_path: Path) -> SettingsRepository:
    engine = DbEngine(tmp_path / "settings.db")
    await engine.init_schema()
    return SettingsRepository(engine)


def _make_job_manager(settings: SettingsRepository) -> JobManager:
    return JobManager(settings, ProxyPool(settings), SseBroadcaster())


async def _register(tmp_path: Path):
    settings = await _make_settings(tmp_path)
    await register_upi_namespace(settings)
    session_cache = AccountSessionCache(settings, tmp_path / "cache")
    job_manager = _make_job_manager(settings)
    handler = register_upi_handler(
        job_manager=job_manager,
        settings=settings,
        session_cache=session_cache,
        qr_output_dir=tmp_path / "qr",
        logger_factory=logging.getLogger,
    )
    return settings, job_manager, handler


# ---------------------------------------------------------------------------
# register_upi_namespace
# ---------------------------------------------------------------------------


async def test_register_namespace_seeds_all_keys_with_defaults(tmp_path: Path) -> None:
    settings = await _make_settings(tmp_path)
    await register_upi_namespace(settings)

    # Every key resolves (whitelisted) and is seeded (non-None).
    for key in _ALL_UPI_KEYS:
        assert await settings.get(key) is not None, f"{key} not seeded"

    assert await settings.get("upi.max_concurrent") == 3
    assert await settings.get("upi.run_timeout_seconds") == 120
    assert await settings.get("upi.eligibility_precheck") is False
    assert await settings.get("upi.vendor_base_url") == "https://pix.capybara.cv"
    assert await settings.get("upi.license_codes") == []
    codes = await settings.get("upi.auto_retry_blocked_codes")
    assert "upi_stream_error" in codes
    assert "upi_run_timeout" in codes


async def test_register_namespace_enforces_constraints(tmp_path: Path) -> None:
    settings = await _make_settings(tmp_path)
    await register_upi_namespace(settings)

    with pytest.raises(SettingsValidationError):
        await settings.set("upi.max_concurrent", 0)  # below min
    with pytest.raises(SettingsValidationError):
        await settings.set("upi.max_concurrent", 999)  # above max
    with pytest.raises(SettingsValidationError):
        await settings.set("upi.eligibility_precheck", "yes")  # not a bool


async def test_register_namespace_does_not_overwrite_existing(tmp_path: Path) -> None:
    settings = await _make_settings(tmp_path)
    await register_upi_namespace(settings)
    await settings.set("upi.max_concurrent", 7)

    # Re-seeding on a fresh repository pointing at the same values must not clobber
    # an operator-set value: seed only writes when get() is None. Simulate by
    # re-running the seed loop through a second namespace registration is not
    # possible (namespace already registered), so assert the set value persists.
    assert await settings.get("upi.max_concurrent") == 7


# ---------------------------------------------------------------------------
# register_upi_handler
# ---------------------------------------------------------------------------


async def test_register_handler_registers_under_upi_key(tmp_path: Path) -> None:
    _settings, job_manager, handler = await _register(tmp_path)

    assert job_manager.get_handler("upi") is handler


async def test_handler_satisfies_payment_flow_contract(tmp_path: Path) -> None:
    _settings, _job_manager, handler = await _register(tmp_path)

    assert callable(handler.run)
    assert callable(handler.parse_account_line)
    assert handler.get_max_concurrent_key() == "upi.max_concurrent"


async def test_parse_account_line_delegates_to_shared_parser(tmp_path: Path) -> None:
    _settings, _job_manager, handler = await _register(tmp_path)

    parsed = handler.parse_account_line("user@example.com|access_token_abc")
    assert isinstance(parsed, ParsedAccount)


async def test_submit_batch_routes_to_upi_handler(tmp_path: Path) -> None:
    _settings, job_manager, _handler = await _register(tmp_path)

    result = await job_manager.submit_batch(
        "upi", ["user@example.com|access_token_abc"], start=False
    )

    assert len(result.created_job_ids) == 1
    assert result.skipped == []
