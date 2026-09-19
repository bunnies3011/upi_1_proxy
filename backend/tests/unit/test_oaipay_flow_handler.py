"""Unit tests for OaipayFlowHandler."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import pytest

from app.core.db import DbEngine
from app.core.payment_flow import Job, JobStatus, SimpleCancellationToken
from app.core.session_cache import AccountSessionCache
from app.core.settings_store import SettingsRepository
from app.payments.upi_oaipay import register_oaipay_namespace
from app.payments.upi_oaipay.errors import CaptchaSolveError, StreamError
from app.payments.upi_oaipay.flow import OaipayFlowHandler
from app.payments.upi_oaipay.models import (
    CaptchaConfig,
    LongLinkResult,
    OaipayProgressEvent,
    TurnstileToken,
)
from tests.support import oaipay_fixtures as fx
from tests.support.fake_http import FakeAsyncSession

_EMAIL = "user@example.com"
_ACCESS = "eyJhbGciOiJSUzI1NiJ9.PAYLOAD.SIG_OAIPAY_HANDLER"
_ACCOUNT_LINE = f"{_EMAIL}|{_ACCESS}"
_YES_KEY = "YESCAPTCHA_KEY_SECRET_xyz"
_CHECKOUT = "socks5://proxyuser:proxypass@host:1080"
_PROMOTION = "socks5://proxyuser:proxypass@host:1081"


class _FakeClient:
    def __init__(
        self,
        *,
        config: CaptchaConfig | None = None,
        stream_result: LongLinkResult | None = None,
        stream_exc: BaseException | None = None,
        stream_sleep: float | None = None,
    ) -> None:
        self.config = config or CaptchaConfig(provider="local", site_key=None)
        self.stream_result = stream_result
        self.stream_exc = stream_exc
        self.stream_sleep = stream_sleep
        self.config_calls = 0
        self.stream_calls = 0
        self.token_info_calls = 0
        self.session = None
        self.last_user_agent: str | None = None

    async def get_captcha_config(self) -> CaptchaConfig:
        self.config_calls += 1
        return self.config

    async def token_info(self, *a, **k):
        self.token_info_calls += 1
        return {"ok": True}

    async def long_link_stream(self, *a, user_agent=None, on_progress=None, **k):
        self.stream_calls += 1
        self.last_user_agent = user_agent
        if self.stream_sleep:
            await asyncio.sleep(self.stream_sleep)
        if self.stream_exc:
            raise self.stream_exc
        # Optional: fire progress events before final result (for fail-fast tests).
        progress_events = getattr(self, "progress_events", None) or []
        for ev in progress_events:
            if on_progress is not None:
                on_progress(ev)
        assert self.stream_result is not None
        return self.stream_result


def _ok_result() -> LongLinkResult:
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


async def _settings(tmp_path: Path, overrides: dict | None = None) -> SettingsRepository:
    engine = DbEngine(tmp_path / "settings.db")
    await engine.init_schema()
    settings = SettingsRepository(engine)
    await register_oaipay_namespace(settings)
    base = {
        "oaipay.proxy_checkout": [_CHECKOUT],
        "oaipay.proxy_promotion": [_PROMOTION],
        "oaipay.yescaptcha_api_key": _YES_KEY,
        "oaipay.run_timeout_seconds": 5,
    }
    base.update(overrides or {})
    for k, v in base.items():
        await settings.set(k, v)
    return settings


def _patch_session(monkeypatch: pytest.MonkeyPatch) -> FakeAsyncSession:
    fake = FakeAsyncSession()
    monkeypatch.setattr("app.core.http_client.create_async_client", lambda **kw: fake)
    return fake


def _job(**kw) -> Job:
    return Job(
        job_id=kw.get("job_id", "job-oaipay"),
        payment_method="upi_nocdk",
        account_line=_ACCOUNT_LINE,
        created_at=time.time(),
        cancellation_token=kw.get("token") or SimpleCancellationToken(),
    )


async def _handler(tmp_path: Path, settings, fake: _FakeClient, solver=None):
    cache = AccountSessionCache(settings, tmp_path / "cache")
    cache.apply_settings({"session_cache.enabled": True, "session_cache.ttl_hours": 24})
    logger = logging.getLogger("oaipay.flow.unit")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())

    def factory(session, base_url, secrets, log):
        fake.session = session
        return fake

    return OaipayFlowHandler(
        settings=settings,
        session_cache=cache,
        qr_output_dir=tmp_path / "qr",
        logger_factory=lambda n: logger,
        oaipay_client_factory=factory,
        captcha_solver=solver,
    )


async def test_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch)
    settings = await _settings(tmp_path)
    fake = _FakeClient(stream_result=_ok_result())
    handler = await _handler(tmp_path, settings, fake)
    result = await handler.run(_job(), None)
    assert result.status == JobStatus.QR_READY
    assert result.payment_link == fx.LONG_URL
    assert result.artifact_path
    assert Path(result.artifact_path).is_file()
    assert fake.stream_calls == 1


async def test_local_provider_skips_solver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch)
    settings = await _settings(tmp_path)
    called = {"n": 0}

    async def solver(**kw):
        called["n"] += 1
        return TurnstileToken(token="t", user_agent="ua")

    fake = _FakeClient(
        config=CaptchaConfig(provider="local", site_key=None),
        stream_result=_ok_result(),
    )
    handler = await _handler(tmp_path, settings, fake, solver=solver)
    result = await handler.run(_job(), None)
    assert result.status == JobStatus.QR_READY
    assert called["n"] == 0


async def test_turnstile_empty_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch)
    settings = await _settings(tmp_path, {"oaipay.yescaptcha_api_key": ""})
    fake = _FakeClient(
        config=CaptchaConfig(provider="turnstile", site_key="0x"),
        stream_result=_ok_result(),
    )
    handler = await _handler(tmp_path, settings, fake)
    result = await handler.run(_job(), None)
    assert result.status == JobStatus.ERROR
    assert result.error_code == "oaipay_captcha_error"


async def test_captcha_solve_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch)
    settings = await _settings(tmp_path)
    fake = _FakeClient(
        config=CaptchaConfig(provider="turnstile", site_key="0x"),
        stream_result=_ok_result(),
    )

    async def solver(**kw):
        raise CaptchaSolveError(detail="fail")

    handler = await _handler(tmp_path, settings, fake, solver=solver)
    result = await handler.run(_job(), None)
    assert result.error_code == "oaipay_captcha_error"


async def test_run_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch)
    settings = await _settings(tmp_path)
    fake = _FakeClient(
        stream_result=LongLinkResult(
            ok=False,
            provider_redirect_url=None,
            long_url=None,
            stripe_redirect_url=None,
            cs_id=None,
            upi_intent_id=None,
            upi_expires_at=None,
            fallback=True,
            provider_error="nope",
        )
    )
    handler = await _handler(tmp_path, settings, fake)
    result = await handler.run(_job(), None)
    assert result.error_code == "oaipay_run_failed"


async def test_stream_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch)
    settings = await _settings(tmp_path)
    fake = _FakeClient(stream_exc=StreamError(detail="x"))
    handler = await _handler(tmp_path, settings, fake)
    result = await handler.run(_job(), None)
    assert result.error_code == "oaipay_stream_error"


async def test_already_paid_progress_fails_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OaiPay retries 'User is already paid' — abort as plan=plus (not Free export)."""
    _patch_session(monkeypatch)
    settings = await _settings(tmp_path)
    fake = _FakeClient(stream_result=_ok_result())
    fake.progress_events = [
        OaipayProgressEvent(
            type="progress",
            step=1,
            total=7,
            desc='[第 1 次失败] checkout create failed: {"detail":"User is already paid"}，重试中...',
        ),
        # Would be more retries — must not be delivered because first raises.
        OaipayProgressEvent(type="progress", step=1, total=7, desc="retry 2"),
    ]
    # After raise, stream_result must not be returned as success.
    handler = await _handler(tmp_path, settings, fake)
    result = await handler.run(_job(), None)
    assert result.status == JobStatus.ERROR
    assert result.error_code == "oaipay_already_paid"
    assert result.plan == "plus"
    assert result.error_message
    assert "already paid" in result.error_message.lower()


