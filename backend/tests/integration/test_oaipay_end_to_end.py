"""End-to-end upi_nocdk path with fakes for OaiPay + captcha."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from app.core.db import DbEngine
from app.core.job_manager import JobManager
from app.core.payment_flow import JobStatus
from app.core.proxy_pool import ProxyPool
from app.core.session_cache import AccountSessionCache
from app.core.settings_store import SettingsRepository
from app.core.sse import SseBroadcaster
from app.payments.upi_oaipay import register_oaipay_namespace
from app.payments.upi_oaipay.flow import OaipayFlowHandler
from app.payments.upi_oaipay.models import CaptchaConfig, LongLinkResult
from tests.support import oaipay_fixtures as fx
from tests.support.fake_http import FakeAsyncSession

_EMAIL = "e2e@example.com"
_ACCESS = "eyJ.e2e.ACCESS_TOKEN_OAIPAY"
_LINE = f"{_EMAIL}|{_ACCESS}"
_TERMINAL = {JobStatus.QR_READY, JobStatus.ERROR, JobStatus.STOPPED}


class _FakeClient:
    def __init__(self) -> None:
        self.calls = 0

    async def get_captcha_config(self) -> CaptchaConfig:
        return CaptchaConfig(provider="local", site_key=None)

    async def token_info(self, *a, **k):
        return {"ok": True}

    async def long_link_stream(self, *a, **k) -> LongLinkResult:
        self.calls += 1
        d = fx.done_result_ok()
        return LongLinkResult(
            ok=True,
            provider_redirect_url=d["provider_redirect_url"],
            long_url=d["long_url"],
            stripe_redirect_url=d.get("stripe_redirect_url"),
            cs_id=d.get("cs_id"),
            upi_intent_id=d.get("upi_intent_id"),
            upi_expires_at=d.get("upi_expires_at"),
            fallback=False,
            provider_error=None,
        )


async def _wait_for(jm: JobManager, job_id: str, timeout_s: float = 10.0):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while True:
        record = jm.get_job(job_id)
        if record is not None and record.status in _TERMINAL:
            return record
        if loop.time() >= deadline:
            raise AssertionError(f"job not terminal: {record}")
        await asyncio.sleep(0.05)


async def test_oaipay_e2e_qr_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.core.http_client.create_async_client",
        lambda **kw: FakeAsyncSession(),
    )

    engine = DbEngine(tmp_path / "e2e.db")
    await engine.init_schema()
    settings = SettingsRepository(engine)
    await register_oaipay_namespace(settings)
    await settings.set("oaipay.proxy_checkout", ["socks5://u:p@h:1"])
    await settings.set("oaipay.proxy_promotion", ["socks5://u:p@h:2"])

    sse = SseBroadcaster()
    jm = JobManager(settings, ProxyPool(settings), sse)
    await jm.apply_max_concurrent_from_settings()
    cache = AccountSessionCache(settings, tmp_path / "cache")
    cache.apply_settings({"session_cache.enabled": True, "session_cache.ttl_hours": 24})
    fake = _FakeClient()

    handler = OaipayFlowHandler(
        settings=settings,
        session_cache=cache,
        qr_output_dir=tmp_path / "qr",
        logger_factory=logging.getLogger,
        oaipay_client_factory=lambda session, base_url, secrets, logger: fake,
    )
    jm.register_handler("upi_nocdk", handler)

    terminal: list = []

    async def hook(record) -> None:
        terminal.append(record)

    jm.register_terminal_hook(hook)

    batch = await jm.submit_batch("upi_nocdk", [_LINE], start=True)
    assert not batch.skipped
    assert len(batch.created_job_ids) == 1
    job_id = batch.created_job_ids[0]

    record = await _wait_for(jm, job_id)
    assert record.status == JobStatus.QR_READY, (
        f"status={record.status} code={record.error_code} msg={record.error_message}"
    )
    assert record.artifact_path
    assert Path(record.artifact_path).is_file()
    assert record.payment_link == fx.LONG_URL
    assert fake.calls == 1
    assert terminal
    assert terminal[0].artifact_path == record.artifact_path
    assert terminal[0].payment_link == record.payment_link

    await jm.shutdown()
    await engine.close()
