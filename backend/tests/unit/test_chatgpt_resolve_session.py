"""Characterization tests for the extracted `resolve_session()` free function.

`resolve_session()` was lifted verbatim from `IdealFlowHandler._resolve_session`
into `payments/_chatgpt/session.py`. These tests pin the exact 3-branch
resolution strategy so the extraction is provably behaviour-preserving:

  (a) cache hit + revalidate succeeds  → return cached session, NO login.
  (b) cache miss + password present    → login + persist cache, return session.
  (c) cache miss + direct access_token → SessionBundle(cookies={}), NO login,
      NO cache save.

Plus the skip-revalidate-when-fresh fast path, which must read
`skip_revalidate_if_fresh_hours` off the session cache (not a separate param).
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict
from typing import Any


from app.core.session_cache import CachedSession
from app.payments._chatgpt import SessionBundle, parse_account_line, resolve_session

_LOGGER = logging.getLogger("test.resolve_session")


class _FakeSessionCache:
    """Minimal stand-in for `AccountSessionCache` — records save/clear calls."""

    def __init__(
        self,
        *,
        get_result: CachedSession | None = None,
        skip_revalidate_if_fresh_hours: int = 0,
    ) -> None:
        self._get_result = get_result
        self._skip = skip_revalidate_if_fresh_hours
        self.saved: list[tuple[str, dict[str, Any]]] = []
        self.cleared: list[str] = []

    @property
    def skip_revalidate_if_fresh_hours(self) -> int:
        return self._skip

    async def get(self, account_key: str) -> CachedSession | None:
        return self._get_result

    async def save(self, account_key: str, payload: dict[str, Any]) -> None:
        self.saved.append((account_key, payload))

    async def clear(self, account_key: str) -> None:
        self.cleared.append(account_key)


class _FakeLoginClient:
    """Records interaction so tests can assert login was / was not attempted."""

    def __init__(
        self,
        *,
        revalidate_result: bool = False,
        login_result: SessionBundle | BaseException | None = None,
    ) -> None:
        self._revalidate_result = revalidate_result
        self._login_result = login_result
        self.revalidate_calls: list[SessionBundle] = []
        self.hydrate_calls: list[SessionBundle] = []
        self.reset_count = 0
        self.login_calls: list[tuple[str, str | None, str | None]] = []

    async def revalidate(self, session: SessionBundle) -> bool:
        self.revalidate_calls.append(session)
        return self._revalidate_result

    def hydrate_from_cache(self, session: SessionBundle) -> None:
        self.hydrate_calls.append(session)

    def reset_openai_cookies(self) -> int:
        self.reset_count += 1
        return 0

    async def login(
        self, *, email: str, password: str, totp_secret: str | None
    ) -> SessionBundle:
        self.login_calls.append((email, password, totp_secret))
        if isinstance(self._login_result, BaseException):
            raise self._login_result
        assert self._login_result is not None
        return self._login_result


def _cached(payload: dict[str, Any], *, age_seconds: float = 0.0) -> CachedSession:
    return CachedSession(
        account_key="acct",
        payload=payload,
        saved_at=time.time() - age_seconds,
    )


async def test_cache_hit_revalidate_ok_returns_cached_no_login() -> None:
    """(a) Cache hit + revalidate True → cached session, login never called."""
    parsed = parse_account_line("user@example.com|hunter2|")
    cached_payload = {
        "email": "user@example.com",
        "access_token": "cached_token",
        "cookies": {"cf_clearance": "abc"},
    }
    cache = _FakeSessionCache(
        get_result=_cached(cached_payload), skip_revalidate_if_fresh_hours=0
    )
    login_client = _FakeLoginClient(revalidate_result=True)

    session = await resolve_session(
        login_client=login_client,
        session_cache=cache,
        parsed=parsed,
        account_key="acct",
        logger=_LOGGER,
    )

    assert session == SessionBundle(
        email="user@example.com",
        access_token="cached_token",
        cookies={"cf_clearance": "abc"},
    )
    assert login_client.login_calls == []
    assert login_client.revalidate_calls  # revalidate WAS consulted
    assert cache.saved == []


async def test_cache_hit_fresh_skips_revalidate_and_login() -> None:
    """Fast path: fresh cache (age < skip window) → hydrate, no revalidate,
    no login. Locks the read of `skip_revalidate_if_fresh_hours` off the
    session cache."""
    parsed = parse_account_line("user@example.com|hunter2|")
    cached_payload = {
        "email": "user@example.com",
        "access_token": "cached_token",
        "cookies": {"cf_clearance": "abc"},
    }
    cache = _FakeSessionCache(
        get_result=_cached(cached_payload, age_seconds=60.0),
        skip_revalidate_if_fresh_hours=24,
    )
    login_client = _FakeLoginClient(revalidate_result=False)

    session = await resolve_session(
        login_client=login_client,
        session_cache=cache,
        parsed=parsed,
        account_key="acct",
        logger=_LOGGER,
    )

    assert session.access_token == "cached_token"
    assert login_client.login_calls == []
    assert login_client.revalidate_calls == []  # skipped entirely
    assert login_client.hydrate_calls  # hydrated cookies instead
    assert cache.saved == []


async def test_cache_miss_password_logs_in_and_saves() -> None:
    """(b) Cache miss + password → login called, session persisted to cache."""
    parsed = parse_account_line("user@example.com|hunter2|JBSWY3DPEHPK3PXP")
    logged_in = SessionBundle(
        email="user@example.com",
        access_token="fresh_token",
        cookies={"cf_clearance": "xyz"},
    )
    cache = _FakeSessionCache(get_result=None)
    login_client = _FakeLoginClient(login_result=logged_in)

    session = await resolve_session(
        login_client=login_client,
        session_cache=cache,
        parsed=parsed,
        account_key="acct",
        logger=_LOGGER,
    )

    assert session == logged_in
    assert login_client.login_calls == [
        ("user@example.com", "hunter2", "JBSWY3DPEHPK3PXP")
    ]
    assert cache.saved == [("acct", asdict(logged_in))]


async def test_direct_access_token_no_login_no_cache_save() -> None:
    """(c) 2-part account (email|access_token) → SessionBundle(cookies={}),
    no login, no cache write."""
    parsed = parse_account_line("user@example.com|sk-directtoken123")
    cache = _FakeSessionCache(get_result=None)
    login_client = _FakeLoginClient()

    session = await resolve_session(
        login_client=login_client,
        session_cache=cache,
        parsed=parsed,
        account_key="acct",
        logger=_LOGGER,
    )

    assert session == SessionBundle(
        email="user@example.com",
        access_token="sk-directtoken123",
        cookies={},
    )
    assert login_client.login_calls == []
    assert cache.saved == []
