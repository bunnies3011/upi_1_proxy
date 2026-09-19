"""R7.7: Backend_Service KHÔNG gọi HTTP request nào sau khi job đạt `qr_ready`.

Sau khi IdealJob render PNG xong + return `JobResult(QR_READY)`, Backend
KHÔNG được poll thanh toán, không refresh, không callback. Mọi tương tác
với ngân hàng do người dùng cuối thực hiện trên app IssuerBank (nằm ngoài
luồng Backend_Service).

Hai tầng defense-in-depth:

    (A) Static structural check: đọc source `flow.py`, verify từ dòng chứa
        `.render_png(` đến `return JobResult(...QR_READY)` KHÔNG có pattern
        gọi HTTP nào.

    (B) Runtime request counting: dùng `FakeAsyncSession`, snapshot
        `len(session.calls)` trước và sau `asyncio.sleep(0.5)` khi `run()`
        đã return QR_READY. Assert bằng nhau.
"""
from __future__ import annotations

import asyncio
import logging
import re
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
# (A) — Static structural check
# ---------------------------------------------------------------------------

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_FLOW_SOURCE_PATH = _BACKEND_ROOT / "app" / "payments" / "ideal" / "flow.py"


def test_no_http_call_between_render_png_and_return_qr_ready() -> None:
    """Từ dòng đầu tiên chứa `.render_png(` đến `return JobResult(...QR_READY)`
    gần nhất, KHÔNG có pattern gọi HTTP client.

    **Validates: Requirements 7.7**
    """
    assert _FLOW_SOURCE_PATH.exists()
    source = _FLOW_SOURCE_PATH.read_text(encoding="utf-8")

    render_match = re.search(r"\.render_png\s*\(", source)
    assert render_match is not None

    render_end = source.index(")", render_match.end())
    tail = source[render_end + 1:]

    return_match = re.search(r"return\s+JobResult\s*\(", tail)
    assert return_match is not None

    between_render_and_return = tail[: return_match.start()]

    forbidden_patterns = [
        r"http_client\.(get|post|put|delete|request)\s*\(",
        r"chatgpt_client\.(create_checkout|approve|login|revalidate)\s*\(",
        r"stripe_client\.(init|elements_sessions|confirm|refresh_poll|follow_redirect)\s*\(",
        r"transaction_client\.initiate\s*\(",
        r"self\._http_client\.",
    ]

    violations: list[tuple[str, str]] = []
    for pattern in forbidden_patterns:
        for match in re.finditer(pattern, between_render_and_return):
            context_start = max(0, match.start() - 30)
            context_end = min(len(between_render_and_return), match.end() + 30)
            snippet = between_render_and_return[context_start:context_end].replace("\n", " ")
            violations.append((pattern, snippet))

    assert not violations, (
        "R7.7 cấm Backend_Service gọi thêm request sau khi đạt qr_ready. "
        "Phát hiện HTTP call giữa `render_png(...)` và `return JobResult"
        "(...QR_READY)` trong `flow.py`:\n"
        + "\n".join(
            f"  pattern={pat!r} @ ...{snippet!r}..."
            for pat, snippet in violations
        )
    )


# ---------------------------------------------------------------------------
# (B) — Runtime request counting với FakeAsyncSession
# ---------------------------------------------------------------------------

_JOB_ID = "job-35-4-no-request-after-qr-ready"
_ACCOUNT_LINE = "user@example.com|access_token_dummy_35_4"

_CHECKOUT_SESSION_ID = "cs_35_4"
_PUBLISHABLE_KEY = "pk_35_4"
_ELEMENTS_SESSION_ID = "es_35_4"

_REDIRECT_TO_URL = "https://pm-redirects.stripe.com/authorize/acct_35_4/sa_nonce_1"
_ENCODED_TX_URL = "TX_ENC_35_4"
_SIG_VALUE = "SIG_35_4"
_IDEAL_LOCATION = f"https://pay.ideal.nl/transactions/{_ENCODED_TX_URL}?sig={_SIG_VALUE}"
_DEFAULT_ISSUER_ID = "ASNBNL21"
_ISSUER_DEEPLINK = "https://asnb.example/ideal/qr?token=35_4"

_RUN_WALL_CLOCK_TIMEOUT_SECONDS = 15.0
_POST_QR_READY_SLEEP_SECONDS = 0.5
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


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


async def test_no_http_requests_after_qr_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Snapshot số HTTP request trước và sau `asyncio.sleep(0.5)` khi
    `run()` return QR_READY — assert bằng nhau (R7.7).

    **Validates: Requirements 7.7**
    """
    db_path = tmp_path / "settings.db"
    cache_dir = tmp_path / "session_cache"
    qr_output_dir = tmp_path / "qr"

    settings = await _bootstrap_settings(db_path)
    session_cache = AccountSessionCache(settings, cache_dir)
    session_cache.apply_settings(
        {"session_cache.enabled": True, "session_cache.ttl_hours": 24}
    )

    handler = IdealFlowHandler(
        settings=settings,
        session_cache=session_cache,
        profile_generator=IdealProfileGenerator(),
        device_profile_allocator=DeviceProfileAllocator(settings),
        issuer_selector=IssuerSelector(),
        qr_renderer=QrRenderer(),
        logger_factory=logging.getLogger,
        qr_output_dir=qr_output_dir,
    )

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
            f"[timeout] {_RUN_WALL_CLOCK_TIMEOUT_SECONDS}s (elapsed={elapsed:.2f}s)."
        ) from exc

    assert result.status == JobStatus.QR_READY, (
        f"Flow phải đạt QR_READY. Nhận: {result!r}"
    )
    assert result.artifact_path is not None
    artifact_path = Path(result.artifact_path)
    assert artifact_path.exists()
    header = artifact_path.read_bytes()[: len(_PNG_MAGIC)]
    assert header == _PNG_MAGIC, (
        f"File tại {artifact_path} không phải PNG (header={header!r})"
    )

    calls_before = len(fake_session.calls)
    assert calls_before >= 8, (
        f"Happy-path phải phát ≥ 8 request. Nhận: {calls_before}"
    )

    await asyncio.sleep(_POST_QR_READY_SLEEP_SECONDS)

    calls_after = len(fake_session.calls)
    if calls_after != calls_before:
        extra_urls = [c.url for c in fake_session.calls[calls_before:]]
        raise AssertionError(
            f"R7.7 VIOLATED: {calls_after - calls_before} request THÊM "
            f"sau qr_ready. before={calls_before}, after={calls_after}. "
            f"URL thừa: {extra_urls!r}"
        )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
