"""Integration test end-to-end cho `IdealFlowHandler.run()` — Task 33.1.

**Mục tiêu**: chạy trọn 12 bước của `IdealFlowHandler` với 3 domain thật
(`chatgpt.com` / `api.stripe.com` / `pay.ideal.nl`) đã được mock qua
`FakeAsyncSession`, đầu vào `Job` dạng `email|access_token` (bypass login
pure-HTTP), đầu ra là 1 file QR PNG hợp lệ trên đĩa (magic bytes `\\x89PNG`).

**Validates Requirements**: 1.5, 1.6, 2.1, 2.3, 3.1, 3.2, 4.1, 4.3, 4.7,
5.1, 5.4, 6.2, 7.1, 7.2.

Timeout wall-clock cứng 30s qua `asyncio.wait_for` — nếu `run()` treo →
`TimeoutError` raise ngay lập tức thay vì để CI hang.

Ghi chú:
    - Sau khi migrate httpx → curl_cffi, respx không còn hoạt động (respx
      mock ở tầng `httpx.AsyncTransport`). Thay bằng monkey-patch
      `app.core.http_client.create_async_client` để trả `FakeAsyncSession`
      shared với routes đã đăng ký. Mọi `create_async_client` (main job
      client, pay_ideal_client, follow_redirect fresh client) đều dùng
      chung 1 session này.
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

# ---------------------------------------------------------------------------
# Constants — cố định để routes/assertion khớp CHÍNH XÁC.
# ---------------------------------------------------------------------------

_JOB_ID = "job-e2e-ideal-flow"
_EMAIL = "user@example.com"
_ACCESS_TOKEN = "access_token_xyz"
_ACCOUNT_LINE = f"{_EMAIL}|{_ACCESS_TOKEN}"

_CHECKOUT_SESSION_ID = "cs_test_e2e_123"
_PUBLISHABLE_KEY = "pk_test_e2e_abc"
_ELEMENTS_SESSION_ID = "elements_sess_e2e_zzz"

_REDIRECT_TO_URL = "https://pm-redirects.stripe.com/authorize/acct_e2e/sa_nonce_e2e_xyz"
_ENCODED_TX_URL = "TX_ENC_E2E_ABC"
_SIG_VALUE = "SIG_E2E_XYZ_123"
_IDEAL_LOCATION = f"https://pay.ideal.nl/transactions/{_ENCODED_TX_URL}?sig={_SIG_VALUE}"

_DEFAULT_ISSUER_ID = "RABONL2U"
_ISSUER_DEEPLINK = "https://rabo.example/ideal/qr?token=e2e"

_RUN_WALL_CLOCK_TIMEOUT_SECONDS = 30.0
_PNG_MAGIC_BYTES = b"\x89PNG\r\n\x1a\n"


async def _bootstrap_settings(db_path: Path) -> SettingsRepository:
    engine = DbEngine(db_path)
    await engine.init_schema()
    settings = SettingsRepository(engine)
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


async def test_ideal_flow_end_to_end_qr_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chạy full flow với FakeAsyncSession + assert QR_READY + file PNG hợp lệ."""
    db_path = tmp_path / "settings.db"
    cache_dir = tmp_path / "session_cache"
    qr_output_dir = tmp_path / "qr"

    settings = await _bootstrap_settings(db_path)
    session_cache = AccountSessionCache(settings, cache_dir)
    session_cache.apply_settings({"session_cache.enabled": True, "session_cache.ttl_hours": 24})
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

    # Monkey-patch factory — mọi `create_async_client(...)` trong `flow.py`,
    # `stripe_client.py`, ... đều nhận cùng session này.
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
            f"[timeout] IdealFlowHandler.run() vượt "
            f"{_RUN_WALL_CLOCK_TIMEOUT_SECONDS}s (elapsed={elapsed:.2f}s) "
            "— khả năng có bước treo (retry, sleep, deadlock)."
        ) from exc

    elapsed = time.monotonic() - started_at
    assert elapsed < _RUN_WALL_CLOCK_TIMEOUT_SECONDS

    assert result.status == JobStatus.QR_READY, (
        f"Kỳ vọng JobResult.status=QR_READY, nhận: {result.status!r} "
        f"(error_code={result.error_code!r}, error_message={result.error_message!r})"
    )
    assert result.artifact_path is not None
    assert result.error_code is None
    assert result.error_message is None

    artifact_path = Path(result.artifact_path)
    assert artifact_path.exists()
    assert artifact_path.name == f"{_JOB_ID}.png"
    magic = artifact_path.read_bytes()[:8]
    assert magic == _PNG_MAGIC_BYTES, f"Magic bytes: {magic!r}"
    assert artifact_path.stat().st_size > len(_PNG_MAGIC_BYTES)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v", "-s"])
