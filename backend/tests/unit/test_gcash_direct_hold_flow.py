from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.payment_flow import Job, JobStatus, SimpleCancellationToken
from app.payments._chatgpt.models import SessionBundle
from app.payments.gcash_direct.browser_qr import (
    GcashBrowserQrResult,
    _is_completion_url,
)
from app.payments.gcash_direct.headless_qr import GcashHeadlessQrResult
from app.payments.gcash_direct.post_login_flow import (
    _pick_confirm_proxy_for_attempt,
    run_post_login,
)
from app.payments.upi_direct.models import ResolvedProxyPools


class _Settings:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = values

    async def get(self, key: str) -> object:
        return self.values.get(key)


class _AsyncClientContext:
    def __init__(self, client: object) -> None:
        self.client = client

    async def __aenter__(self) -> object:
        return self.client

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None


def test_gcash_confirm_blank_checkout_pool_uses_direct_even_with_promo_pool() -> None:
    pick = _pick_confirm_proxy_for_attempt(
        ResolvedProxyPools(
            checkout_lines=[],
            promotion_lines=["proxyb.example:1234:user:pass"],
        ),
        attempt=1,
    )

    assert pick is None


def test_gcash_confirm_checkout_pool_rotates_by_attempt() -> None:
    pools = ResolvedProxyPools(
        checkout_lines=[
            "proxya1.example:1234:user:pass",
            "proxya2.example:1234:user:pass",
        ],
        promotion_lines=["proxyb.example:1234:user:pass"],
    )

    first = _pick_confirm_proxy_for_attempt(pools, attempt=1)
    second = _pick_confirm_proxy_for_attempt(pools, attempt=2)
    third = _pick_confirm_proxy_for_attempt(pools, attempt=3)

    assert first is not None
    assert second is not None
    assert third is not None
    assert first.raw_line.startswith("proxya1.example")
    assert second.raw_line.startswith("proxya2.example")
    assert third.raw_line.startswith("proxya1.example")


@pytest.mark.asyncio
async def test_gcash_headless_qr_still_opens_browser_hold_when_capture_disabled(
    monkeypatch, tmp_path: Path
) -> None:
    import app.payments.gcash_direct.post_login_flow as flow

    browser_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        flow.http,
        "create_async_client",
        lambda **_kwargs: _AsyncClientContext(SimpleNamespace()),
    )
    monkeypatch.setattr(
        flow,
        "_create_gcash_checkout",
        lambda *_args, **_kwargs: _async_return(
            {
                "checkout_session_id": "oaics_test",
                "checkout_provider": "open_ai",
                "processor_entity": "openai_llc",
                "billing_details": {"currency": "PHP"},
                "custom_payment_methods": [{"id": "cpmt_test"}],
                "checkout_state": {"email": "user@example.com"},
            }
        ),
    )
    monkeypatch.setattr(
        flow,
        "_submit_gcash_taxes",
        lambda *_args, **_kwargs: _async_return(
            {"checkout_session": {"amount_total": 0, "currency": "PHP"}}
        ),
    )
    monkeypatch.setattr(
        flow,
        "_update_gcash_checkout_promo",
        lambda *_args, **_kwargs: _async_return(
            {"custom_payment_methods": [{"id": "cpmt_test"}]}
        ),
    )
    monkeypatch.setattr(
        flow,
        "_gcash_confirm_with_error_retries",
        lambda **_kwargs: _async_return({"status": "success"}),
    )
    monkeypatch.setattr(
        flow,
        "_gcash_start_native",
        lambda *_args, **_kwargs: _async_return(
            {
                "next_action": {
                    "url": "https://checkoutshopper-live.adyen.com/checkoutshopper/checkoutPaymentRedirect?redirectData=test"
                }
            }
        ),
    )
    monkeypatch.setattr(
        flow,
        "capture_gcash_headless_qr",
        lambda **_kwargs: _async_return(
            GcashHeadlessQrResult(
                artifact_path=tmp_path / "headless.png",
                payment_link="https://m.gcash/s/headless",
                gcash_page_url="https://m.gcash.com/gcashapp/gcash-merchants-auth/index.html?x=1",
                qr_payload="payload",
                consult_uuid="uuid_test",
                expires_at=123.0,
            )
        ),
    )

    async def fake_browser_qr(**kwargs):
        browser_calls.append(kwargs)
        return GcashBrowserQrResult(
            artifact_path=None,
            final_url="https://m.gcash.com/gcashapp/gcash-merchants-auth/index.html?x=browser",
        )

    monkeypatch.setattr(flow, "capture_gcash_browser_qr", fake_browser_qr)

    job = Job(
        job_id="job_test",
        payment_method="gcash_direct",
        account_line="user@example.com|pass|totp",
        created_at=0,
        cancellation_token=SimpleCancellationToken(),
    )
    session = SessionBundle(
        email="user@example.com",
        access_token="token",
        cookies={"__Secure-next-auth.session-token": "cookie"},
    )
    settings = _Settings(
        {
            "gcash_direct.stripe_request_timeout_seconds": 30,
            "gcash_direct.approve_error_retries": 1,
            "gcash_direct.headless_qr_enabled": True,
            "gcash_direct.headless_poll_seconds": 300,
            "gcash_direct.browser_qr_enabled": True,
            "gcash_direct.browser_qr_timeout_seconds": 60,
            "gcash_direct.browser_headless": True,
            "gcash_direct.browser_qr_capture_enabled": False,
            "gcash_direct.browser_hold_seconds": 300,
            "gcash_direct.browser_hold_max_active": 50,
        }
    )

    async with AsyncExitStack() as stack:
        result = await run_post_login(
            job=job,
            session=session,
            stack=stack,
            logger=logging.LoggerAdapter(logging.getLogger(__name__), {}),
            known_secrets=[],
            health_box=[None],
            pools=ResolvedProxyPools(),
            settings=settings,  # type: ignore[arg-type]
            require_promo=True,
            qr_output_dir=tmp_path,
        )

    assert result.status == JobStatus.QR_READY
    assert result.artifact_path == str(tmp_path / "headless.png")
    assert result.payment_link == (
        "https://m.gcash.com/gcashapp/gcash-merchants-auth/index.html?x=browser"
    )
    assert len(browser_calls) == 1
    assert browser_calls[0]["capture_qr"] is False
    assert browser_calls[0]["payment_link"] == (
        "https://m.gcash.com/gcashapp/gcash-merchants-auth/index.html?x=1"
    )
    assert browser_calls[0]["hold_seconds"] == 300
    assert browser_calls[0]["hold_max_active"] == 50


