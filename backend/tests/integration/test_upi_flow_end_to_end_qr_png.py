"""Integration test: a UPI job submitted through the real registration + submit
path is driven to `QR_READY` against a faked vendor, and the decoded PNG is then
retrievable via `GET /api/jobs/{job_id}/qr.png`.

This closes the last seam of the plan: the UPI handler reuses the *unchanged*
iDEAL `qr.png` route by setting `JobResult.artifact_path` to the decoded vendor
PNG. It mirrors two existing tests:

    * `tests/integration/test_ideal_flow_full.py` — full `bootstrap_services()` +
      `submit_batch()` + scheduler drive to a terminal state.
    * `tests/unit/test_api_jobs_qr_binary_content.py` — the `qr.png` route's
      binary contract (`image/png`, real PNG bytes, no base64-in-JSON).

Everything vendor-facing is faked (`FakeAsyncSession`): the pool's `key/verify`,
the `challenge`, and the NDJSON `run` stream all resolve against the Phase-2
fixtures, so there is no network and no wall-clock dependency.

A second test drives the *submit/scheduler/stop* seam that Phase 4's
handler-level cancel test does not exercise: a `JobManager.stop()` on a RUNNING
UPI job interrupts the vendor stream mid-gap, ends the job `STOPPED`, writes no
artifact, and restores the reserved license credit.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from pathlib import Path
from typing import Callable

import httpx
import pytest
from fastapi import FastAPI

from app.api import deps
from app.api.routes_jobs import router as jobs_router
from app.bootstrap import BootstrappedServices, bootstrap_services
from app.core.payment_flow import JobStatus
from tests.support import upi_vendor_fixtures as fx
from tests.support.fake_http import FakeAsyncSession, FakeResponse, FakeStreamResponse

_EMAIL = "user@example.com"
_ACCESS_TOKEN = "eyJhbGciOiJSUzI1NiJ9.PAYLOAD.SIG_UPI_QR_PNG_E2E_MARKER"
_ACCOUNT_LINE = f"{_EMAIL}|{_ACCESS_TOKEN}"

_BASE_URL = "https://pix.capybara.cv"
_KEY_VERIFY_URL = f"{_BASE_URL}/api/v1/key/verify"
_CHALLENGE_URL = f"{_BASE_URL}/api/chatgpt/challenge"
_RUN_URL = f"{_BASE_URL}/api/chatgpt/run"

_PK_CODE = "PK-E2E-QR"
_START_CREDIT = 5

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_TERMINAL_STATES: frozenset[JobStatus] = frozenset(
    {JobStatus.QR_READY, JobStatus.ERROR, JobStatus.STOPPED}
)


# ---------------------------------------------------------------------------
# Fake vendor wiring
# ---------------------------------------------------------------------------


def _key_verify_response() -> FakeResponse:
    """`key/verify` payload the pool parses into a code with remaining credit."""
    return FakeResponse(
        200,
        json_body={
            "channel": "upi",
            "items": [
                {
                    "code": _PK_CODE,
                    "remaining": _START_CREDIT,
                    "total": _START_CREDIT,
                    "valid": True,
                }
            ],
            "total": _START_CREDIT,
            "valid": True,
        },
    )


def _build_fake_session(run_spec: FakeStreamResponse) -> FakeAsyncSession:
    """A `FakeAsyncSession` wired for the pool + handler vendor calls.

    `run_spec` is the streaming `run` response — a success stream for the QR path
    or a long-silent-gap stream for the cancellation path.
    """
    fake = FakeAsyncSession()
    fake.route("POST", _KEY_VERIFY_URL, _key_verify_response())
    fake.route(
        "POST",
        _CHALLENGE_URL,
        FakeResponse(
            200,
            json_body={"nonce": "nonce-e2e", "expires": 1_800_000_000, "mac": "mac-e2e"},
        ),
    )
    fake.stream_route("POST", _RUN_URL, run_spec)
    return fake


# ---------------------------------------------------------------------------
# Bootstrap / teardown helpers
# ---------------------------------------------------------------------------


async def _seed_upi_pool(services: BootstrappedServices) -> None:
    """List one PK code and re-hydrate the pool (mirrors bootstrap step 7)."""
    await services.settings.set("upi.license_codes", [_PK_CODE])
    services.upi_license_pool.apply_settings(await services.settings.list("upi"))


#: Background tasks `bootstrap_services()` spawns but does not expose on
#: `BootstrappedServices` (proxy preflight probe + Telegram pull-mode polling).
#: They loop until loop teardown, so a test cancels them by name to avoid a
#: "Task was destroyed but it is pending" warning at GC.
_BOOTSTRAP_BACKGROUND_TASK_NAMES: frozenset[str] = frozenset(
    {"proxy-preflight-startup", "telegram-pull-mode-polling"}
)


async def _shutdown(services: BootstrappedServices) -> None:
    """Cancel background tasks then close the DB (mirrors main.py lifespan)."""
    await services.job_manager.shutdown()

    leftover = [
        task
        for task in asyncio.all_tasks()
        if task.get_name() in _BOOTSTRAP_BACKGROUND_TASK_NAMES and not task.done()
    ]
    for task in leftover:
        task.cancel()
    for task in leftover:
        with contextlib.suppress(asyncio.CancelledError):
            await task

    await services.db_engine.close()


async def _wait_for(
    job_manager,
    job_id: str,
    predicate: Callable[[object], bool],
    timeout_s: float,
) -> object:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while True:
        record = job_manager.get_job(job_id)
        if record is not None and predicate(record):
            return record
        if loop.time() >= deadline:
            snapshot = (
                None
                if record is None
                else (record.status, record.error_code, record.error_message)
            )
            raise AssertionError(
                f"predicate not met within {timeout_s}s: {snapshot!r}"
            )
        await asyncio.sleep(0.05)


def _make_route_app(services: BootstrappedServices) -> FastAPI:
    """Minimal app mounting the real jobs router over the bootstrapped manager.

    The router carries no auth (removed) and `qr.png` only reads the in-memory
    `_JobRecord` + streams the file from disk, so a bare app + a dependency
    override exercises the exact production route handler without the full
    lifespan.
    """
    app = FastAPI()
    app.include_router(jobs_router)
    app.dependency_overrides[deps.get_job_manager] = lambda: services.job_manager
    return app


# ---------------------------------------------------------------------------
# Test 1 — full path to QR_READY then GET /api/jobs/{id}/qr.png
# ---------------------------------------------------------------------------


async def test_upi_qr_png_served_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Submit UPI batch → scheduler → vendor QR → `/qr.png` serves the PNG."""
    fake = _build_fake_session(FakeStreamResponse(fx.success_chunks()))
    monkeypatch.setattr(
        "app.core.http_client.create_async_client", lambda **kw: fake
    )

    services = await bootstrap_services(
        db_path=tmp_path / "test.db",
        bind_host="127.0.0.1",
        session_cache_dir=tmp_path / "session_cache",
        qr_output_dir=tmp_path / "qr",
    )
    try:
        await _seed_upi_pool(services)

        batch = await services.job_manager.submit_batch("upi", [_ACCOUNT_LINE])
        assert not batch.skipped, f"unexpected skipped lines: {batch.skipped}"
        assert len(batch.created_job_ids) == 1
        job_id = batch.created_job_ids[0]

        record = await _wait_for(
            services.job_manager,
            job_id,
            lambda r: r.status in _TERMINAL_STATES,
            timeout_s=10.0,
        )
        assert record.status == JobStatus.QR_READY, (
            f"job not QR_READY: status={record.status}, "
            f"error_code={record.error_code!r}, "
            f"error_message={record.error_message!r}"
        )
        assert record.artifact_path is not None
        assert record.payment_link == fx.EXPECTED_HOSTED_URL

        # The credit was committed on success (5 -> 4).
        assert services.upi_license_pool.total_remaining() == _START_CREDIT - 1

        # -- Serve the artifact over the real qr.png route --------------------
        expected_png = base64.b64decode(fx.SMALL_PNG_B64)
        app = _make_route_app(services)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get(f"/api/jobs/{job_id}/qr.png")

        assert response.status_code == 200, (
            f"qr.png returned {response.status_code}: {response.text}"
        )
        assert response.headers["content-type"] == "image/png", (
            f"content-type must be exact 'image/png', got "
            f"{response.headers.get('content-type')!r}"
        )
        assert response.content.startswith(_PNG_MAGIC), (
            f"body must start with PNG magic bytes, got {response.content[:8]!r}"
        )
        # The served bytes are the decoded vendor QR, byte-for-byte.
        assert response.content == expected_png
    finally:
        await _shutdown(services)


