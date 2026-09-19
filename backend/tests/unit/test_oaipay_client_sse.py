"""Unit tests for OaipayClient long-link-stream SSE parser."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from app.core.payment_flow import SimpleCancellationToken
from app.payments.upi_oaipay.errors import (
    RunCancelledError,
    RunFailedError,
    StreamError,
)
from app.payments.upi_oaipay.models import LongLinkResult, OaipayProgressEvent
from app.payments.upi_oaipay.oaipay_client import OaipayClient
from tests.support import oaipay_fixtures as fx
from tests.support.fake_http import FakeAsyncSession, FakeStreamResponse

BASE = "https://oaipay.12001234.xyz"
STREAM_URL = f"{BASE}/api/long-link-stream"
ACCESS = "eyJ.access.TOKEN_MARKER_oaipay_stream"
SOLVER_UA = fx.SOLVER_UA


def _client(session: FakeAsyncSession) -> OaipayClient:
    logger = logging.getLogger("test.oaipay.sse")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return OaipayClient(session, base_url=BASE, logger=logger, known_secrets=[ACCESS])


async def test_happy_path_parses_done() -> None:
    session = FakeAsyncSession()
    session.stream_route(
        "POST", STREAM_URL, FakeStreamResponse(fx.success_sse_chunks())
    )
    events: list[OaipayProgressEvent] = []
    result = await _client(session).long_link_stream(
        ACCESS,
        {"checkout": "socks5://u:p@h:1", "promotion": "socks5://u:p@h:2"},
        turnstile_token="tok",
        user_agent=SOLVER_UA,
        on_progress=events.append,
    )
    assert isinstance(result, LongLinkResult)
    assert result.ok is True
    assert result.provider_redirect_url == fx.QR_DATA_URI
    assert result.long_url == fx.LONG_URL
    assert result.fallback is False
    assert any(e.type == "started" and e.queued == 2 for e in events)
    call = next(c for c in session.calls if c.url == STREAM_URL)
    assert call.headers is not None
    assert call.headers.get("user-agent") == SOLVER_UA
    assert call.timeout is None


async def test_done_ok_false_returned_not_raised() -> None:
    session = FakeAsyncSession()
    body = {
        "type": "done",
        "result": fx.done_result_failed(fallback=True, provider_error="x"),
    }
    chunk = f"data: {json.dumps(body)}\n\n".encode()
    session.stream_route("POST", STREAM_URL, FakeStreamResponse([chunk]))
    result = await _client(session).long_link_stream(
        ACCESS, {"checkout": "a", "promotion": "b"}
    )
    assert result.ok is False
    assert result.fallback is True


async def test_skips_heartbeat_and_non_json() -> None:
    session = FakeAsyncSession()
    parts = (
        ": heartbeat\n"
        "event: ping\n"
        "id: 1\n"
        "data: not-json\n\n"
        + fx.done_event_ok()
    )
    session.stream_route(
        "POST", STREAM_URL, FakeStreamResponse([parts.encode("utf-8")])
    )
    result = await _client(session).long_link_stream(
        ACCESS, {"checkout": "a", "promotion": "b"}
    )
    assert result.ok is True


async def test_multiline_data_framing() -> None:
    session = FakeAsyncSession()
    # Split JSON across two data: lines then blank line.
    payload = json.dumps({"type": "done", "result": fx.done_result_ok()})
    mid = len(payload) // 2
    framed = f"data: {payload[:mid]}\ndata: {payload[mid:]}\n\n"
    session.stream_route(
        "POST", STREAM_URL, FakeStreamResponse([framed.encode("utf-8")])
    )
    result = await _client(session).long_link_stream(
        ACCESS, {"checkout": "a", "promotion": "b"}
    )
    assert result.ok is True


async def test_stream_without_done_raises() -> None:
    session = FakeAsyncSession()
    session.stream_route(
        "POST",
        STREAM_URL,
        FakeStreamResponse([b'data: {"type":"progress","step":1}\n\n']),
    )
    with pytest.raises(StreamError, match="without done"):
        await _client(session).long_link_stream(
            ACCESS, {"checkout": "a", "promotion": "b"}
        )


async def test_non_2xx_raises() -> None:
    session = FakeAsyncSession()
    session.stream_route(
        "POST",
        STREAM_URL,
        FakeStreamResponse([], status_code=502),
    )
    with pytest.raises(StreamError, match="http_502"):
        await _client(session).long_link_stream(
            ACCESS, {"checkout": "a", "promotion": "b"}
        )


async def test_cancel_mid_silent_gap() -> None:
    session = FakeAsyncSession()
    token = SimpleCancellationToken()
    session.stream_route(
        "POST",
        STREAM_URL,
        FakeStreamResponse(
            [b'data: {"type":"progress"}\n\n', b"never"],
            delays=[0.0, 5.0],
        ),
    )

    async def cancel_soon() -> None:
        await asyncio.sleep(0.1)
        token.cancel()

    task = asyncio.create_task(cancel_soon())
    with pytest.raises(RunCancelledError):
        await _client(session).long_link_stream(
            ACCESS,
            {"checkout": "a", "promotion": "b"},
            cancellation_token=token,
        )
    await task


async def test_progress_callback_raise_aborts_without_waiting_stream() -> None:
    """already-paid fail-fast must not wait for OaiPay retry chunks."""
    session = FakeAsyncSession()
    paid = (
        b'data: {"type":"progress","step":1,"total":7,'
        b'"desc":"checkout create failed: User is already paid"}\n\n'
    )
    session.stream_route(
        "POST",
        STREAM_URL,
        FakeStreamResponse(
            [paid, b'data: {"type":"progress","step":1,"desc":"retry"}\n\n'],
            delays=[0.0, 30.0],
        ),
    )

    def _boom(ev: OaipayProgressEvent) -> None:
        if "already paid" in (ev.desc or "").lower():
            raise RunFailedError(detail="User is already paid")

    started = asyncio.get_event_loop().time()
    with pytest.raises(RunFailedError, match="already paid"):
        await _client(session).long_link_stream(
            ACCESS,
            {"checkout": "a", "promotion": "b"},
            on_progress=_boom,
        )
    elapsed = asyncio.get_event_loop().time() - started
    assert elapsed < 2.0, f"abort hung {elapsed:.2f}s waiting for stream"
