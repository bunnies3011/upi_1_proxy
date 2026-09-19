"""Integration: OaiPay namespace + handler wired via bootstrap."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from app.bootstrap import bootstrap_services

_BG_NAMES = frozenset({"proxy-preflight-startup", "telegram-pull-mode-polling"})


async def _shutdown(services) -> None:
    await services.job_manager.shutdown()
    leftover = [
        t
        for t in asyncio.all_tasks()
        if t.get_name() in _BG_NAMES and not t.done()
    ]
    for t in leftover:
        t.cancel()
    for t in leftover:
        with contextlib.suppress(asyncio.CancelledError):
            await t
    await services.db_engine.close()


async def test_oaipay_handler_registered_and_defaults_seeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "boot.db"
    qr_dir = tmp_path / "qr"
    cache_dir = tmp_path / "cache"
    qr_dir.mkdir()
    cache_dir.mkdir()

    async def _noop_probe(*a, **k):
        return None

    monkeypatch.setattr("app.core.proxy_health.probe_pool_batch", _noop_probe)

    services = await bootstrap_services(
        db_path=db_path,
        bind_host="127.0.0.1",
        session_cache_dir=cache_dir,
        qr_output_dir=qr_dir,
    )
    try:
        jm = services.job_manager
        settings = services.settings
        assert jm.get_handler("upi_nocdk") is not None
        assert await settings.get("oaipay.max_concurrent") == 3
        assert await settings.get("oaipay.base_url") == "https://oaipay.12001234.xyz"

        await settings.set("oaipay.max_concurrent", 5)
        await jm.maybe_reload_concurrency(["oaipay.max_concurrent"])
        # Concurrency is per payment method now; assert on this method's limiter
        # rather than the aggregate max (ideal seeds 20, which is unrelated).
        assert jm.get_method_max_concurrent("upi_nocdk") == 5
    finally:
        await _shutdown(services)