@pytest.mark.asyncio
async def test_gcash_headless_qr_is_replaced_by_browser_qr_when_capture_enabled(
    monkeypatch, tmp_path: Path
) -> None:
    import app.payments.gcash_direct.post_login_flow as flow

    browser_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        flow.http,
        "create_async_client",
        lambda **_kwargs: _AsyncClientContext(SimpleNamespace()),
    )
    monkeypatch.setattr(
        flow,
        "_create_gcash_checkout",
        lambda *_args, **_kwargs: _async_return(
            {
                "checkout_session_id": "oaics_test",
                "checkout_provider": "open_ai",
                "processor_entity": "openai_llc",
                "billing_details": {"currency": "PHP"},
                "custom_payment_methods": [{"id": "cpmt_test"}],
                "checkout_state": {"email": "user@example.com"},
            }
        ),
    )
    monkeypatch.setattr(
        flow,
        "_submit_gcash_taxes",
        lambda *_args, **_kwargs: _async_return(
            {"checkout_session": {"amount_total": 0, "currency": "PHP"}}
        ),
    )
    monkeypatch.setattr(
        flow,
        "_update_gcash_checkout_promo",
        lambda *_args, **_kwargs: _async_return(
            {"custom_payment_methods": [{"id": "cpmt_test"}]}
        ),
    )
    monkeypatch.setattr(
        flow,
        "_gcash_confirm_with_error_retries",
        lambda **_kwargs: _async_return({"status": "success"}),
    )
    monkeypatch.setattr(
        flow,
        "_gcash_start_native",
        lambda *_args, **_kwargs: _async_return(
            {
                "next_action": {
                    "url": "https://checkoutshopper-live.adyen.com/checkoutshopper/checkoutPaymentRedirect?redirectData=test"
                }
            }
        ),
    )
    monkeypatch.setattr(
        flow,
        "capture_gcash_headless_qr",
        lambda **_kwargs: _async_return(
            GcashHeadlessQrResult(
                artifact_path=tmp_path / "headless.png",
                payment_link="https://m.gcash/s/headless",
                gcash_page_url="https://m.gcash.com/gcashapp/gcash-merchants-auth/index.html?x=1",
                qr_payload="payload",
                consult_uuid="uuid_test",
                expires_at=123.0,
            )
        ),
    )

    async def fake_browser_qr(**kwargs):
        browser_calls.append(kwargs)
        return GcashBrowserQrResult(
            artifact_path=tmp_path / "browser.png",
            final_url="https://m.gcash.com/gcashapp/gcash-merchants-auth/index.html?x=browser",
        )

    monkeypatch.setattr(flow, "capture_gcash_browser_qr", fake_browser_qr)

    job = Job(
        job_id="job_test",
        payment_method="gcash_direct",
        account_line="user@example.com|pass|totp",
        created_at=0,
        cancellation_token=SimpleCancellationToken(),
    )
    session = SessionBundle(
        email="user@example.com",
        access_token="token",
        cookies={"__Secure-next-auth.session-token": "cookie"},
    )
    settings = _Settings(
        {
            "gcash_direct.stripe_request_timeout_seconds": 30,
            "gcash_direct.approve_error_retries": 1,
            "gcash_direct.headless_qr_enabled": True,
            "gcash_direct.headless_poll_seconds": 300,
            "gcash_direct.browser_qr_enabled": True,
            "gcash_direct.browser_qr_timeout_seconds": 60,
            "gcash_direct.browser_headless": True,
            "gcash_direct.browser_qr_capture_enabled": True,
            "gcash_direct.browser_hold_seconds": 300,
            "gcash_direct.browser_hold_max_active": 50,
        }
    )

    async with AsyncExitStack() as stack:
        result = await run_post_login(
            job=job,
            session=session,
            stack=stack,
            logger=logging.LoggerAdapter(logging.getLogger(__name__), {}),
            known_secrets=[],
            health_box=[None],
            pools=ResolvedProxyPools(),
            settings=settings,  # type: ignore[arg-type]
            require_promo=True,
            qr_output_dir=tmp_path,
        )

    assert result.status == JobStatus.QR_READY
    assert result.artifact_path == str(tmp_path / "browser.png")
    assert len(browser_calls) == 1
    assert browser_calls[0]["capture_qr"] is True
    assert browser_calls[0]["payment_link"] == (
        "https://m.gcash.com/gcashapp/gcash-merchants-auth/index.html?x=1"
    )


