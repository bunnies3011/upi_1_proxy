"""Unit tests for `UpiFlowHandler` — error mapping + credit lifecycle.

Each foreseen terminal maps to a stable `error_code` (or STOPPED), and a reserved
credit is restored on every non-success terminal via the `finally` release. The
eligibility precheck runs BEFORE acquire, so an ineligible account reserves
nothing. A vendor credit-specific failure prunes the code via `mark_exhausted`.
The `access_token` value is redacted out of every error message.

Error codes asserted here are the ACTUAL codes from `payments/upi/errors.py`
(e.g. `upi_ineligible`, `upi_run_failed`, `upi_stream_error`, `upi_run_timeout`,
`upi_challenge_error`).
"""

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
from app.payments.upi import register_upi_namespace
from app.payments.upi.errors import ChallengeError
from app.payments.upi.flow import UpiFlowHandler
from app.payments.upi.license_pool import UpiLicensePool
from app.payments.upi.models import (
    AccountCheck,
    Challenge,
    KeyVerify,
    KeyVerifyItem,
    ProgressEvent,
    VendorRunResult,
)
from app.payments.upi.vendor_client import CapybaraClient
from tests.support import upi_vendor_fixtures as fx
from tests.support.fake_http import FakeAsyncSession, FakeResponse, FakeStreamResponse

_EMAIL = "user@example.com"
_ACCESS_TOKEN = "eyJhbGciOiJSUzI1NiJ9.PAYLOAD.SIG_HANDLER_MARKER"
_ACCOUNT_LINE = f"{_EMAIL}|{_ACCESS_TOKEN}"

_BASE_URL = "https://pix.capybara.cv"
_CHALLENGE_URL = f"{_BASE_URL}/api/chatgpt/challenge"
_RUN_URL = f"{_BASE_URL}/api/chatgpt/run"


# ---------------------------------------------------------------------------
# Helpers / test doubles
# ---------------------------------------------------------------------------


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
        settings=None,  # type: ignore[arg-type]
        vendor_client_factory=lambda: _FakeKeyVerifyClient(items),
        refresh_interval_seconds=300.0,
    )
    pool.apply_settings({"upi.license_codes": list(codes_remaining.keys())})
    return pool


def _run_result(
    ok: bool,
    code: str,
    *,
    qr: str | None = None,
    hosted: str | None = None,
) -> VendorRunResult:
    return VendorRunResult(
        ok=ok,
        code=code,
        qr_image_png=qr,
        qr_image_svg=None,
        hosted_url=hosted,
        amount=0,
        currency="INR",
        intent_type="setup_intent",
        expires_at=None,
        elapsed_ms=10,
        attempts=1,
    )


class _FakeVendor:
    """Configurable vendor stand-in for `account_check` / `challenge` / `run`."""

    def __init__(
        self,
        *,
        account_check: AccountCheck | None = None,
        challenge: Challenge | None = None,
        challenge_exc: BaseException | None = None,
        run_result: VendorRunResult | None = None,
        run_exc: BaseException | None = None,
        run_sleep: float | None = None,
    ) -> None:
        self._account_check = account_check
        self._challenge = challenge or Challenge(nonce="n", expires=1, mac="m")
        self._challenge_exc = challenge_exc
        self._run_result = run_result
        self._run_exc = run_exc
        self._run_sleep = run_sleep
        self.account_check_calls = 0

    async def account_check(self, access_token: str) -> AccountCheck:
        self.account_check_calls += 1
        assert self._account_check is not None
        return self._account_check

    async def challenge(self, code: str) -> Challenge:
        if self._challenge_exc is not None:
            raise self._challenge_exc
        return self._challenge

    async def run(self, access_token, code, challenge, *, cancellation_token, on_progress):
        if self._run_sleep is not None:
            await asyncio.sleep(self._run_sleep)
        if on_progress is not None:
            on_progress(ProgressEvent(stage="creating", percent=25, label="init", attempts=1))
        if self._run_exc is not None:
            raise self._run_exc
        assert self._run_result is not None
        return self._run_result


async def _make_settings(tmp_path: Path, overrides: dict | None = None) -> SettingsRepository:
    engine = DbEngine(tmp_path / "settings.db")
    await engine.init_schema()
    settings = SettingsRepository(engine)
    await register_upi_namespace(settings)
    for key, value in (overrides or {}).items():
        await settings.set(key, value)
    return settings


def _make_handler(
    settings: SettingsRepository,
    tmp_path: Path,
    pool: UpiLicensePool,
    vendor_factory,
) -> tuple[UpiFlowHandler, _CapturingHandler]:
    session_cache = AccountSessionCache(settings, tmp_path / "cache")
    session_cache.apply_settings(
        {"session_cache.enabled": True, "session_cache.ttl_hours": 24}
    )
    logger, cap = _logger_with_capture("upi.flow.unit")
    handler = UpiFlowHandler(
        settings=settings,
        session_cache=session_cache,
        qr_output_dir=tmp_path / "qr",
        logger_factory=lambda name: logger,
        license_pool=pool,
        vendor_client_factory=vendor_factory,
    )
    return handler, cap


