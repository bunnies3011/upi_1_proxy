"""YesCaptcha Turnstile solver for OaiPay (`payments/upi_oaipay/`)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Awaitable, Callable

from app.core import http_client as http
from app.core.redaction import redact_message
from app.payments.upi_oaipay.errors import CaptchaSolveError
from app.payments.upi_oaipay.models import TurnstileToken

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.payment_flow import CancellationToken

_DEFAULT_CREATE_URL = "https://api.yescaptcha.com/createTask"
_DEFAULT_RESULT_URL = "https://api.yescaptcha.com/getTaskResult"
_DEFAULT_ACTION = "generate_long_link"
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


async def solve_turnstile_yescaptcha(
    *,
    session: http.AsyncSession,
    page_url: str,
    site_key: str,
    api_key: str,
    logger: logging.Logger,
    known_secrets: list[str],
    cancellation_token: "CancellationToken | None" = None,
    create_url: str = _DEFAULT_CREATE_URL,
    result_url: str = _DEFAULT_RESULT_URL,
    action: str = _DEFAULT_ACTION,
    poll_interval_seconds: float = 2.0,
    deadline_seconds: float = 120.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], float] = time.monotonic,
) -> TurnstileToken:
    """Create a YesCaptcha Turnstile task, poll until ready, return token + UA."""
    secrets = list(known_secrets)
    if api_key and api_key not in secrets:
        secrets.append(api_key)

    task_body = {
        "clientKey": api_key,
        "task": {
            "type": "TurnstileTaskProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
            "action": action,
        },
    }

    try:
        create_resp = await session.post(create_url, json=task_body)
        create_data = create_resp.json()
    except Exception as exc:  # noqa: BLE001 - map all create failures
        detail = redact_message(f"create_transport: {exc}", secrets)
        raise CaptchaSolveError(detail=detail) from exc

    if not isinstance(create_data, dict):
        raise CaptchaSolveError(detail="create_payload_not_dict")

    if create_data.get("errorId"):
        detail = redact_message(f"create_error: {create_data}", secrets)
        raise CaptchaSolveError(detail=detail)

    task_id = create_data.get("taskId")
    if not task_id:
        detail = redact_message(f"create_no_taskId: {create_data}", secrets)
        raise CaptchaSolveError(detail=detail)

    logger.info("oaipay captcha: task created, polling")

    deadline = now() + deadline_seconds
    while now() < deadline:
        if cancellation_token is not None and cancellation_token.is_cancelled():
            raise CaptchaSolveError(detail="cancelled")

        await sleep(poll_interval_seconds)

        if cancellation_token is not None and cancellation_token.is_cancelled():
            raise CaptchaSolveError(detail="cancelled")

        try:
            result_resp = await session.post(
                result_url,
                json={"clientKey": api_key, "taskId": task_id},
            )
            result_data = result_resp.json()
        except Exception as exc:  # noqa: BLE001
            detail = redact_message(f"result_transport: {exc}", secrets)
            raise CaptchaSolveError(detail=detail) from exc

        if not isinstance(result_data, dict):
            raise CaptchaSolveError(detail="result_payload_not_dict")

        if result_data.get("errorId"):
            detail = redact_message(f"result_error: {result_data}", secrets)
            raise CaptchaSolveError(detail=detail)

        if result_data.get("status") == "ready":
            sol = result_data.get("solution") or {}
            if not isinstance(sol, dict):
                sol = {}
            token = sol.get("token") or sol.get("gRecaptchaResponse") or ""
            ua = sol.get("userAgent") or _DEFAULT_UA
            if not token:
                detail = redact_message(f"ready_empty_token: {result_data}", secrets)
                raise CaptchaSolveError(detail=detail)
            return TurnstileToken(token=str(token), user_agent=str(ua) if ua else None)

    raise CaptchaSolveError(detail="timeout")


__all__ = ["solve_turnstile_yescaptcha"]
