"""End-to-end test for `UpiFlowHandler.run()` mirroring the iDEAL e2e harness.

Runs the whole UPI flow (parse -> resolve_session (direct access_token) ->
challenge -> run NDJSON stream -> QR decode) against a `FakeAsyncSession`: a real
`CapybaraClient` streams the Phase-2 success fixture, the license pool is loaded
from a fake `key_verify`, and the output is asserted to be a valid PNG on disk
plus the vendor `hosted_url` as the payment link. Vendor progress lines must also
surface through the per-job (SSE) logger.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import pytest

from app.core.db import DbEngine
from app.core.payment_flow import Job, JobResult, JobStatus, SimpleCancellationToken
from app.core.session_cache import AccountSessionCache
from app.core.settings_store import SettingsRepository
from app.payments.upi import register_upi_namespace
from app.payments.upi.flow import UpiFlowHandler
from app.payments.upi.license_pool import UpiLicensePool
from app.payments.upi.models import KeyVerify, KeyVerifyItem
from app.payments.upi.vendor_client import CapybaraClient
from tests.support import upi_vendor_fixtures as fx
from tests.support.fake_http import FakeAsyncSession, FakeResponse, FakeStreamResponse

_JOB_ID = "job-e2e-upi-flow"
_EMAIL = "user@example.com"
_ACCESS_TOKEN = "eyJhbGciOiJSUzI1NiJ9.PAYLOAD.SIG_UPI_E2E_MARKER"
_ACCOUNT_LINE = f"{_EMAIL}|{_ACCESS_TOKEN}"

_BASE_URL = "https://pix.capybara.cv"
_CHALLENGE_URL = f"{_BASE_URL}/api/chatgpt/challenge"
_RUN_URL = f"{_BASE_URL}/api/chatgpt/run"

_PNG_MAGIC_BYTES = b"\x89PNG\r\n\x1a\n"
_RUN_WALL_CLOCK_TIMEOUT_SECONDS = 30.0


class _CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _logger_with_capture(name: str) -> tuple[logging.Logger, _CapturingHandler]:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False
    handler = _CapturingHandler()
    logger.addHandler(handler)
    return logger, handler


class _FakeKeyVerifyClient:
    """Stand-in vendor client for the pool's `key_verify` only."""

    def __init__(self, items_by_code: dict[str, KeyVerifyItem]) -> None:
        self._items_by_code = items_by_code

    async def key_verify(self, codes, channel: str = "upi") -> KeyVerify:
        items = tuple(self._items_by_code[c] for c in codes if c in self._items_by_code)
        total = sum(i.remaining for i in items)
        valid = bool(items) and all(i.valid for i in items)
        return KeyVerify(channel=channel, items=items, total=total, valid=valid)


def _make_pool(codes_remaining: dict[str, int]) -> UpiLicensePool:
    items = {
        code: KeyVerifyItem(code=code, remaining=remaining, total=max(remaining, 1), valid=True)
        for code, remaining in codes_remaining.items()
    }
    pool = UpiLicensePool(
        settings=None,  # type: ignore[arg-type] — apply_settings does not query DB
        vendor_client_factory=lambda: _FakeKeyVerifyClient(items),
        refresh_interval_seconds=300.0,
    )
    pool.apply_settings({"upi.license_codes": list(codes_remaining.keys())})
    return pool


async def _bootstrap_settings(db_path: Path) -> SettingsRepository:
    engine = DbEngine(db_path)
    await engine.init_schema()
    settings = SettingsRepository(engine)
    await register_upi_namespace(settings)
    return settings


async def test_upi_flow_end_to_end_qr_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = await _bootstrap_settings(tmp_path / "settings.db")
    session_cache = AccountSessionCache(settings, tmp_path / "cache")
    session_cache.apply_settings(
        {"session_cache.enabled": True, "session_cache.ttl_hours": 24}
    )
    qr_output_dir = tmp_path / "qr"
    pool = _make_pool({"PK-E2E": 5})
    logger, cap = _logger_with_capture("upi.flow.e2e")

    def _vendor_factory(session, base_url, known_secrets, log):
        return CapybaraClient(
            session, base_url=base_url, logger=log, known_secrets=known_secrets
        )

    handler = UpiFlowHandler(
        settings=settings,
        session_cache=session_cache,
        qr_output_dir=qr_output_dir,
        logger_factory=lambda name: logger,
        license_pool=pool,
        vendor_client_factory=_vendor_factory,
    )

    fake = FakeAsyncSession()
    fake.route(
        "POST",
        _CHALLENGE_URL,
        FakeResponse(200, json_body={"nonce": "nonce-e2e", "expires": 1_800_000_000, "mac": "mac-e2e"}),
    )
    fake.stream_route("POST", _RUN_URL, FakeStreamResponse(fx.success_chunks()))
    monkeypatch.setattr(
        "app.core.http_client.create_async_client", lambda **kw: fake
    )

    job = Job(
        job_id=_JOB_ID,
        payment_method="upi",
        account_line=_ACCOUNT_LINE,
        created_at=time.time(),
        cancellation_token=SimpleCancellationToken(),
    )

    result: JobResult = await asyncio.wait_for(
        handler.run(job, proxy_lease=None),
        timeout=_RUN_WALL_CLOCK_TIMEOUT_SECONDS,
    )

    assert result.status == JobStatus.QR_READY, (
        f"expected QR_READY, got {result.status!r} "
        f"(error_code={result.error_code!r}, error_message={result.error_message!r})"
    )
    assert result.error_code is None
    assert result.payment_link == fx.EXPECTED_HOSTED_URL

    assert result.artifact_path is not None
    artifact = Path(result.artifact_path)
    assert artifact.exists()
    assert artifact.name == f"{_JOB_ID}.png"
    assert artifact.read_bytes()[:8] == _PNG_MAGIC_BYTES
    assert artifact.stat().st_size > len(_PNG_MAGIC_BYTES)

    # Vendor progress surfaced through the (SSE) logger.
    assert any(m.startswith("[creating] 25%") for m in cap.messages), cap.messages
    assert any(m.startswith("[fetching] 80%") for m in cap.messages), cap.messages

    # The credit was committed on success (5 -> 4, kept).
    assert pool.total_remaining() == 4

    # The access_token value never leaked into a captured log line.
    assert all(_ACCESS_TOKEN not in m for m in cap.messages)