def _job() -> Job:
    return Job(
        job_id="job-unit-upi",
        payment_method="upi",
        account_line=_ACCOUNT_LINE,
        created_at=time.time(),
        cancellation_token=SimpleCancellationToken(),
    )


def _patch_bare_session(monkeypatch: pytest.MonkeyPatch) -> FakeAsyncSession:
    fake = FakeAsyncSession()
    monkeypatch.setattr("app.core.http_client.create_async_client", lambda **kw: fake)
    return fake


# ---------------------------------------------------------------------------
# Pool exhausted -> upi_no_license_credit (no credit reserved)
# ---------------------------------------------------------------------------


async def test_empty_pool_maps_to_no_license_credit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_bare_session(monkeypatch)
    settings = await _make_settings(tmp_path)
    pool = _make_pool({})  # empty
    vendor = _FakeVendor(run_result=_run_result(True, "upi_qr_ready"))
    handler, _cap = _make_handler(settings, tmp_path, pool, lambda *a: vendor)

    result = await handler.run(_job(), proxy_lease=None)

    assert result.status == JobStatus.ERROR
    assert result.error_code == "upi_no_license_credit"


# ---------------------------------------------------------------------------
# result.ok=false (non-credit) -> upi_run_failed (credit restored, no prune)
# ---------------------------------------------------------------------------


async def test_vendor_business_failure_maps_to_run_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_bare_session(monkeypatch)
    settings = await _make_settings(tmp_path)
    pool = _make_pool({"PK-1": 5})
    vendor = _FakeVendor(run_result=_run_result(False, "upi_create_failed"))
    handler, _cap = _make_handler(settings, tmp_path, pool, lambda *a: vendor)

    result = await handler.run(_job(), proxy_lease=None)

    assert result.status == JobStatus.ERROR
    assert result.error_code == "upi_run_failed"
    # Credit restored (non-committed release) and code NOT pruned.
    assert pool.total_remaining() == 5
    assert "PK-1" in pool._codes  # noqa: SLF001


# ---------------------------------------------------------------------------
# Truncated stream -> upi_stream_error (real vendor client over fake stream)
# ---------------------------------------------------------------------------


async def test_truncated_stream_maps_to_stream_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _patch_bare_session(monkeypatch)
    fake.route(
        "POST", _CHALLENGE_URL, FakeResponse(200, json_body={"nonce": "n", "expires": 1, "mac": "m"})
    )
    fake.stream_route("POST", _RUN_URL, FakeStreamResponse(fx.truncated_chunks()))
    settings = await _make_settings(tmp_path)
    pool = _make_pool({"PK-1": 5})

    def _factory(session, base_url, known_secrets, log):
        return CapybaraClient(session, base_url=base_url, logger=log, known_secrets=known_secrets)

    handler, _cap = _make_handler(settings, tmp_path, pool, _factory)

    result = await handler.run(_job(), proxy_lease=None)

    assert result.status == JobStatus.ERROR
    assert result.error_code == "upi_stream_error"
    assert pool.total_remaining() == 5  # credit restored


# ---------------------------------------------------------------------------
# run exceeds run_timeout_seconds -> upi_run_timeout (TimeoutError mapped)
# ---------------------------------------------------------------------------


async def test_run_timeout_maps_to_timeout_not_escaping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_bare_session(monkeypatch)
    settings = await _make_settings(tmp_path, {"upi.run_timeout_seconds": 1})
    pool = _make_pool({"PK-1": 5})
    vendor = _FakeVendor(run_result=_run_result(True, "upi_qr_ready"), run_sleep=5.0)
    handler, _cap = _make_handler(settings, tmp_path, pool, lambda *a: vendor)

    started = time.monotonic()
    result = await handler.run(_job(), proxy_lease=None)
    elapsed = time.monotonic() - started

    assert result.status == JobStatus.ERROR
    assert result.error_code == "upi_run_timeout"
    assert elapsed < 3.0
    assert pool.total_remaining() == 5  # credit restored


# ---------------------------------------------------------------------------
# Ineligible (precheck on) -> upi_ineligible AND no credit reserved
# ---------------------------------------------------------------------------


async def test_ineligible_precheck_reserves_no_credit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_bare_session(monkeypatch)
    settings = await _make_settings(tmp_path, {"upi.eligibility_precheck": True})
    pool = _make_pool({"PK-1": 5})
    vendor = _FakeVendor(
        account_check=AccountCheck(eligible=False, is_paid=False, plan_type=None),
        run_result=_run_result(True, "upi_qr_ready"),
    )
    handler, _cap = _make_handler(settings, tmp_path, pool, lambda *a: vendor)

    result = await handler.run(_job(), proxy_lease=None)

    assert result.status == JobStatus.ERROR
    assert result.error_code == "upi_ineligible"
    assert vendor.account_check_calls == 1
    # Precheck runs BEFORE acquire → the code was never even verified/reserved.
    # (If acquire ran first, verify would set remaining=5 then release would
    # restore it → total_remaining()==5; staying at 0 proves acquire never ran.)
    assert pool.total_remaining() == 0
    assert pool._codes["PK-1"].in_flight == 0  # noqa: SLF001
    assert pool._codes["PK-1"].last_verified_at is None  # noqa: SLF001


