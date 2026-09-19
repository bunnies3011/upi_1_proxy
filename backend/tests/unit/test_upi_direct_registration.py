"""Unit tests for UPI Direct namespace + handler registration."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.core.db import DbEngine
from app.core.job_manager import JobManager
from app.core.proxy_pool import ProxyPool
from app.core.session_cache import AccountSessionCache
from app.core.settings_store import SettingsRepository
from app.core.sse import SseBroadcaster
from app.payments._chatgpt.models import ChatgptAccount
from app.payments.upi_direct import (
    register_upi_direct_handler,
    register_upi_direct_namespace,
)

_ALL_KEYS = (
    "upi_direct.proxy_checkout",
    "upi_direct.proxy_promotion",
    "upi_direct.max_concurrent",
    "upi_direct.run_timeout_seconds",
    "upi_direct.stripe_request_timeout_seconds",
    "upi_direct.approve_error_retries",
    "upi_direct.require_promo",
)


async def _settings(tmp_path: Path) -> SettingsRepository:
    engine = DbEngine(tmp_path / "settings.db")
    await engine.init_schema()
    return SettingsRepository(engine)


async def test_register_namespace_seeds_defaults(tmp_path: Path) -> None:
    settings = await _settings(tmp_path)
    await register_upi_direct_namespace(settings)
    for key in _ALL_KEYS:
        assert await settings.get(key) is not None
    assert await settings.get("upi_direct.max_concurrent") == 3
    assert await settings.get("upi_direct.run_timeout_seconds") == 300
    assert await settings.get("upi_direct.stripe_request_timeout_seconds") == 30
    assert await settings.get("upi_direct.approve_error_retries") == 1
    assert await settings.get("upi_direct.require_promo") is True
    assert await settings.get("upi_direct.proxy_checkout") == []
    assert await settings.get("upi_direct.proxy_promotion") == []


async def test_register_namespace_no_overwrite(tmp_path: Path) -> None:
    settings = await _settings(tmp_path)
    await register_upi_direct_namespace(settings)
    await settings.set("upi_direct.max_concurrent", 7)
    assert await settings.get("upi_direct.max_concurrent") == 7


async def test_register_namespace_accepts_max_concurrent_50(tmp_path: Path) -> None:
    settings = await _settings(tmp_path)
    await register_upi_direct_namespace(settings)
    await settings.set("upi_direct.max_concurrent", 50)
    assert await settings.get("upi_direct.max_concurrent") == 50

    with pytest.raises(Exception):
        await settings.set("upi_direct.max_concurrent", 51)


async def test_register_namespace_accepts_approve_error_retries_50(
    tmp_path: Path,
) -> None:
    settings = await _settings(tmp_path)
    await register_upi_direct_namespace(settings)
    await settings.set("upi_direct.approve_error_retries", 50)
    assert await settings.get("upi_direct.approve_error_retries") == 50

    with pytest.raises(Exception):
        await settings.set("upi_direct.approve_error_retries", 51)


async def test_register_handler_contract(tmp_path: Path) -> None:
    settings = await _settings(tmp_path)
    await register_upi_direct_namespace(settings)
    cache = AccountSessionCache(settings, tmp_path / "cache")
    jm = JobManager(settings, ProxyPool(settings), SseBroadcaster())
    handler = register_upi_direct_handler(
        job_manager=jm,
        settings=settings,
        session_cache=cache,
        qr_output_dir=tmp_path / "qr",
        logger_factory=logging.getLogger,
    )
    assert handler.get_max_concurrent_key() == "upi_direct.max_concurrent"
    assert callable(getattr(handler, "check_plan_status", None))
    assert callable(getattr(handler, "get_account_dedup_key", None))
    assert "upi_direct" in jm._handlers  # noqa: SLF001

    with pytest.raises(Exception):
        register_upi_direct_handler(
            job_manager=jm,
            settings=settings,
            session_cache=cache,
            qr_output_dir=tmp_path / "qr",
            logger_factory=logging.getLogger,
        )


def test_dedup_key_normalized_email() -> None:
    from app.payments.upi_direct.flow import UpiDirectFlowHandler

    handler = UpiDirectFlowHandler(
        settings=None,  # type: ignore[arg-type]
        session_cache=None,  # type: ignore[arg-type]
        qr_output_dir=Path("."),
        logger_factory=logging.getLogger,
    )
    parsed = ChatgptAccount(
        raw_line="User@Example.COM|tok",
        email="User@Example.COM",
        password=None,
        totp_secret=None,
        access_token="tok",
    )
    assert handler.get_account_dedup_key(parsed) == "user@example.com"


def test_parse_account_formats() -> None:
    from app.payments.upi_direct.flow import UpiDirectFlowHandler
    from app.core.payment_flow import AccountLineError

    handler = UpiDirectFlowHandler(
        settings=None,  # type: ignore[arg-type]
        session_cache=None,  # type: ignore[arg-type]
        qr_output_dir=Path("."),
        logger_factory=logging.getLogger,
    )
    ok3 = handler.parse_account_line("a@b.com|pass|TOTP")
    assert isinstance(ok3, ChatgptAccount)
    ok2 = handler.parse_account_line("a@b.com|access_token_here")
    assert isinstance(ok2, ChatgptAccount)
    bad = handler.parse_account_line("not-an-email|x")
    assert isinstance(bad, AccountLineError)