# ---------------------------------------------------------------------------
# Test 2 — submit/scheduler/stop seam: cancel mid-stream -> STOPPED
# ---------------------------------------------------------------------------


async def test_upi_cancel_mid_stream_via_submit_path_stops_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`JobManager.stop()` on a RUNNING UPI job interrupts the vendor stream.

    Unlike the Phase-4 handler-level cancel test (which calls `handler.run`
    directly), this drives the full submit → scheduler → `stop` → cleanup seam:
    the job ends `STOPPED`, no artifact file is written, and the reserved license
    credit is restored to the pool.
    """
    chunks, delays = fx.cancellation_chunks_and_delays(gap_seconds=10.0)
    fake = _build_fake_session(FakeStreamResponse(chunks, delays=delays))
    monkeypatch.setattr(
        "app.core.http_client.create_async_client", lambda **kw: fake
    )

    services = await bootstrap_services(
        db_path=tmp_path / "test.db",
        bind_host="127.0.0.1",
        session_cache_dir=tmp_path / "session_cache",
        qr_output_dir=tmp_path / "qr",
    )
    try:
        await _seed_upi_pool(services)

        batch = await services.job_manager.submit_batch("upi", [_ACCOUNT_LINE])
        assert len(batch.created_job_ids) == 1
        job_id = batch.created_job_ids[0]

        # Wait until the job is RUNNING (in the vendor stream's silent gap), then
        # request a stop — the stream read is raced against the cancel token.
        await _wait_for(
            services.job_manager,
            job_id,
            lambda r: r.status == JobStatus.RUNNING,
            timeout_s=5.0,
        )

        started = asyncio.get_event_loop().time()
        await services.job_manager.stop(job_id)

        record = await _wait_for(
            services.job_manager,
            job_id,
            lambda r: r.status in _TERMINAL_STATES,
            timeout_s=3.0,
        )
        elapsed = asyncio.get_event_loop().time() - started

        assert record.status == JobStatus.STOPPED, (
            f"expected STOPPED, got {record.status} "
            f"(error_code={record.error_code!r})"
        )
        # Interrupted well before the 10s vendor gap would have elapsed.
        assert elapsed < 2.0, f"cancel took {elapsed:.2f}s (expected < 2s)"
        assert record.artifact_path is None
        assert not (services.qr_output_dir / f"{job_id}.png").exists()

        # Reserved credit restored (release committed=False): 5 -> 4 -> 5.
        assert services.upi_license_pool.total_remaining() == _START_CREDIT
    finally:
        await _shutdown(services)
