"""Integration test end-to-end 12-step IdealFlowHandler.run() với FakeAsyncSession.

Kịch bản: submit 1 IdealJob qua `JobManager.submit_batch(...)`, mock TẤT CẢ
endpoint mà 12 bước iDEAL đi qua bằng `FakeAsyncSession` (post migrate httpx
→ curl_cffi). Bypass bước login pure-HTTP: dùng account_line định dạng
2-part `email|access_token` (Requirement 1.1).

Timing đảm bảo test không stuck (<5s): mock trả 2xx ngay lập tức.

_Requirements: 1.5, 1.6, 2.1, 2.3, 3.1, 3.2, 4.1, 4.3, 4.7, 5.1, 5.4, 6.2,
               7.1, 7.2
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.bootstrap import bootstrap_services
from app.core.payment_flow import JobStatus
from tests.support.fake_http import FakeAsyncSession
from tests.support.ideal_routes import register_ideal_happy_path

_CHECKOUT_ID = "cs_test_int_flow_1"
_PUBLISHABLE_KEY = "pk_test_int_flow_1"
_ELEMENTS_SESSION_ID = "es_test_int_flow_1"
_ENCODED_TX_URL = "encoded_tx_int_flow"
_SIG = "sig_int_flow_1"
_ISSUER_ID = "ASNBNL21"
_ISSUER_DEEPLINK = "https://ideal.example.com/qr/asnb"
_REDIRECT_URL_STRIPE = "https://pm-redirects.stripe.com/authorize/acct_test/sa_nonce_test"
_IDEAL_REDIRECT_LOCATION = f"https://pay.ideal.nl/transactions/{_ENCODED_TX_URL}?sig={_SIG}"

_DEVICE_PROFILE_NL: dict = {
    "language": "nl-NL",
    "timeZone": "Europe/Amsterdam",
    "screenWidth": 1920,
    "screenHeight": 1080,
    "screenAvailableWidth": 1920,
    "screenAvailableHeight": 1040,
    "colorDepth": 24,
}

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_TERMINAL_STATES: frozenset[JobStatus] = frozenset(
    {JobStatus.QR_READY, JobStatus.ERROR, JobStatus.STOPPED}
)


async def _wait_for_job_terminal(
    job_manager, job_id: str, timeout_s: float
) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while True:
        record = job_manager.get_job(job_id)
        if record is not None and record.status in _TERMINAL_STATES:
            return
        if loop.time() >= deadline:
            snapshot = (
                None
                if record is None
                else (record.status, record.error_code, record.error_message)
            )
            raise AssertionError(
                f"Job không đạt terminal trong {timeout_s}s: {snapshot!r}"
            )
        await asyncio.sleep(0.05)


async def test_full_flow_end_to_end_qr_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration end-to-end: submit IdealJob → 12-step → QR_READY + file PNG."""
    fake_session = FakeAsyncSession()
    register_ideal_happy_path(
        fake_session,
        checkout_session_id=_CHECKOUT_ID,
        publishable_key=_PUBLISHABLE_KEY,
        elements_session_id=_ELEMENTS_SESSION_ID,
        encoded_tx_url=_ENCODED_TX_URL,
        sig_value=_SIG,
        redirect_to_url=_REDIRECT_URL_STRIPE,
        ideal_location=_IDEAL_REDIRECT_LOCATION,
        default_issuer_id=_ISSUER_ID,
        issuer_deeplink=_ISSUER_DEEPLINK,
        amount={"currency": "EUR", "value": 20},
        creditor_name="OpenAI Ireland",
    )

    monkeypatch.setattr(
        "app.core.http_client.create_async_client",
        lambda **kw: fake_session,
    )

    services = await bootstrap_services(
        db_path=tmp_path / "test.db",
        bind_host="127.0.0.1",
        session_cache_dir=tmp_path / "session_cache",
        qr_output_dir=tmp_path / "qr",
    )
    try:
        await services.settings.set("ideal.default_issuer", _ISSUER_ID)
        await services.settings.set(
            "ideal.device_profiles", [_DEVICE_PROFILE_NL]
        )

        batch = await services.job_manager.submit_batch(
            "ideal",
            ["test@example.com|integration_test_access_token"],
        )
        assert not batch.skipped, (
            f"Không kỳ vọng dòng account bị skip: {batch.skipped}"
        )
        assert len(batch.created_job_ids) == 1
        job_id = batch.created_job_ids[0]

        await _wait_for_job_terminal(services.job_manager, job_id, timeout_s=4.5)

        record = services.job_manager.get_job(job_id)
        assert record is not None
        assert record.status == JobStatus.QR_READY, (
            f"Job không đạt QR_READY: status={record.status}, "
            f"error_code={record.error_code!r}, "
            f"error_message={record.error_message!r}"
        )

        assert record.artifact_path is not None
        artifact_path = Path(record.artifact_path)
        assert artifact_path.exists(), f"File PNG chưa tồn tại: {artifact_path}"

        header = artifact_path.read_bytes()[: len(_PNG_MAGIC)]
        assert header == _PNG_MAGIC, (
            f"File tại {artifact_path} không phải PNG (header={header!r})"
        )
    finally:
        scheduler_task = services.job_manager._scheduler_task  # noqa: SLF001
        if scheduler_task is not None and not scheduler_task.done():
            scheduler_task.cancel()
            try:
                await scheduler_task
            except asyncio.CancelledError:
                pass
        await services.db_engine.close()
