"""Unit tests for YesCaptcha Turnstile solver."""

from __future__ import annotations

import pytest

from app.core.payment_flow import SimpleCancellationToken
from app.payments.upi_oaipay.captcha import solve_turnstile_yescaptcha
from app.payments.upi_oaipay.errors import CaptchaSolveError
from app.payments.upi_oaipay.models import TurnstileToken
from tests.support.fake_http import FakeAsyncSession, FakeResponse

API_KEY = "YESCAPTCHA_SECRET_KEY_abc123"
CREATE_URL = "https://api.yescaptcha.com/createTask"
RESULT_URL = "https://api.yescaptcha.com/getTaskResult"
SOLVER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SolverUA/1.0"


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


async def test_happy_path_returns_token_and_ua(clock: _Clock) -> None:
    session = FakeAsyncSession()
    session.route(
        "POST",
        CREATE_URL,
        FakeResponse(json_body={"errorId": 0, "taskId": "task-1"}),
    )
    session.route(
        "POST",
        RESULT_URL,
        FakeResponse(
            json_body={
                "errorId": 0,
                "status": "ready",
                "solution": {"token": "turnstile-tok", "userAgent": SOLVER_UA},
            }
        ),
    )
    import logging

    logger = logging.getLogger("test.oaipay.captcha")
    result = await solve_turnstile_yescaptcha(
        session=session,
        page_url="https://oaipay.12001234.xyz/",
        site_key="0xsite",
        api_key=API_KEY,
        logger=logger,
        known_secrets=[API_KEY],
        sleep=clock.sleep,
        now=clock.now,
    )
    assert isinstance(result, TurnstileToken)
    assert result.token == "turnstile-tok"
    assert result.user_agent == SOLVER_UA
    create_call = next(c for c in session.calls if c.url == CREATE_URL)
    assert create_call.json_body["clientKey"] == API_KEY
    assert create_call.json_body["task"]["websiteKey"] == "0xsite"
    assert create_call.json_body["task"]["action"] == "generate_long_link"
    assert create_call.json_body["task"]["type"] == "TurnstileTaskProxyless"


async def test_create_error_id_raises(clock: _Clock) -> None:
    session = FakeAsyncSession()
    session.route(
        "POST",
        CREATE_URL,
        FakeResponse(json_body={"errorId": 1, "errorDescription": "bad key " + API_KEY}),
    )
    import logging

    with pytest.raises(CaptchaSolveError) as ei:
        await solve_turnstile_yescaptcha(
            session=session,
            page_url="https://oaipay.12001234.xyz/",
            site_key="0xsite",
            api_key=API_KEY,
            logger=logging.getLogger("t"),
            known_secrets=[API_KEY],
            sleep=clock.sleep,
            now=clock.now,
        )
    assert API_KEY not in str(ei.value)


async def test_timeout_uses_injected_sleep(clock: _Clock) -> None:
    session = FakeAsyncSession()
    session.route(
        "POST",
        CREATE_URL,
        FakeResponse(json_body={"errorId": 0, "taskId": "task-1"}),
    )
    # Always processing — never ready.
    for _ in range(100):
        session.route(
            "POST",
            RESULT_URL,
            FakeResponse(json_body={"errorId": 0, "status": "processing"}),
        )
    import logging

    with pytest.raises(CaptchaSolveError, match="timeout"):
        await solve_turnstile_yescaptcha(
            session=session,
            page_url="https://oaipay.12001234.xyz/",
            site_key="0xsite",
            api_key=API_KEY,
            logger=logging.getLogger("t"),
            known_secrets=[API_KEY],
            poll_interval_seconds=2.0,
            deadline_seconds=5.0,
            sleep=clock.sleep,
            now=clock.now,
        )
    assert clock.sleeps  # used injected sleep, not real wall clock


async def test_empty_token_raises(clock: _Clock) -> None:
    session = FakeAsyncSession()
    session.route(
        "POST",
        CREATE_URL,
        FakeResponse(json_body={"errorId": 0, "taskId": "task-1"}),
    )
    session.route(
        "POST",
        RESULT_URL,
        FakeResponse(
            json_body={"errorId": 0, "status": "ready", "solution": {"token": ""}}
        ),
    )
    import logging

    with pytest.raises(CaptchaSolveError):
        await solve_turnstile_yescaptcha(
            session=session,
            page_url="https://oaipay.12001234.xyz/",
            site_key="0xsite",
            api_key=API_KEY,
            logger=logging.getLogger("t"),
            known_secrets=[API_KEY],
            sleep=clock.sleep,
            now=clock.now,
        )


async def test_cancel_stops_early(clock: _Clock) -> None:
    session = FakeAsyncSession()
    session.route(
        "POST",
        CREATE_URL,
        FakeResponse(json_body={"errorId": 0, "taskId": "task-1"}),
    )
    session.route(
        "POST",
        RESULT_URL,
        FakeResponse(json_body={"errorId": 0, "status": "processing"}),
    )
    token = SimpleCancellationToken()

    async def sleep_and_cancel(seconds: float) -> None:
        clock.sleeps.append(seconds)
        clock.t += seconds
        token.cancel()

    import logging

    with pytest.raises(CaptchaSolveError, match="cancelled"):
        await solve_turnstile_yescaptcha(
            session=session,
            page_url="https://oaipay.12001234.xyz/",
            site_key="0xsite",
            api_key=API_KEY,
            logger=logging.getLogger("t"),
            known_secrets=[API_KEY],
            cancellation_token=token,
            poll_interval_seconds=2.0,
            deadline_seconds=120.0,
            sleep=sleep_and_cancel,
            now=clock.now,
        )
    assert len(clock.sleeps) <= 2