# ---------------------------------------------------------------------------
# Cancel mid-stream -> STOPPED within ~1-2s AND credit restored
# ---------------------------------------------------------------------------


async def test_cancel_mid_stream_returns_stopped_and_restores_credit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _patch_bare_session(monkeypatch)
    fake.route(
        "POST", _CHALLENGE_URL, FakeResponse(200, json_body={"nonce": "n", "expires": 1, "mac": "m"})
    )
    chunks, delays = fx.cancellation_chunks_and_delays(gap_seconds=10.0)
    fake.stream_route("POST", _RUN_URL, FakeStreamResponse(chunks, delays=delays))
    settings = await _make_settings(tmp_path)
    pool = _make_pool({"PK-1": 5})

    def _factory(session, base_url, known_secrets, log):
        return CapybaraClient(session, base_url=base_url, logger=log, known_secrets=known_secrets)

    handler, _cap = _make_handler(settings, tmp_path, pool, _factory)
    job = _job()

    async def _cancel_soon() -> None:
        await asyncio.sleep(0.2)
        job.cancellation_token.cancel()

    started = time.monotonic()
    _cancel_task = asyncio.ensure_future(_cancel_soon())
    result = await asyncio.wait_for(handler.run(job, proxy_lease=None), timeout=5.0)
    elapsed = time.monotonic() - started
    await _cancel_task

    assert result.status == JobStatus.STOPPED
    assert elapsed < 2.0
    assert pool.total_remaining() == 5  # credit restored (committed=False)


# ---------------------------------------------------------------------------
# access_token value never in error_message (redaction)
# ---------------------------------------------------------------------------


async def test_access_token_redacted_in_error_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_bare_session(monkeypatch)
    settings = await _make_settings(tmp_path)
    pool = _make_pool({"PK-1": 5})
    # Simulate a leak: the challenge error detail carries the raw access_token.
    vendor = _FakeVendor(challenge_exc=ChallengeError(detail=_ACCESS_TOKEN))
    handler, _cap = _make_handler(settings, tmp_path, pool, lambda *a: vendor)

    result = await handler.run(_job(), proxy_lease=None)

    assert result.status == JobStatus.ERROR
    assert result.error_code == "upi_challenge_error"
    assert result.error_message is not None
    assert _ACCESS_TOKEN not in result.error_message
    assert "***REDACTED***" in result.error_message
    # Credit reserved by acquire (before challenge) then restored.
    assert pool.total_remaining() == 5


# ---------------------------------------------------------------------------
# Vendor credit-specific failure -> mark_exhausted (prune) + no_license_credit
# ---------------------------------------------------------------------------


async def test_credit_specific_failure_prunes_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_bare_session(monkeypatch)
    settings = await _make_settings(tmp_path)
    pool = _make_pool({"PK-1": 5, "PK-2": 4})
    # First acquire is round-robin → PK-1; it reports a credit-specific failure.
    vendor = _FakeVendor(run_result=_run_result(False, "license_exhausted"))
    handler, _cap = _make_handler(settings, tmp_path, pool, lambda *a: vendor)

    pool_logger = logging.getLogger("app.payments.upi.license_pool")
    pool_logger.setLevel(logging.WARNING)
    pool_logger.handlers.clear()
    pool_logger.propagate = False
    warn_cap = _CapturingHandler()
    pool_logger.addHandler(warn_cap)

    result = await handler.run(_job(), proxy_lease=None)

    assert result.status == JobStatus.ERROR
    assert result.error_code == "upi_no_license_credit"
    # PK-1 pruned via mark_exhausted; a WARNING was logged on removal.
    assert "PK-1" not in pool._codes  # noqa: SLF001
    assert any("PK-1" in m for m in warn_cap.messages)
    # A subsequent acquire skips the pruned code.
    assert await pool.acquire() == "PK-2"


# ---------------------------------------------------------------------------
# QR decode failure -> upi_qr_decode_failed (credit restored)
# ---------------------------------------------------------------------------


async def test_bad_qr_base64_maps_to_qr_decode_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_bare_session(monkeypatch)
    settings = await _make_settings(tmp_path)
    pool = _make_pool({"PK-1": 5})
    vendor = _FakeVendor(
        run_result=_run_result(
            True,
            "upi_qr_ready",
            qr="data:image/png;base64,!!!not-valid-base64!!!",
            hosted="https://payments.stripe.com/upi/x",
        )
    )
    handler, _cap = _make_handler(settings, tmp_path, pool, lambda *a: vendor)

    result = await handler.run(_job(), proxy_lease=None)

    assert result.status == JobStatus.ERROR
    assert result.error_code == "upi_qr_decode_failed"
    assert pool.total_remaining() == 5  # credit restored