@pytest.mark.asyncio
async def test_gcash_blank_promo_pool_uses_direct_main_with_checkout_pool_for_confirm(
    monkeypatch, tmp_path: Path
) -> None:
    import app.payments.gcash_direct.post_login_flow as flow

    client_proxies: list[object] = []

    def fake_create_async_client(**kwargs):
        client_proxies.append(kwargs.get("proxy"))
        return _AsyncClientContext(SimpleNamespace())

    monkeypatch.setattr(flow.http, "create_async_client", fake_create_async_client)
    monkeypatch.setattr(
        flow,
        "_create_gcash_checkout",
        lambda *_args, **_kwargs: _async_return(
            {
                "checkout_session_id": "oaics_test",
                "checkout_provider": "open_ai",
                "processor_entity": "openai_llc",
                "billing_details": {"currency": "PHP"},
                "custom_payment_methods": [{"id": "cpmt_test"}],
                "checkout_state": {"email": "user@example.com"},
            }
        ),
    )
    monkeypatch.setattr(
        flow,
        "_submit_gcash_taxes",
        lambda *_args, **_kwargs: _async_return(
            {"checkout_session": {"amount_total": 0, "currency": "PHP"}}
        ),
    )
    monkeypatch.setattr(
        flow,
        "_update_gcash_checkout_promo",
        lambda *_args, **_kwargs: _async_return(
            {"custom_payment_methods": [{"id": "cpmt_test"}]}
        ),
    )
    monkeypatch.setattr(
        flow,
        "_gcash_confirm_with_error_retries",
        lambda **_kwargs: _async_return({"status": "success"}),
    )
    monkeypatch.setattr(
        flow,
        "_gcash_start_native",
        lambda *_args, **_kwargs: _async_return(
            {
                "next_action": {
                    "url": "https://checkoutshopper-live.adyen.com/checkoutshopper/checkoutPaymentRedirect?redirectData=test"
                }
            }
        ),
    )

    job = Job(
        job_id="job_test",
        payment_method="gcash_direct",
        account_line="user@example.com|pass|totp",
        created_at=0,
        cancellation_token=SimpleCancellationToken(),
    )
    session = SessionBundle(
        email="user@example.com",
        access_token="token",
        cookies={"__Secure-next-auth.session-token": "cookie"},
    )
    settings = _Settings(
        {
            "gcash_direct.stripe_request_timeout_seconds": 30,
            "gcash_direct.approve_error_retries": 1,
            "gcash_direct.headless_qr_enabled": False,
            "gcash_direct.browser_qr_enabled": False,
        }
    )

    async with AsyncExitStack() as stack:
        result = await run_post_login(
            job=job,
            session=session,
            stack=stack,
            logger=logging.LoggerAdapter(logging.getLogger(__name__), {}),
            known_secrets=[],
            health_box=[None],
            pools=ResolvedProxyPools(
                checkout_lines=["proxy.example:1234:user:pass"],
                promotion_lines=[],
            ),
            settings=settings,  # type: ignore[arg-type]
            require_promo=True,
            qr_output_dir=tmp_path,
        )

    assert result.status == JobStatus.QR_READY
    assert len(client_proxies) == 1
    assert client_proxies[0] is None


