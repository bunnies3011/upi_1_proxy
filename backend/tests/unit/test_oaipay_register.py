"""Unit tests for OaiPay namespace + handler registration."""

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
from app.payments.upi_oaipay import register_oaipay_handler, register_oaipay_namespace

_ALL_KEYS = (
    "oaipay.base_url",
    "oaipay.proxy_checkout",
    "oaipay.proxy_promotion",
    "oaipay.yescaptcha_api_key",
    "oaipay.max_concurrent",
    "oaipay.run_timeout_seconds",
)


async def _settings(tmp_path: Path) -> SettingsRepository:
    engine = DbEngine(tmp_path / "settings.db")
    await engine.init_schema()
    return SettingsRepository(engine)


async def test_register_namespace_seeds_defaults(tmp_path: Path) -> None:
    settings = await _settings(tmp_path)
    await register_oaipay_namespace(settings)
    for key in _ALL_KEYS:
        assert await settings.get(key) is not None
    assert await settings.get("oaipay.max_concurrent") == 3
    assert await settings.get("oaipay.run_timeout_seconds") == 300
    assert await settings.get("oaipay.base_url") == "https://oaipay.12001234.xyz"
    assert await settings.get("oaipay.proxy_checkout") == []
    assert await settings.get("oaipay.yescaptcha_api_key") == ""


async def test_register_namespace_no_overwrite(tmp_path: Path) -> None:
    settings = await _settings(tmp_path)
    await register_oaipay_namespace(settings)
    await settings.set("oaipay.max_concurrent", 7)
    # Seed only writes when get() is None — existing operator value stays.
    assert await settings.get("oaipay.max_concurrent") == 7
    assert await settings.get("oaipay.base_url") == "https://oaipay.12001234.xyz"


async def test_register_handler(tmp_path: Path) -> None:
    settings = await _settings(tmp_path)
    await register_oaipay_namespace(settings)
    cache = AccountSessionCache(settings, tmp_path / "cache")
    jm = JobManager(settings, ProxyPool(settings), SseBroadcaster())
    handler = register_oaipay_handler(
        job_manager=jm,
        settings=settings,
        session_cache=cache,
        qr_output_dir=tmp_path / "qr",
        logger_factory=logging.getLogger,
    )
    assert handler.get_max_concurrent_key() == "oaipay.max_concurrent"
    assert callable(getattr(handler, "check_plan_status", None))
    assert "upi_nocdk" in jm._handlers  # noqa: SLF001

    with pytest.raises(Exception):
        register_oaipay_handler(
            job_manager=jm,
            settings=settings,
            session_cache=cache,
            qr_output_dir=tmp_path / "qr",
            logger_factory=logging.getLogger,
        )
