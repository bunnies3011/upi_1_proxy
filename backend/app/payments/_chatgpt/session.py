"""Session resolution for the shared ChatGPT module (`payments/_chatgpt/`).

`resolve_session()` was lifted verbatim from `IdealFlowHandler._resolve_session`
so the iDEAL flow and (later) the UPI flow share one cache→login→direct-token
strategy. Every dependency the method used to read off `self` is now an explicit
keyword parameter; `skip_revalidate_if_fresh_hours` is read off the injected
`session_cache` (single source of truth).

Payment_Module_Boundary: imports only `app.core.*` and `app.payments._chatgpt.*`
— NOTHING from `app.payments.ideal`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict
from typing import Any

from app.core.session_cache import AccountSessionCache
from app.payments._chatgpt.errors import ChatgptLoginError as LoginError
from app.payments._chatgpt.login_client import ChatgptLoginClient
from app.payments._chatgpt.models import ChatgptAccount, SessionBundle


def _safe_build_session_from_cache(
    payload: dict[str, Any],
    logger: logging.Logger,
) -> SessionBundle | None:
    """Build `SessionBundle` from a cached dict, `None` if the shape is corrupt.

    The cache payload from `session_cache.get()` is a dict parsed from JSON by
    `AccountSessionCache` — its shape is not guaranteed to match `SessionBundle`
    (e.g. an old schema, or a hand-edited file). Use try/except instead of assert
    so we fall back to login rather than crashing the job.
    """
    try:
        return SessionBundle(
            email=payload["email"],
            access_token=payload["access_token"],
            cookies=payload["cookies"],
        )
    except (KeyError, TypeError) as exc:
        logger.warning(
            "session_cache payload shape corrupt: %s (ignoring cache)",
            exc,
        )
        return None


async def resolve_session(
    *,
    login_client: ChatgptLoginClient,
    session_cache: AccountSessionCache,
    parsed: ChatgptAccount,
    account_key: str,
    logger: logging.Logger,
) -> SessionBundle:
    """Resolve a `SessionBundle` for the current job (Requirement 1.2, 1.3, 10.4).

    Strategy:
        1. Try `session_cache.get(account_key)`. On hit and
           `login_client.revalidate(cached_session)` == True → use it (R10.4
           happy path). On a fresh-enough cache, skip revalidate entirely.
        2. On cache miss OR revalidate fail → fall back:
             - if `parsed.password` non-empty → `login_client.login(...)` (R1.3),
               then `session_cache.save(account_key, asdict(session))` on success.
             - if `parsed.access_token` non-empty → build
               `SessionBundle(email, access_token, cookies={})` directly (R1.1
               2-part format), NO login, NO cache save (token not verified against
               chatgpt.com so TTL unknown — avoid caching stale tokens).
        3. Neither fallback satisfied → raise `ChatgptLoginError` (defensive;
           parse_account_line should have prevented this).

    Returns:
        A valid `SessionBundle` (non-None). `access_token` is always non-empty.

    Raises:
        ChatgptLoginError: insufficient input (no password nor access_token), or
            propagated from `login_client.login()` when login truly fails.
    """
    # (1) Try cache.
    cached = await session_cache.get(account_key)
    cache_had_hit = False
    if cached is not None:
        cached_session = _safe_build_session_from_cache(cached.payload, logger)
        if cached_session is not None:
            cache_had_hit = True

            # (1a) Skip revalidate when the cache is "fresh" —
            # `session_cache.skip_revalidate_if_fresh_hours` > 0 and the cache age
            # is under that threshold. Saves 1-2 Cloudflare round-trips (valuable
            # with rotating proxies where each request must warm CF on a new IP).
            # If the cache is actually invalid, `create_checkout` fails early and
            # the flow handles it as a domain error — an accepted trade-off to
            # reduce CF challenge burn.
            skip_window_hours = session_cache.skip_revalidate_if_fresh_hours
            if skip_window_hours > 0:
                age_seconds = time.time() - cached.saved_at
                if age_seconds < skip_window_hours * 3600:
                    login_client.hydrate_from_cache(cached_session)
                    logger.info(
                        "session_cache hit (skip revalidate, age=%ds < %dh) "
                        "account_key=%s",
                        int(age_seconds),
                        skip_window_hours,
                        account_key[:12],
                    )
                    return cached_session

            # (1b) Cache old enough to need revalidation — original path.
            revalidated = await login_client.revalidate(cached_session)
            if revalidated:
                logger.info("session_cache hit account_key=%s", account_key[:12])
                return cached_session
            # Revalidate failed — the jar is NOT polluted (2026-07 fix: manual
            # Cookie header instead of jar set). Clean up defensively + log count.
            removed = login_client.reset_openai_cookies()
            logger.info(
                "session_cache stale, HOLDING account_key=%s pending "
                "login result (reset %d openai cookies) — cache will ONLY "
                "clear if login fails due to credential/account issues (not transient)",
                account_key[:12],
                removed,
            )
            # IMPORTANT: do NOT clear cache yet. "Conservative eviction" (2026-07):
            #   - Revalidate fail may be transient (CF 403, timeout, rate-limit) →
            #     the cached session may STILL be valid, just not revalidatable now.
            #   - If login fallback succeeds → the new cache overwrites the old via
            #     `save()` (atomic replace).
            #   - If login fallback fails on a transient error → KEEP the old cache
            #     so a later retry (once CF unblocks) can reuse it.
            #   - If login fallback fails on `invalid_credential` / `account_locked`
            #     → clear cache (the session is truly invalid).
        else:
            # Corrupt payload shape — clear now to avoid looping forever (cannot
            # build a SessionBundle from this payload).
            login_client.reset_openai_cookies()
            await session_cache.clear(account_key)

    # (2) Fallback: login or direct access_token.
    if parsed.password:
        try:
            session = await login_client.login(
                email=parsed.email,
                password=parsed.password,
                totp_secret=parsed.totp_secret,
            )
        except LoginError as exc:
            # Only evict cache when the error DEFINITELY means the session is
            # invalid. `network_error` / `mfa_required` (temporarily missing TOTP)
            # are transient → keep cache for a later retry.
            if cache_had_hit and exc.reason in (
                "invalid_credential",
                "account_locked",
            ):
                await session_cache.clear(account_key)
                logger.info(
                    "session_cache evicted account_key=%s reason=%s",
                    account_key[:12],
                    exc.reason,
                )
            elif cache_had_hit:
                logger.info(
                    "session_cache KEPT account_key=%s reason=%s "
                    "(transient — cache can be reused on retry)",
                    account_key[:12],
                    exc.reason,
                )
            raise
        # R1.2, R10.5: save cache after login OK. `save` does NOT raise on I/O
        # error (R10.7 — the only Fail_Fast exception), so call it directly.
        await session_cache.save(account_key, asdict(session))
        return session

    if parsed.access_token:
        # 2-part `email|access_token` format — use the token directly, empty
        # cookies. Do NOT save cache since the token was not verified via
        # `revalidate` (caching it risks storing an invalid token).
        return SessionBundle(
            email=parsed.email,
            access_token=parsed.access_token,
            cookies={},
        )

    # Neither password nor access_token — parse_account_line should have blocked
    # this (2-part requires access_token non-empty, 3-part requires password
    # non-empty). Defensive raise for a clear Fail_Fast.
    raise LoginError(
        reason="invalid_credential",
        message=(
            "ChatgptAccount has neither `password` nor `access_token` — "
            "violates the parse_account_line invariant (defensive)."
        ),
    )


__all__ = ["resolve_session"]