@pytest.mark.asyncio
async def test_gcash_blank_checkout_pool_uses_promo_pool_as_main(
    monkeypatch, tmp_path: Path
) -> None:
    import app.payments.gcash_direct.post_login_flow as flow

    client_proxies: list[object] = []

    def fake_create_async_client(**kwargs):
        client_proxies.append(kwargs.get("proxy"))
        return _AsyncClientContext(SimpleNamespace())

    monkeypatch.setattr(flow.http, "create_async_client", fake_create_async_client)
    monkeypatch.setattr(
        flow,
        "_create_gcash_checkout",
        lambda *_args, **_kwargs: _async_return(
            {
                "checkout_session_id": "oaics_test",
                "checkout_provider": "open_ai",
                "processor_entity": "openai_llc",
                "billing_details": {"currency": "PHP"},
                "custom_payment_methods": [{"id": "cpmt_test"}],
                "checkout_state": {"email": "user@example.com"},
            }
        ),
    )
    monkeypatch.setattr(
        flow,
        "_submit_gcash_taxes",
        lambda *_args, **_kwargs: _async_return(
            {"checkout_session": {"amount_total": 0, "currency": "PHP"}}
        ),
    )
    monkeypatch.setattr(
        flow,
        "_update_gcash_checkout_promo",
        lambda *_args, **_kwargs: _async_return(
            {"custom_payment_methods": [{"id": "cpmt_test"}]}
        ),
    )
    monkeypatch.setattr(
        flow,
        "_gcash_confirm_with_error_retries",
        lambda **_kwargs: _async_return({"status": "success"}),
    )
    monkeypatch.setattr(
        flow,
        "_gcash_start_native",
        lambda *_args, **_kwargs: _async_return(
            {
                "next_action": {
                    "url": "https://checkoutshopper-live.adyen.com/checkoutshopper/checkoutPaymentRedirect?redirectData=test"
                }
            }
        ),
    )

    job = Job(
        job_id="job_test",
        payment_method="gcash_direct",
        account_line="user@example.com|pass|totp",
        created_at=0,
        cancellation_token=SimpleCancellationToken(),
    )
    session = SessionBundle(
        email="user@example.com",
        access_token="token",
        cookies={"__Secure-next-auth.session-token": "cookie"},
    )
    settings = _Settings(
        {
            "gcash_direct.stripe_request_timeout_seconds": 30,
            "gcash_direct.approve_error_retries": 1,
            "gcash_direct.headless_qr_enabled": False,
            "gcash_direct.browser_qr_enabled": False,
        }
    )

    async with AsyncExitStack() as stack:
        result = await run_post_login(
            job=job,
            session=session,
            stack=stack,
            logger=logging.LoggerAdapter(logging.getLogger(__name__), {}),
            known_secrets=[],
            health_box=[None],
            pools=ResolvedProxyPools(
                checkout_lines=[],
                promotion_lines=["proxyb.example:1234:user:pass"],
            ),
            settings=settings,  # type: ignore[arg-type]
            require_promo=True,
            qr_output_dir=tmp_path,
        )

    assert result.status == JobStatus.QR_READY
    assert len(client_proxies) == 1
    assert client_proxies[0]


def test_gcash_browser_completion_url_is_chatgpt_success_only() -> None:
    assert _is_completion_url("https://chatgpt.com/payments/success?x=1")
    assert _is_completion_url("https://chatgpt.com/#plus_onboarding")
    assert not _is_completion_url(
        "https://m.gcash.com/gcashapp/gcash-merchants-auth/index.html",
        "Online Payment Linking Successful!",
    )


async def _async_return(value):
    return value
