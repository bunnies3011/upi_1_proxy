"""Unit tests for UpiDirectFlowHandler Phase-1 login stub + safety nets."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.db import DbEngine
from app.core.payment_flow import (
    Job,
    JobResult,
    JobStatus,
    ProxyLeaseHealth,
    SimpleCancellationToken,
)
from app.core.proxy_pool import ProxyLease
from app.core.session_cache import AccountSessionCache
from app.core.settings_store import SettingsRepository
from app.payments._chatgpt.errors import ChatgptLoginError
from app.payments._chatgpt.models import SessionBundle
from app.payments.upi_direct import register_upi_direct_namespace
from app.payments.upi_direct.cancel_helpers import await_cancelable
from app.payments.upi_direct.errors import CheckoutError
from app.payments.upi_direct.flow import (
    UpiDirectFlowHandler,
    _is_token_invalidated_checkout,
)
from app.payments.upi_direct.proxy_pools import proxy_secret_values


async def _settings(tmp_path: Path) -> SettingsRepository:
    engine = DbEngine(tmp_path / "settings.db")
    await engine.init_schema()
    settings = SettingsRepository(engine)
    await register_upi_direct_namespace(settings)
    return settings


def _job(line: str = "user@ex.com|password|TOTP") -> Job:
    return Job(
        job_id="job-1",
        payment_method="upi_direct",
        account_line=line,
        created_at=0.0,
        cancellation_token=SimpleCancellationToken(),
    )


def _lease() -> ProxyLease:
    return ProxyLease(
        proxy_id="http://user:s3cret@proxy.example:8080",
        materialized_url="http://user:s3cret@proxy.example:8080",
        leased_at=0.0,
    )


@pytest.fixture
async def handler(tmp_path: Path) -> UpiDirectFlowHandler:
    settings = await _settings(tmp_path)
    cache = AccountSessionCache(settings, tmp_path / "cache")
    return UpiDirectFlowHandler(
        settings=settings,
        session_cache=cache,
        qr_output_dir=tmp_path / "qr",
        logger_factory=logging.getLogger,
    )


async def _seed_proxy_a(handler: UpiDirectFlowHandler) -> None:
    await handler._settings.set(  # noqa: SLF001
        "upi_direct.proxy_checkout",
        ["http://u:p@proxy.example:8080"],
    )


def _mock_post_login_success() -> tuple:
    """Patch checkout→elements path to reach confirm_pending."""
    from app.payments.upi_direct.models import (
        AmountState,
        CheckoutState,
        ElementsState,
        StripeInitState,
    )

    checkout = CheckoutState(
        checkout_session_id="cs_test",
        publishable_key="pk_test",
        processor_entity="openai_llc",
        raw_payload={"checkout_session_id": "cs_test"},
    )
    init = StripeInitState(
        init_checksum="chk",
        config_id="cfg",
        page_id="ppage_1",
        amount=AmountState(0, "inr", "elements_options.amount"),
    )
    elements = ElementsState(session_id="els_1", config_id="ecfg")
    return checkout, init, elements


async def test_login_success_returns_qr_ready_alive(
    handler: UpiDirectFlowHandler,
) -> None:
    await _seed_proxy_a(handler)
    session = SessionBundle(
        email="user@ex.com",
        access_token="resolved-token-abc",
        cookies={},
    )
    checkout, init, elements = _mock_post_login_success()
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=MagicMock())
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "app.payments.upi_direct.flow.http.create_async_client",
            return_value=fake_client,
        ),
        patch(
            "app.payments.upi_direct.flow._resolve_session_fn",
            new=AsyncMock(return_value=session),
        ),
        patch(
            "app.payments.upi_direct.post_login_flow.ChatgptUpiClient.create_checkout",
            new=AsyncMock(return_value=checkout),
        ),
        patch(
            "app.payments.upi_direct.post_login_flow.StripeUpiClient.init",
            new=AsyncMock(return_value=init),
        ),
        patch(
            "app.payments.upi_direct.post_login_flow.StripeUpiClient.elements_sessions",
            new=AsyncMock(return_value=elements),
        ),
        patch(
            "app.payments.upi_direct.post_login_flow.StripeUpiClient.ensure_token_config",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.payments.upi_direct.post_login_flow.run_confirm_approve_qr",
            new=AsyncMock(
                return_value=JobResult(
                    status=JobStatus.QR_READY,
                    artifact_path="/tmp/job.png",
                    proxy_lease_health=ProxyLeaseHealth.ALIVE,
                )
            ),
        ),
    ):
        result = await handler.run(_job(), _lease())

    assert result.status == JobStatus.QR_READY
    assert result.proxy_lease_health == ProxyLeaseHealth.ALIVE


async def test_login_success_direct_no_lease_health(
    handler: UpiDirectFlowHandler,
) -> None:
    await _seed_proxy_a(handler)
    session = SessionBundle(
        email="user@ex.com", access_token="tok", cookies={}
    )
    checkout, init, elements = _mock_post_login_success()
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=MagicMock())
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "app.payments.upi_direct.flow.http.create_async_client",
            return_value=fake_client,
        ),
        patch(
            "app.payments.upi_direct.flow._resolve_session_fn",
            new=AsyncMock(return_value=session),
        ),
        patch(
            "app.payments.upi_direct.post_login_flow.ChatgptUpiClient.create_checkout",
            new=AsyncMock(return_value=checkout),
        ),
        patch(
            "app.payments.upi_direct.post_login_flow.StripeUpiClient.init",
            new=AsyncMock(return_value=init),
        ),
        patch(
            "app.payments.upi_direct.post_login_flow.StripeUpiClient.elements_sessions",
            new=AsyncMock(return_value=elements),
        ),
        patch(
            "app.payments.upi_direct.post_login_flow.StripeUpiClient.ensure_token_config",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.payments.upi_direct.post_login_flow.run_confirm_approve_qr",
            new=AsyncMock(
                return_value=JobResult(
                    status=JobStatus.QR_READY,
                    artifact_path="/tmp/job.png",
                    proxy_lease_health=None,
                )
            ),
        ),
    ):
        result = await handler.run(_job(), None)

    assert result.status == JobStatus.QR_READY
    assert result.proxy_lease_health is None


async def test_login_network_error_marks_dead(
    handler: UpiDirectFlowHandler,
) -> None:
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=MagicMock())
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "app.payments.upi_direct.flow.http.create_async_client",
            return_value=fake_client,
        ),
        patch(
            "app.payments.upi_direct.flow._resolve_session_fn",
            new=AsyncMock(
                side_effect=ChatgptLoginError(reason="network_error")
            ),
        ),
    ):
        result = await handler.run(_job(), _lease())

    assert result.status == JobStatus.ERROR
    assert result.error_code == "login_failed"
    assert result.proxy_lease_health == ProxyLeaseHealth.DEAD


async def test_login_auth_error_does_not_mark_dead(
    handler: UpiDirectFlowHandler,
) -> None:
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=MagicMock())
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "app.payments.upi_direct.flow.http.create_async_client",
            return_value=fake_client,
        ),
        patch(
            "app.payments.upi_direct.flow._resolve_session_fn",
            new=AsyncMock(
                side_effect=ChatgptLoginError(reason="invalid_credential")
            ),
        ),
    ):
        result = await handler.run(_job(), _lease())

    assert result.error_code == "login_failed"
    assert result.proxy_lease_health is None


def _token_invalidated_error() -> CheckoutError:
    error = CheckoutError(detail='http_401:{"code":"token_invalidated"}')
    setattr(error, "status_code", 401)
    setattr(
        error,
        "response_payload",
        {"error": {"code": "token_invalidated"}},
    )
    return error


def test_token_invalidated_classifier_is_narrow() -> None:
    assert _is_token_invalidated_checkout(_token_invalidated_error())

    other_401 = CheckoutError(detail='http_401:{"code":"other"}')
    setattr(other_401, "status_code", 401)
    assert not _is_token_invalidated_checkout(other_401)

    wrong_status = CheckoutError(detail='http_403:{"code":"token_invalidated"}')
    setattr(wrong_status, "status_code", 403)
    assert not _is_token_invalidated_checkout(wrong_status)


async def test_invalidated_cached_token_relogs_and_retries_once(
    tmp_path: Path,
) -> None:
    settings = await _settings(tmp_path)
    cache = AccountSessionCache(settings, tmp_path / "cache")
    stale = SessionBundle(
        email="user@ex.com",
        access_token="stale-token",
        cookies={"session": "stale"},
    )
    fresh = SessionBundle(
        email="user@ex.com",
        access_token="fresh-token",
        cookies={"session": "fresh"},
    )
    sessions_seen: list[SessionBundle] = []

    async def post_login_runner(**kwargs: Any) -> JobResult:
        current = kwargs["session"]
        sessions_seen.append(current)
        if len(sessions_seen) == 1:
            raise _token_invalidated_error()
        return JobResult(status=JobStatus.QR_READY, payment_link="https://example.test")

    handler = UpiDirectFlowHandler(
        settings=settings,
        session_cache=cache,
        qr_output_dir=tmp_path / "qr",
        logger_factory=logging.getLogger,
        post_login_runner=post_login_runner,
    )
    account_key = handler._compute_account_key("user@ex.com")  # noqa: SLF001
    await cache.save(
        account_key,
        {
            "email": stale.email,
            "access_token": stale.access_token,
            "cookies": stale.cookies,
        },
    )

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=MagicMock())
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_login_client = MagicMock()
    fake_login_client.reset_openai_cookies.return_value = 1
    resolve = AsyncMock(side_effect=[stale, fresh])

    with (
        patch(
            "app.payments.upi_direct.flow.http.create_async_client",
            return_value=fake_client,
        ),
        patch(
            "app.payments.upi_direct.flow.ChatgptLoginClient",
            return_value=fake_login_client,
        ),
        patch(
            "app.payments.upi_direct.flow._resolve_session_fn",
            new=resolve,
        ),
    ):
        result = await handler.run(_job(), None)

    assert result.status == JobStatus.QR_READY
    assert sessions_seen == [stale, fresh]
    assert resolve.await_count == 2
    fake_login_client.reset_openai_cookies.assert_called_once()
    cached = await cache.get(account_key)
    assert cached is not None
    assert cached.payload["access_token"] == "fresh-token"


async def test_redaction_hides_password_and_token(
    handler: UpiDirectFlowHandler, caplog: pytest.LogCaptureFixture
) -> None:
    password = "SuperSecretPass99"
    token = "resolved-access-token-xyz"
    session = SessionBundle(email="user@ex.com", access_token=token, cookies={})
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=MagicMock())
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with (
        caplog.at_level(logging.INFO),
        patch(
            "app.payments.upi_direct.flow.http.create_async_client",
            return_value=fake_client,
        ),
        patch(
            "app.payments.upi_direct.flow._resolve_session_fn",
            new=AsyncMock(return_value=session),
        ),
    ):
        await handler.run(_job(f"user@ex.com|{password}|TOTP"), None)

    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert password not in joined
    assert token not in joined


async def test_cancel_during_login_returns_stopped(
    handler: UpiDirectFlowHandler,
) -> None:
    job = _job()

    async def _slow_resolve(**_kwargs: Any) -> SessionBundle:
        await asyncio.sleep(5)
        return SessionBundle(email="u", access_token="t", cookies={})

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=MagicMock())
    fake_client.__aexit__ = AsyncMock(return_value=None)

    async def _cancel_soon() -> None:
        await asyncio.sleep(0.05)
        job.cancellation_token.cancel()

    with (
        patch(
            "app.payments.upi_direct.flow.http.create_async_client",
            return_value=fake_client,
        ),
        patch(
            "app.payments.upi_direct.flow._resolve_session_fn",
            new=_slow_resolve,
        ),
    ):
        cancel_task = asyncio.create_task(_cancel_soon())
        result = await handler.run(job, None)
        await cancel_task

    assert result.status == JobStatus.STOPPED


async def test_timeout_before_login(handler: UpiDirectFlowHandler, tmp_path: Path) -> None:
    await handler._settings.set("upi_direct.run_timeout_seconds", 5)  # noqa: SLF001

    async def _slow_resolve(**_kwargs: Any) -> SessionBundle:
        await asyncio.sleep(10)
        return SessionBundle(email="u", access_token="t", cookies={})

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=MagicMock())
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "app.payments.upi_direct.flow.http.create_async_client",
            return_value=fake_client,
        ),
        patch(
            "app.payments.upi_direct.flow._resolve_session_fn",
            new=_slow_resolve,
        ),
    ):
        result = await handler.run(_job(), None)

    assert result.status == JobStatus.ERROR
    assert result.error_code == "upi_direct_run_timeout"


async def test_await_cancelable_prefers_user_cancel() -> None:
    token = SimpleCancellationToken()
    job = Job(
        job_id="j",
        payment_method="upi_direct",
        account_line="a|b",
        created_at=0.0,
        cancellation_token=token,
    )

    async def _slow() -> str:
        await asyncio.sleep(2)
        return "ok"

    async def _cancel() -> None:
        await asyncio.sleep(0.05)
        token.cancel()

    t = asyncio.create_task(_cancel())
    with pytest.raises(asyncio.CancelledError):
        await await_cancelable(job, _slow())
    await t


def test_proxy_secret_values_includes_password() -> None:
    secrets = proxy_secret_values(["http://u:p@ss@host:1"])
    # materialize may quote; at least raw line and password fragments present
    assert any("p@ss" in s or "p%40ss" in s or s == "http://u:p@ss@host:1" for s in secrets)
    secrets2 = proxy_secret_values(["host:8080:user:passw0rd"])
    assert "passw0rd" in secrets2


async def test_boundary_no_ideal_import() -> None:
    import ast

    import app.payments.upi_direct as pkg
    import app.payments.upi_direct.flow as flow_mod
    import app.payments.upi_direct.proxy_pools as pools_mod

    for mod in (pkg, flow_mod, pools_mod):
        src = Path(mod.__file__).read_text(encoding="utf-8")  # type: ignore[arg-type]
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("app.payments.ideal")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("app.payments.ideal")
