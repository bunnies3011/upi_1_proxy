"""UPI handler exposes check_plan_status (Check Plus button)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core.payment_flow import Job, SimpleCancellationToken
from app.core.session_cache import CachedSession
from app.payments.upi.flow import UpiFlowHandler
from app.payments.upi.license_pool import UpiLicensePool


class _FakeSettings:
    async def get(self, key: str) -> Any:
        return None


class _FakeSessionCache:
    def __init__(self, by_key: dict[str, CachedSession | None]) -> None:
        self._by_key = by_key
        self.calls: list[str] = []

    async def get(self, account_key: str) -> CachedSession | None:
        self.calls.append(account_key)
        return self._by_key.get(account_key)


def _job(account_line: str = "user@example.com|pass|otp") -> Job:
    return Job(
        job_id="job-upi-1",
        payment_method="upi",
        account_line=account_line,
        created_at=time.time(),
        cancellation_token=SimpleCancellationToken(),
    )


def _handler(tmp_path: Path, session_cache: _FakeSessionCache) -> UpiFlowHandler:
    pool = UpiLicensePool(
        settings=None,  # type: ignore[arg-type]
        vendor_client_factory=lambda: None,  # type: ignore[arg-type,return-value]
        refresh_interval_seconds=300.0,
    )
    return UpiFlowHandler(
        settings=_FakeSettings(),  # type: ignore[arg-type]
        session_cache=session_cache,  # type: ignore[arg-type]
        qr_output_dir=tmp_path,
        logger_factory=lambda name: __import__("logging").getLogger(name),
        license_pool=pool,
        vendor_client_factory=lambda *a, **k: None,  # type: ignore[return-value]
    )


async def test_check_plan_not_supported_gone_handler_has_method(tmp_path: Path) -> None:
    handler = _handler(tmp_path, _FakeSessionCache({}))
    assert callable(getattr(handler, "check_plan_status", None))


async def test_check_plan_no_session_cached(tmp_path: Path) -> None:
    cache = _FakeSessionCache({})
    handler = _handler(tmp_path, cache)
    result = await handler.check_plan_status(_job())
    assert result["plan"] == "unknown"
    assert result["error"] == "no_session_cached"
    assert result["email"] == "user@example.com"


async def test_check_plan_invalid_account_line(tmp_path: Path) -> None:
    handler = _handler(tmp_path, _FakeSessionCache({}))
    result = await handler.check_plan_status(_job(account_line="not-an-account"))
    assert result["plan"] == "unknown"
    assert "invalid_account_line" in result["error"]


async def test_check_plan_delegates_to_shared_when_session_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    email = "user@example.com"
    key = hashlib.sha256(email.encode()).hexdigest()[:32]
    cached = CachedSession(
        account_key=key,
        payload={
            "email": email,
            "access_token": "tok-abc",
            "cookies": {"oai-did": "did"},
        },
        saved_at=0.0,
    )
    cache = _FakeSessionCache({key: cached})
    handler = _handler(tmp_path, cache)

    async def _fake_check(client, session, logger):
        assert session.email == email
        assert session.access_token == "tok-abc"
        return {
            "plan": "plus",
            "email": email,
            "raw_plan": "entitlement.subscription_plan=plus",
        }

    monkeypatch.setattr("app.payments.upi.flow._check_plan_status_fn", _fake_check)
    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "app.payments.upi.flow.http.create_async_client",
        lambda **kw: fake_client,
    )

    result = await handler.check_plan_status(_job())
    assert result["plan"] == "plus"
    assert result["email"] == email


def test_parse_entitlement_plan_plus() -> None:
    from app.payments._chatgpt.plan_status import parse_entitlement_plan

    data = {
        "accounts": {
            "default": {
                "entitlement": {
                    "subscription_plan": "chatgptplusplan",
                    "has_active_subscription": True,
                    "expires_at": "2026-08-01T00:00:00Z",
                }
            }
        }
    }
    parsed = parse_entitlement_plan(data)
    assert parsed["plan"] == "plus"
    assert parsed["has_active_subscription"] is True


async def test_shared_check_plan_trusts_plus_label_before_active_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.payments._chatgpt.models import SessionBundle
    from app.payments._chatgpt.plan_status import check_plan_status

    class _Response:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.status_code = 200
            self._payload = payload

        def json(self) -> dict[str, Any]:
            return self._payload

    class _Client:
        def __init__(self) -> None:
            self.calls = 0
            self.cookies = _CookieJar()

        async def get(self, url: str, headers: dict[str, str]) -> _Response:
            self.calls += 1
            if self.calls == 1:
                return _Response(
                    {
                        "user": {"email": "user@example.com"},
                        "accessToken": "fresh-token",
                    }
                )
            return _Response(
                {
                    "accounts": {
                        "default": {
                            "entitlement": {
                                "subscription_plan": "plus",
                                "has_active_subscription": False,
                            }
                        }
                    }
                    }
                )

    class _CookieJar:
        def set(self, name: str, value: str, *, domain: str) -> None:
            return None

    session = SessionBundle(
        email="user@example.com",
        access_token="old-token",
        cookies={"oai-did": "did"},
    )
    result = await check_plan_status(
        _Client(),  # type: ignore[arg-type]
        session,
        __import__("logging").getLogger("test"),
    )
    assert result["plan"] == "plus"
    assert result["raw_plan"] == "entitlement.subscription_plan=plus"


async def test_classify_plan_from_session_account_and_jwt() -> None:
    import base64
    import json
    from app.payments._chatgpt.plan_status import classify_plan_from_session

    # Case 1: modern ChatGPT account.planType = plus
    payload_account_plus = {
        "account": {"planType": "plus", "structure": "personal"},
        "user": {"email": "test@plus.com"},
    }
    plan, raw = classify_plan_from_session(payload_account_plus)
    assert plan == "plus"
    assert "account.planType=plus" in raw

    # Case 2: modern ChatGPT account.planType = free
    payload_account_free = {
        "account": {"planType": "free", "structure": "personal"},
        "user": {"email": "test@free.com"},
    }
    plan, raw = classify_plan_from_session(payload_account_free)
    assert plan == "free"
    assert "account.planType=free" in raw

    # Case 3: JWT claims contain chatgpt_plan_type
    jwt_claims = {"https://api.openai.com/auth": {"chatgpt_plan_type": "plus"}}
    token_body = base64.urlsafe_b64encode(json.dumps(jwt_claims).encode()).decode()
    jwt_token = f"eyJhbGciOiJSUzI1NiJ9.{token_body}.sig"
    payload_jwt = {"accessToken": jwt_token}
    plan, raw = classify_plan_from_session(payload_jwt)
    assert plan == "plus"
    assert "jwt.auth.chatgpt_plan_type=plus" in raw


async def test_check_plan_status_detects_plus_when_entitlement_fails_401() -> None:
    """When entitlement returns 401 token_expired, fallback classifies session account.planType."""
    from app.payments._chatgpt.models import SessionBundle
    from app.payments._chatgpt.plan_status import check_plan_status

    class _Response:
        def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict[str, Any]:
            return self._payload

    class _CookieJar:
        def set(self, name: str, value: str, *, domain: str) -> None:
            return None

    class _Client:
        def __init__(self) -> None:
            self.calls = 0
            self.cookies = _CookieJar()

        async def get(self, url: str, headers: dict[str, str]) -> _Response:
            self.calls += 1
            if self.calls == 1:
                # /api/auth/session returns 200 with modern account.planType = plus
                return _Response(
                    200,
                    {
                        "user": {"email": "upgraded@example.com"},
                        "account": {"planType": "plus", "structure": "personal"},
                        "accessToken": "fresh-token",
                    },
                )
            # Entitlement returns 401 token_expired
            return _Response(
                401,
                {"detail": {"code": "token_expired", "message": "Token expired"}},
            )

    session = SessionBundle(
        email="upgraded@example.com",
        access_token="old-token",
        cookies={"oai-did": "did"},
    )
    result = await check_plan_status(
        _Client(),  # type: ignore[arg-type]
        session,
        __import__("logging").getLogger("test"),
    )
    assert result["plan"] == "plus"
    assert "account.planType=plus" in result["raw_plan"]
    assert "error" not in result

