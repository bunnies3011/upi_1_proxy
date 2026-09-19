"""Unit tests for `CapybaraClient` — the UPI vendor client + NDJSON parser.

Covers the four `pix.capybara.cv` endpoints and, above all, the streaming
`run()`: progress events, the final QR result, distinct handling of a business
failure (`result.ok=false`) vs a truncated stream, interrupt-driven cancellation
during a silent gap, and value-based redaction of the `access_token` from every
log/error string (including error paths).

The fake HTTP session (`tests/support/fake_http`) streams caller-supplied byte
chunks — the fixtures split JSON objects across chunk boundaries so the
production line-buffer is proven to survive partial chunks.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from app.core.payment_flow import SimpleCancellationToken
from app.payments.upi.errors import (
    EligibilityError,
    VendorRunCancelled,
    VendorStreamError,
)
from app.payments.upi.models import Challenge, ProgressEvent, VendorRunResult
from app.payments.upi.vendor_client import CapybaraClient
from tests.support import upi_vendor_fixtures as fx
from tests.support.fake_http import FakeAsyncSession, FakeResponse, FakeStreamResponse

BASE_URL = "https://pix.capybara.cv"
RUN_URL = f"{BASE_URL}/api/chatgpt/run"
ACCOUNT_CHECK_URL = f"{BASE_URL}/api/account/check"
KEY_VERIFY_URL = f"{BASE_URL}/api/v1/key/verify"
CHALLENGE_URL = f"{BASE_URL}/api/chatgpt/challenge"
#: The vendor page the client GETs once to prime the `pix_chatgpt_session`
#: cookie required by the JSON/stream endpoints (base_url + the `/dev/upi`
#: referer path).
PAGE_URL = f"{BASE_URL}/dev/upi"
#: The session cookie the page GET hands back; the JSON endpoints reject a
#: request that arrives without it (`errorCode: page_session_required`).
PAGE_SESSION_COOKIE = "pix_chatgpt_session"

# A stand-in ChatGPT access token (JWT-shaped). The signature segment is a unique
# marker the redaction assertions search for.
ACCESS_TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJhdXRoLm9wZW5haS5jb20ifQ.SIG_SECRET_MARKER_9f8e7d6c5b4a"

CODE = "PK-ABCDEF"
CHALLENGE = Challenge(nonce="nonce-xyz", expires=1_800_000_000, mac="mac-deadbeef")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _CapturingHandler(logging.Handler):
    """Collects fully-formatted log messages so tests can scan them for leaks."""

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


def _client(session: FakeAsyncSession, *, name: str = "test.upi.vendor") -> CapybaraClient:
    logger, _ = _logger_with_capture(name)
    return CapybaraClient(session, logger=logger, known_secrets=[ACCESS_TOKEN])


def _collect_progress() -> tuple[list[ProgressEvent], callable]:
    events: list[ProgressEvent] = []

    def on_progress(ev: ProgressEvent) -> None:
        events.append(ev)

    return events, on_progress


# ---------------------------------------------------------------------------
# run() — success
# ---------------------------------------------------------------------------


async def test_run_success_returns_qr_result() -> None:
    session = FakeAsyncSession()
    session.stream_route("POST", RUN_URL, FakeStreamResponse(fx.success_chunks()))
    client = _client(session)
    events, on_progress = _collect_progress()

    result = await client.run(
        ACCESS_TOKEN,
        CODE,
        CHALLENGE,
        cancellation_token=SimpleCancellationToken(),
        on_progress=on_progress,
    )

    assert isinstance(result, VendorRunResult)
    assert result.ok is True
    assert result.code == fx.EXPECTED_RUN_CODE
    assert result.qr_image_png.startswith("data:image/png;base64,")
    assert result.qr_image_png == fx.EXPECTED_QR_DATA_URI
    assert result.hosted_url == fx.EXPECTED_HOSTED_URL
    assert result.currency == "INR"
    assert result.intent_type == "setup_intent"
    assert result.elapsed_ms == 51234


async def test_run_emits_one_progress_event_per_line() -> None:
    session = FakeAsyncSession()
    session.stream_route("POST", RUN_URL, FakeStreamResponse(fx.success_chunks()))
    client = _client(session)
    events, on_progress = _collect_progress()

    await client.run(
        ACCESS_TOKEN,
        CODE,
        CHALLENGE,
        cancellation_token=SimpleCancellationToken(),
        on_progress=on_progress,
    )

    assert [(e.stage, e.percent) for e in events] == [
        (stage, percent) for stage, percent, _, _ in fx.PROGRESS_LINES
    ]
    # First progress line has no label/attempts; a later one does.
    assert events[0].label is None and events[0].attempts is None
    assert events[1].label == "[w1] init..." and events[1].attempts == 1


async def test_run_line_buffer_survives_mid_object_chunk_split() -> None:
    # Tiny chunks guarantee a JSON object is split across two chunks.
    chunks = fx.success_chunks(size=7)
    assert fx.spans_object_boundary(chunks), "fixture must split an object mid-way"
    session = FakeAsyncSession()
    session.stream_route("POST", RUN_URL, FakeStreamResponse(chunks))
    client = _client(session)
    events, on_progress = _collect_progress()

    result = await client.run(
        ACCESS_TOKEN,
        CODE,
        CHALLENGE,
        cancellation_token=SimpleCancellationToken(),
        on_progress=on_progress,
    )

    assert result.ok is True
    assert result.code == fx.EXPECTED_RUN_CODE
    assert len(events) == len(fx.PROGRESS_LINES)


# ---------------------------------------------------------------------------
# run() — failure vs truncation (distinct)
# ---------------------------------------------------------------------------


async def test_run_failure_returns_ok_false_without_raising() -> None:
    session = FakeAsyncSession()
    session.stream_route("POST", RUN_URL, FakeStreamResponse(fx.failure_chunks()))
    client = _client(session)
    events, on_progress = _collect_progress()

    result = await client.run(
        ACCESS_TOKEN,
        CODE,
        CHALLENGE,
        cancellation_token=SimpleCancellationToken(),
        on_progress=on_progress,
    )

    assert isinstance(result, VendorRunResult)
    assert result.ok is False
    assert result.code == "upi_create_failed"
    assert result.qr_image_png is None
    # Progress still surfaced before the failure result.
    assert len(events) == len(fx.PROGRESS_LINES)


async def test_run_truncated_stream_raises_stream_error() -> None:
    session = FakeAsyncSession()
    session.stream_route("POST", RUN_URL, FakeStreamResponse(fx.truncated_chunks()))
    client = _client(session)
    events, on_progress = _collect_progress()

    with pytest.raises(VendorStreamError):
        await client.run(
            ACCESS_TOKEN,
            CODE,
            CHALLENGE,
            cancellation_token=SimpleCancellationToken(),
            on_progress=on_progress,
        )


async def test_run_transport_error_mid_stream_raises_stream_error() -> None:
    session = FakeAsyncSession()
    from app.core import http_client as http

    chunks = fx.success_chunks()
    stream = FakeStreamResponse(
        chunks, raise_at=(1, http.NetworkError("connection reset mid-stream"))
    )
    session.stream_route("POST", RUN_URL, stream)
    client = _client(session)
    events, on_progress = _collect_progress()

    with pytest.raises(VendorStreamError):
        await client.run(
            ACCESS_TOKEN,
            CODE,
            CHALLENGE,
            cancellation_token=SimpleCancellationToken(),
            on_progress=on_progress,
        )


# ---------------------------------------------------------------------------
# run() — interrupt-driven cancellation during a silent gap
# ---------------------------------------------------------------------------


async def test_run_cancellation_during_silent_gap_is_prompt() -> None:
    gap = 10.0  # long silent hold — a poll-between-lines reader would block here.
    chunks, delays = fx.cancellation_chunks_and_delays(gap_seconds=gap)
    session = FakeAsyncSession()
    session.stream_route("POST", RUN_URL, FakeStreamResponse(chunks, delays=delays))
    client = _client(session)

    token = SimpleCancellationToken()

    def on_progress(ev: ProgressEvent) -> None:
        # Cancel right after the first progress line, before the silent gap ends.
        token.cancel()

    started = time.monotonic()
    with pytest.raises(VendorRunCancelled):
        # Outer wall-clock cap far below the 10s gap: if cancellation were only
        # polled between lines, run() would block on the read and wait_for would
        # raise TimeoutError instead of VendorRunCancelled — failing this test.
        await asyncio.wait_for(
            client.run(
                ACCESS_TOKEN,
                CODE,
                CHALLENGE,
                cancellation_token=token,
                on_progress=on_progress,
            ),
            timeout=3.0,
        )
    elapsed = time.monotonic() - started

    assert token.is_cancelled() is True
    assert elapsed < 2.0, f"cancellation took {elapsed:.2f}s (expected interrupt-driven)"


# ---------------------------------------------------------------------------
# JSON endpoints — shape parsing
# ---------------------------------------------------------------------------


async def test_account_check_parses_shape() -> None:
    session = FakeAsyncSession()
    session.route(
        "POST",
        ACCOUNT_CHECK_URL,
        FakeResponse(200, json_body={"eligible": True, "is_paid": True, "plan_type": "plus"}),
    )
    client = _client(session)

    result = await client.account_check(ACCESS_TOKEN)

    assert result.eligible is True
    assert result.is_paid is True
    assert result.plan_type == "plus"
    # The token travelled in the body, not the URL/log.
    call = session.call_for("POST", "/api/account/check")
    assert call.json_body == {"accessToken": ACCESS_TOKEN}


async def test_key_verify_parses_shape() -> None:
    session = FakeAsyncSession()
    session.route(
        "POST",
        KEY_VERIFY_URL,
        FakeResponse(
            200,
            json_body={
                "channel": "upi",
                "items": [
                    {"code": "PK-ABCDEF", "remaining": 42, "total": 45, "valid": True},
                    {"code": "PK-GHIJKL", "remaining": 0, "total": 45, "valid": False},
                ],
                "total": 45,
                "valid": True,
            },
        ),
    )
    client = _client(session)

    result = await client.key_verify(["PK-ABCDEF", "PK-GHIJKL"], channel="upi")

    assert result.channel == "upi"
    assert len(result.items) == 2
    assert result.items[0].code == "PK-ABCDEF"
    assert result.items[0].remaining == 42
    assert result.items[0].valid is True
    assert result.items[1].remaining == 0
    assert result.items[1].valid is False
    call = session.call_for("POST", "/api/v1/key/verify")
    assert call.json_body == {"codes": ["PK-ABCDEF", "PK-GHIJKL"], "channel": "upi"}


async def test_challenge_parses_shape() -> None:
    session = FakeAsyncSession()
    session.route(
        "POST",
        CHALLENGE_URL,
        FakeResponse(
            200,
            json_body={"nonce": "abc123", "expires": 1_800_000_500, "mac": "deadbeefcafe"},
        ),
    )
    client = _client(session)

    result = await client.challenge(CODE)

    assert isinstance(result, Challenge)
    assert result.nonce == "abc123"
    assert result.expires == 1_800_000_500
    assert result.mac == "deadbeefcafe"
    call = session.call_for("POST", "/api/chatgpt/challenge")
    assert call.json_body == {"code": CODE}


# ---------------------------------------------------------------------------
# Redaction — access_token VALUE absent from every log/error string
# ---------------------------------------------------------------------------


async def test_access_token_value_absent_from_logs_on_success() -> None:
    session = FakeAsyncSession()
    session.stream_route("POST", RUN_URL, FakeStreamResponse(fx.success_chunks()))
    logger, handler = _logger_with_capture("test.upi.vendor.redact.success")
    client = CapybaraClient(session, logger=logger, known_secrets=[ACCESS_TOKEN])
    events, on_progress = _collect_progress()

    await client.run(
        ACCESS_TOKEN,
        CODE,
        CHALLENGE,
        cancellation_token=SimpleCancellationToken(),
        on_progress=on_progress,
    )

    joined = "\n".join(handler.messages)
    assert ACCESS_TOKEN not in joined
    assert "SIG_SECRET_MARKER_9f8e7d6c5b4a" not in joined


async def test_access_token_value_redacted_on_error_path() -> None:
    # Vendor echoes the token back inside an error body — the client must not
    # let that value reach logs or the raised exception message.
    session = FakeAsyncSession()
    session.route(
        "POST",
        ACCOUNT_CHECK_URL,
        FakeResponse(
            400,
            text=f'{{"error":"bad token","received":"{ACCESS_TOKEN}"}}',
        ),
    )
    logger, handler = _logger_with_capture("test.upi.vendor.redact.error")
    client = CapybaraClient(session, logger=logger, known_secrets=[ACCESS_TOKEN])

    with pytest.raises(EligibilityError) as exc_info:
        await client.account_check(ACCESS_TOKEN)

    assert ACCESS_TOKEN not in str(exc_info.value)
    joined = "\n".join(handler.messages)
    assert ACCESS_TOKEN not in joined
    assert "SIG_SECRET_MARKER_9f8e7d6c5b4a" not in joined


# ---------------------------------------------------------------------------
# Page-session prime — the client GETs `/dev/upi` once so the vendor's
# `pix_chatgpt_session` cookie is captured and carried to the real endpoints.
# ---------------------------------------------------------------------------


def _page_get_response() -> FakeResponse:
    """A page GET that hands back the session cookie (like the live vendor)."""
    return FakeResponse(
        200,
        text="<html>upi</html>",
        cookies={PAGE_SESSION_COOKIE: "sess-token-abc123"},
    )


def _gated_verify(session: FakeAsyncSession):
    """Verify route that 403s with `page_session_required` UNLESS the page GET
    already deposited `pix_chatgpt_session` into the session cookie jar."""

    def _handler(call) -> FakeResponse:
        if PAGE_SESSION_COOKIE in session.cookies.get_dict():
            return FakeResponse(
                200,
                json_body={
                    "channel": "upi",
                    "items": [
                        {"code": "PK-ABCDEF", "remaining": 10, "total": 10, "valid": True}
                    ],
                    "total": 10,
                    "valid": True,
                },
            )
        return FakeResponse(
            403,
            json_body={
                "error": "请先打开页面后再校验 CDK",
                "errorCode": "page_session_required",
            },
        )

    return _handler


async def test_key_verify_primes_page_session_then_cookie_gates_success() -> None:
    # RED: without the prime GET, the gated verify route returns 403
    # `page_session_required` and key_verify raises LicenseError.
    session = FakeAsyncSession()
    session.route("GET", PAGE_URL, _page_get_response())
    session.route("POST", KEY_VERIFY_URL, _gated_verify(session))
    client = _client(session)

    result = await client.key_verify(["PK-ABCDEF"], channel="upi")

    assert result.valid is True
    assert result.items[0].remaining == 10
    # The page GET was recorded BEFORE the verify POST (prime runs first).
    methods_urls = [(c.method, c.url) for c in session.calls]
    assert ("GET", PAGE_URL) in methods_urls
    assert methods_urls.index(("GET", PAGE_URL)) < methods_urls.index(
        ("POST", KEY_VERIFY_URL)
    )


async def test_page_session_primed_exactly_once_across_two_calls() -> None:
    # RED: two vendor calls on one client must share a single prime GET.
    session = FakeAsyncSession()
    session.route("GET", PAGE_URL, _page_get_response())
    session.route(
        "POST",
        KEY_VERIFY_URL,
        FakeResponse(200, json_body={"channel": "upi", "items": [], "total": 0, "valid": True}),
    )
    session.route(
        "POST",
        CHALLENGE_URL,
        FakeResponse(200, json_body={"nonce": "abc", "expires": 1, "mac": "def"}),
    )
    client = _client(session)

    await client.key_verify(["PK-ABCDEF"])
    await client.challenge(CODE)

    page_gets = [c for c in session.calls if c.method == "GET" and c.url == PAGE_URL]
    assert len(page_gets) == 1, f"expected exactly one prime GET, got {len(page_gets)}"


async def test_key_verify_proceeds_when_prime_transport_fails() -> None:
    # Best-effort: a page GET that raises must NOT introduce a new exception —
    # the real endpoint call still runs and its own result/error is authoritative.
    from app.core import http_client as http

    session = FakeAsyncSession()
    session.route("GET", PAGE_URL, http.NetworkError("prime connection reset"))
    session.route(
        "POST",
        KEY_VERIFY_URL,
        FakeResponse(
            200,
            json_body={
                "channel": "upi",
                "items": [{"code": "PK-ABCDEF", "remaining": 3, "total": 3, "valid": True}],
                "total": 3,
                "valid": True,
            },
        ),
    )
    client = _client(session)

    result = await client.key_verify(["PK-ABCDEF"])

    assert result.valid is True
    assert any(
        c.method == "POST" and c.url == KEY_VERIFY_URL for c in session.calls
    ), "verify POST must still fire after a failed prime"


async def test_failed_prime_is_retried_on_next_call() -> None:
    # RED: a failed prime must NOT be latched — a later call retries it.
    from app.core import http_client as http

    session = FakeAsyncSession()
    session.route("GET", PAGE_URL, http.NetworkError("prime connection reset"))
    session.route(
        "POST",
        KEY_VERIFY_URL,
        FakeResponse(200, json_body={"channel": "upi", "items": [], "total": 0, "valid": True}),
    )
    client = _client(session)

    await client.key_verify(["PK-ABCDEF"])
    await client.key_verify(["PK-ABCDEF"])

    page_gets = [c for c in session.calls if c.method == "GET" and c.url == PAGE_URL]
    assert len(page_gets) == 2, "a failed prime should be retried on the next call"


async def test_prime_non_2xx_does_not_raise() -> None:
    # A non-2xx page GET is best-effort too: logged, swallowed, endpoint proceeds.
    session = FakeAsyncSession()
    session.route("GET", PAGE_URL, FakeResponse(503, text="upstream down"))
    session.route(
        "POST",
        CHALLENGE_URL,
        FakeResponse(200, json_body={"nonce": "abc", "expires": 1, "mac": "def"}),
    )
    client = _client(session)

    result = await client.challenge(CODE)

    assert result.nonce == "abc"


async def test_account_check_primes_page_session_first() -> None:
    session = FakeAsyncSession()
    session.route("GET", PAGE_URL, _page_get_response())
    session.route(
        "POST",
        ACCOUNT_CHECK_URL,
        FakeResponse(200, json_body={"eligible": True, "is_paid": True, "plan_type": "plus"}),
    )
    client = _client(session)

    await client.account_check(ACCESS_TOKEN)

    methods_urls = [(c.method, c.url) for c in session.calls]
    assert methods_urls.index(("GET", PAGE_URL)) < methods_urls.index(
        ("POST", ACCOUNT_CHECK_URL)
    )


async def test_run_primes_page_session_before_opening_stream() -> None:
    session = FakeAsyncSession()
    session.route("GET", PAGE_URL, _page_get_response())
    session.stream_route("POST", RUN_URL, FakeStreamResponse(fx.success_chunks()))
    client = _client(session)
    events, on_progress = _collect_progress()

    result = await client.run(
        ACCESS_TOKEN,
        CODE,
        CHALLENGE,
        cancellation_token=SimpleCancellationToken(),
        on_progress=on_progress,
    )

    assert result.ok is True
    methods_urls = [(c.method, c.url) for c in session.calls]
    assert methods_urls.index(("GET", PAGE_URL)) < methods_urls.index(
        ("POST", RUN_URL)
    )