async def test_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch)
    settings = await _settings(tmp_path, {"oaipay.run_timeout_seconds": 1})
    fake = _FakeClient(stream_result=_ok_result(), stream_sleep=3.0)
    handler = await _handler(tmp_path, settings, fake)
    result = await handler.run(_job(), None)
    assert result.error_code == "oaipay_run_timeout"


async def test_qr_decode_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch)
    settings = await _settings(tmp_path)
    fake = _FakeClient(
        stream_result=LongLinkResult(
            ok=True,
            provider_redirect_url="https://not-a-data-uri",
            long_url=fx.LONG_URL,
            stripe_redirect_url=None,
            cs_id=None,
            upi_intent_id=None,
            upi_expires_at=None,
        )
    )
    handler = await _handler(tmp_path, settings, fake)
    result = await handler.run(_job(), None)
    assert result.error_code == "oaipay_qr_decode_failed"


async def test_empty_pool_before_captcha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch)
    settings = await _settings(tmp_path, {"oaipay.proxy_checkout": []})
    called = {"n": 0}

    async def solver(**kw):
        called["n"] += 1
        return TurnstileToken(token="t", user_agent="ua")

    fake = _FakeClient(
        config=CaptchaConfig(provider="turnstile", site_key="0x"),
        stream_result=_ok_result(),
    )
    handler = await _handler(tmp_path, settings, fake, solver=solver)
    result = await handler.run(_job(), None)
    assert result.error_code == "oaipay_config_invalid"
    assert called["n"] == 0
    assert fake.config_calls == 0


async def test_cancel_before_captcha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch)
    settings = await _settings(tmp_path)
    token = SimpleCancellationToken()
    token.cancel()
    called = {"n": 0}

    async def solver(**kw):
        called["n"] += 1
        return TurnstileToken(token="t", user_agent="ua")

    fake = _FakeClient(
        config=CaptchaConfig(provider="turnstile", site_key="0x"),
        stream_result=_ok_result(),
    )
    handler = await _handler(tmp_path, settings, fake, solver=solver)
    result = await handler.run(_job(token=token), None)
    assert result.status == JobStatus.STOPPED
    assert called["n"] == 0


async def test_bad_base_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch)
    settings = await _settings(
        tmp_path, {"oaipay.base_url": "http://evil.example"}
    )
    fake = _FakeClient(stream_result=_ok_result())
    handler = await _handler(tmp_path, settings, fake)
    result = await handler.run(_job(), None)
    assert result.error_code == "oaipay_config_invalid"
    assert fake.stream_calls == 0


async def test_redaction_in_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch)
    settings = await _settings(tmp_path)
    fake = _FakeClient(
        stream_exc=StreamError(detail=f"leak {_ACCESS} {_YES_KEY} proxypass")
    )
    handler = await _handler(tmp_path, settings, fake)
    result = await handler.run(_job(), None)
    assert result.error_message
    assert _ACCESS not in result.error_message
    assert _YES_KEY not in result.error_message
    assert "proxypass" not in result.error_message


async def test_check_plan_status_callable(tmp_path: Path) -> None:
    settings = await _settings(tmp_path)
    fake = _FakeClient(stream_result=_ok_result())
    handler = await _handler(tmp_path, settings, fake)
    assert callable(getattr(handler, "check_plan_status", None))
