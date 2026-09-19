"""R2.7: Backend_Service KHÔNG gọi `POST api.stripe.com/v1/consumers/sessions/lookup`.

Endpoint này thuộc Stripe Link consumer sign-in — bước OPTIONAL của Stripe
SDK. Luồng iDEAL pure-HTTP KHÔNG cần, vì:
    - Tốn round-trip mạng không cần thiết.
    - Có nguy cơ tạo Stripe Link consumer session không mong muốn.
    - Vi phạm minimal-API-surface của luồng iDEAL đã tối giản.

Chiến thuật test (sau migrate httpx → curl_cffi):
    1. Dùng `FakeAsyncSession` với `register_ideal_happy_path` — 12-step
       chạy trọn tới QR_READY.
    2. KHÔNG đăng ký route cho `POST /v1/consumers/sessions/lookup`. Nếu
       code có gọi endpoint này → `FakeAsyncSession.request` raise
       `AssertionError` NGAY LẬP TỨC (route không tồn tại).
    3. Đi kèm assert bonus: `JobResult.status == QR_READY` — chứng minh
       endpoint này không cần thiết.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import pytest

from app.core.db import DbEngine
from app.core.payment_flow import (
    Job,
    JobResult,
    JobStatus,
    SimpleCancellationToken,
)
from app.core.session_cache import AccountSessionCache
from app.core.settings_store import SettingsRepository
from app.payments.ideal import register_ideal_namespace
from app.payments.ideal.device_profile import DeviceProfileAllocator
from app.payments.ideal.flow import IdealFlowHandler
from app.payments.ideal.issuer_selector import IssuerSelector
from app.payments.ideal.profile_generator import IdealProfileGenerator
from app.payments.ideal.qr_renderer import QrRenderer
from tests.support.fake_http import FakeAsyncSession
from tests.support.ideal_routes import register_ideal_happy_path

_JOB_ID = "job-no-consumers-lookup"
_EMAIL = "user@example.com"
_ACCESS_TOKEN = "access_token_no_lookup"
_ACCOUNT_LINE = f"{_EMAIL}|{_ACCESS_TOKEN}"

_CHECKOUT_SESSION_ID = "cs_test_no_lookup_123"
_PUBLISHABLE_KEY = "pk_test_no_lookup_abc"
_ELEMENTS_SESSION_ID = "elements_sess_no_lookup_zzz"

_REDIRECT_TO_URL = "https://pm-redirects.stripe.com/authorize/acct_no_lookup/sa_nonce_xyz"
_ENCODED_TX_URL = "TX_ENC_NO_LOOKUP"
_SIG_VALUE = "SIG_NO_LOOKUP_XYZ"
_IDEAL_LOCATION = f"https://pay.ideal.nl/transactions/{_ENCODED_TX_URL}?sig={_SIG_VALUE}"

_DEFAULT_ISSUER_ID = "RABONL2U"
_ISSUER_DEEPLINK = "https://rabo.example/ideal/qr?token=no_lookup"

_RUN_WALL_CLOCK_TIMEOUT_SECONDS = 30.0
_STRIPE_CONSUMERS_LOOKUP_URL = "https://api.stripe.com/v1/consumers/sessions/lookup"


async def _bootstrap_settings(db_path: Path) -> SettingsRepository:
    engine = DbEngine(db_path)
    await engine.init_schema()
    settings = SettingsRepository(engine)
    await settings.set("ui.input_draft", "no-lookup-test-token")
    await register_ideal_namespace(settings)
    await settings.bulk_set(
        {
            "ideal.default_issuer": _DEFAULT_ISSUER_ID,
            "ideal.max_concurrent": 1,
            "ideal.refresh_poll_max_attempts": 3,
            "ideal.refresh_poll_delay_seconds": 0.001,
            "ideal.stripe_request_timeout_seconds": 5.0,
            "ideal.stripe_max_retry_attempts": 1,
            "ideal.stripe_retry_backoff_seconds": 0.001,
            "ideal.device_profiles": [
                {
                    "language": "nl-NL",
                    "timeZone": "Europe/Amsterdam",
                    "screenWidth": 1920,
                    "screenHeight": 1080,
                    "screenAvailableWidth": 1920,
                    "screenAvailableHeight": 1040,
                    "colorDepth": 24,
                }
            ],
        }
    )
    return settings


def _build_handler(
    settings: SettingsRepository,
    session_cache: AccountSessionCache,
    qr_output_dir: Path,
) -> IdealFlowHandler:
    return IdealFlowHandler(
        settings=settings,
        session_cache=session_cache,
        profile_generator=IdealProfileGenerator(),
        device_profile_allocator=DeviceProfileAllocator(settings),
        issuer_selector=IssuerSelector(),
        qr_renderer=QrRenderer(),
        logger_factory=logging.getLogger,
        qr_output_dir=qr_output_dir,
    )


async def test_backend_service_never_calls_stripe_consumers_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chạy trọn 1 lượt `IdealFlowHandler.run()` với FakeAsyncSession chỉ
    đăng ký routes happy-path. Nếu code có gọi `/v1/consumers/sessions/lookup`,
    FakeAsyncSession raise `AssertionError` ngay → test fail rõ ràng.

    **Validates: Requirement 2.7**
    """
    db_path = tmp_path / "settings.db"
    cache_dir = tmp_path / "session_cache"
    qr_output_dir = tmp_path / "qr"

    settings = await _bootstrap_settings(db_path)
    session_cache = AccountSessionCache(settings, cache_dir)
    session_cache.apply_settings(
        {"session_cache.enabled": True, "session_cache.ttl_hours": 24}
    )
    handler = _build_handler(settings, session_cache, qr_output_dir)

    job = Job(
        job_id=_JOB_ID,
        payment_method="ideal",
        account_line=_ACCOUNT_LINE,
        created_at=time.time(),
        cancellation_token=SimpleCancellationToken(),
    )

    fake_session = FakeAsyncSession()
    register_ideal_happy_path(
        fake_session,
        checkout_session_id=_CHECKOUT_SESSION_ID,
        publishable_key=_PUBLISHABLE_KEY,
        elements_session_id=_ELEMENTS_SESSION_ID,
        encoded_tx_url=_ENCODED_TX_URL,
        sig_value=_SIG_VALUE,
        redirect_to_url=_REDIRECT_TO_URL,
        ideal_location=_IDEAL_LOCATION,
        default_issuer_id=_DEFAULT_ISSUER_ID,
        issuer_deeplink=_ISSUER_DEEPLINK,
    )

    monkeypatch.setattr(
        "app.core.http_client.create_async_client",
        lambda **kw: fake_session,
    )

    started_at = time.monotonic()
    try:
        result: JobResult = await asyncio.wait_for(
            handler.run(job, proxy_lease=None),
            timeout=_RUN_WALL_CLOCK_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        elapsed = time.monotonic() - started_at
        raise AssertionError(
            f"[timeout] {_RUN_WALL_CLOCK_TIMEOUT_SECONDS}s (elapsed={elapsed:.2f}s)"
        ) from exc

    # Assert CHÍNH — R2.7: KHÔNG có call nào tới lookup URL.
    lookup_calls = [c for c in fake_session.calls if c.url == _STRIPE_CONSUMERS_LOOKUP_URL]
    assert not lookup_calls, (
        f"R2.7 VIOLATED: Backend_Service đã gọi {_STRIPE_CONSUMERS_LOOKUP_URL} "
        f"{len(lookup_calls)} lần. Endpoint này thuộc Stripe Link consumer "
        f"sign-in, KHÔNG được kích hoạt trong luồng iDEAL pure-HTTP."
    )

    # Assert BONUS — flow vẫn hoàn tất → endpoint lookup optional.
    assert result.status == JobStatus.QR_READY, (
        f"Flow phải đạt QR_READY khi không có endpoint lookup. Nhận: "
        f"status={result.status!r}, error_code={result.error_code!r}, "
        f"error_message={result.error_message!r}"
    )
    assert result.artifact_path is not None


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
