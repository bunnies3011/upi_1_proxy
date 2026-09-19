"""ChatgptLoginClient — pure-HTTP ChatGPT login + session resolution surface.

Shared base extracted from `payments/ideal/chatgpt_client.py` so both the iDEAL
flow and (later) the UPI flow can reuse ChatGPT authentication without importing
each other. iDEAL's `ChatgptClient` subclasses this and adds Stripe-checkout
methods.

Holds exactly the login/session methods and their real dependencies:
`login`, `revalidate`, `hydrate_from_cache`, `reset_openai_cookies`, plus the
private `_warm_cf_challenge` / `_extract_access_token` that `revalidate` depends
on. The cookie helpers/constants (`_restore_cookies_scoped`,
`_build_cookie_header`, `_CHATGPT_BASE_URL`, `_ENDPOINT_SESSION`, ...) live here
too and are imported back by the checkout subclass.

Payment_Module_Boundary: imports only `app.core.*` and `app.payments._chatgpt.*`
— NOTHING from `app.payments.ideal`.

Sensitive_Data_Redaction (Requirement 1.9): password/totp_secret/access_token
are never logged raw; log entries apply `redact_dict()` as defence-in-depth.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.core import http_client as http
from app.core.redaction import redact_dict
from app.core.session_cache import AccountSessionCache
from app.payments._chatgpt.chatgpt_login import login_pure_request
from app.payments._chatgpt.errors import ChatgptLoginError
from app.payments._chatgpt.models import SessionBundle

# ---------------------------------------------------------------------------
# Constants — endpoints + revalidate policy
# ---------------------------------------------------------------------------

#: Base URL for every ChatGPT auth endpoint (chatgpt.com — R1.3, 1.5, 10.4).
_CHATGPT_BASE_URL = "https://chatgpt.com"

#: Session query endpoint (used by `revalidate` — R10.4).
_ENDPOINT_SESSION = "/api/auth/session"

#: Cloudflare warm-up endpoint — `GET /auth/login` triggers CF to issue the
#: `__cf_bm` bot-management cookie (TTL ~30 min, bound per-session). `revalidate`
#: must warm CF BEFORE hitting `/api/auth/session` because the per-job
#: `AsyncSession` is cold and rotating-proxy IP changes invalidate cached
#: `cf_clearance` → CF returns 403 without a fresh `__cf_bm`.
_ENDPOINT_LOGIN_HTML = "/auth/login"

#: Number of revalidate attempts against `/api/auth/session` on a 403 (assume CF
#: challenge not yet cleared). Attempt 1: warm CF + hit session. Attempt 2: warm
#: again + backoff + retry. `>=3` is pointless — if two warms both fail, the
#: proxy IP is hard-blocked and login fallback is futile too.
_REVALIDATE_MAX_ATTEMPTS = 2

#: Backoff between attempt 1 → attempt 2 (seconds). CF challenge cookie needs a
#: few seconds to propagate across the edge. 2s balances "enough for CF to
#: register" vs "not dragging on when the proxy is already blocked".
_REVALIDATE_RETRY_BACKOFF_SECONDS = 2.0

#: Login-error reason for the (currently unused) `_fetch_session_payload` helper.
_LOGIN_ERROR_NETWORK = "network_error"

# ---------------------------------------------------------------------------
# Cookies helpers — fix HTTP 431 after httpx → curl_cffi migration (2026-07)
# ---------------------------------------------------------------------------

#: Domain assigned when restoring cookies into the jar so they can never
#: "wildcard match" unrelated URLs. `curl_cffi` uses the standard Python
#: ``http.cookiejar.CookieJar`` — empty-domain cookies get sent by libcurl with
#: EVERY request → jar pollution. Fix: every chatgpt cookie is set with
#: ``domain=".chatgpt.com"`` (leading dot matches subdomain + apex — RFC 6265
#: §5.2.3).
_CHATGPT_COOKIE_DOMAIN = ".chatgpt.com"

#: Cookies starting with these prefixes are chatgpt.com-scoped. Whitelist kept
#: in sync with ``chatgpt_login._ESSENTIAL_COOKIE_PREFIXES`` — when the cache is
#: write-through from login, only 3-5 essential cookies are stored.
_CHATGPT_COOKIE_PREFIXES = (
    "__Secure-next-auth.session-token",
    "__Host-next-auth.csrf-token",
    "__Secure-next-auth.csrf-token",
    "oai-did",
    "oai-sc",
    "cf_clearance",
)


def _build_cookie_header(cookies: dict[str, str]) -> str:
    """Convert a cookie dict → ``Cookie`` header string ``"name=v; name=v"``.

    Used by `revalidate()`: sends cookies DIRECTLY via header instead of
    restoring them into the shared jar. This ISOLATES the cookie snapshot from
    ``self._client.cookies.jar`` — if revalidate fails, the main jar stays clean
    so the login fallback is not hit by a 431 from jar pollution.

    Only cookies matching ``_CHATGPT_COOKIE_PREFIXES`` are included — ensuring an
    old bloated cache (29 cookies incl. ``__cf_bm``/``_dd_s``/analytics) does not
    cause a 431 on revalidate. A revalidatable old cache still works; if it
    fails, login runs again → saves a fresh essential-only cache.

    Args:
        cookies: `SessionBundle.cookies` — dict[name→value] snapshot from cache
            (whitelist-filtered on save, or old bloat format).

    Returns:
        ``"name1=value1; name2=value2"`` — empty if there is no essential cookie
        (empty value / no whitelist match / empty dict).
    """
    parts: list[str] = []
    for name, value in cookies.items():
        if not name or not value:
            continue
        # Migration guard: skip cookies not in the essential whitelist. This is
        # the choke point that keeps the header under Cloudflare's 8KB limit even
        # when the on-disk cache is still in the old bloated format.
        if not any(name.startswith(p) for p in _CHATGPT_COOKIE_PREFIXES):
            continue
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def _restore_cookies_scoped(
    client: http.AsyncSession, cookies: dict[str, str]
) -> None:
    """Restore cookies into the jar with an EXPLICIT ``domain=".chatgpt.com"``.

    Unlike ``client.cookies.set(name, value)`` without a domain: the old way
    created empty-domain cookies which libcurl (curl_cffi) sends with EVERY
    request → jar pollution + risk of header overflow for requests to other
    domains (Stripe, pay.ideal.nl…). This scopes cookies to ``.chatgpt.com`` so
    only chatgpt.com requests attach them.

    Only cookies whose name matches ``_CHATGPT_COOKIE_PREFIXES`` are restored —
    defence-in-depth alongside the save whitelist. Out-of-scope cookies (e.g. an
    old bloated cache payload) are skipped.
    """
    for name, value in cookies.items():
        if not name or not value:
            continue
        if not any(name.startswith(p) for p in _CHATGPT_COOKIE_PREFIXES):
            continue
        client.cookies.set(name, value, domain=_CHATGPT_COOKIE_DOMAIN)


class ChatgptLoginClient:
    """Pure-HTTP ChatGPT login + session-resolution surface.

    Attributes:
        _client: shared `http.AsyncSession` (proxy/timeout/UA configured by the
            caller). Cookies live in the client jar after `login`.
        _session_cache: injected for future extension — the caller currently owns
            cache lifecycle (R1.2).
        _logger: realtime logger. In the real flow this is a JobLogger (already
            redacting); this module also applies `redact_dict` as double-safety.
    """

    def __init__(
        self,
        http_client: http.AsyncSession,
        session_cache: AccountSessionCache,
        logger: logging.Logger,
    ) -> None:
        self._client = http_client
        self._session_cache = session_cache
        self._logger = logger

    # ---- login (R1.3, R1.4) ---------------------------------------------

    async def login(
        self,
        email: str,
        password: str,
        totp_secret: str | None,
    ) -> SessionBundle:
        """Log in to ChatGPT pure-HTTP → return `SessionBundle` (Requirement 1.3).

        Delegates to :func:`app.payments._chatgpt.chatgpt_login.login_pure_request`
        — the full flow (prime → CSRF → signin/openai → authorize → sentinel PoW →
        password/verify → MFA verify → follow callback → get session).

        Args:
            email: parsed account identifier (`ChatgptAccount.email`).
            password: account password (non-empty per parse_account_line).
            totp_secret: base32 TOTP secret, or `None` if the account has no MFA
                (R1.1 allows an empty 3rd part).

        Returns:
            `SessionBundle(email, access_token, cookies)` — `cookies` only for
            ``chatgpt.com`` / ``openai.com`` domains.

        Raises:
            ChatgptLoginError: reason ∈ {`invalid_credential`, `mfa_required`,
                `account_locked`, `network_error`} per Requirement 1.4. The
                caller catches this at the flow boundary.
        """
        # Safe log — never log password/totp_secret raw.
        self._logger.info(
            "chatgpt_login begin: %s",
            redact_dict(
                {
                    "email": email,
                    "has_password": bool(password),
                    "has_totp_secret": bool(totp_secret),
                }
            ),
        )

        # Delegate to `chatgpt_login` — flow ported from the Rust UPI bot.
        # `login_pure_request` logs each step via `self._logger`.
        session = await login_pure_request(
            email=email,
            password=password,
            totp_secret=totp_secret,
            http_client=self._client,
            logger=self._logger,
        )

        self._logger.info(
            "chatgpt_login ok: %s",
            redact_dict(
                {
                    "email": email,
                    "access_token": session.access_token,  # masked by redact_dict.
                    "cookie_count": len(session.cookies),
                }
            ),
        )
        return session

    # ---- Internal helper: session payload shared with revalidate ---------

    async def _fetch_session_payload(self) -> dict[str, Any]:
        """`GET /api/auth/session` helper — Requirement 10.4.

        Returns the payload dict on 2xx + valid JSON, raises `ChatgptLoginError`
        on transport/HTTP failure. The caller catches and returns False (does not
        propagate).
        """
        url = f"{_CHATGPT_BASE_URL}{_ENDPOINT_SESSION}"
        try:
            response = await self._client.get(url)
        except (http.TimeoutException, http.NetworkError, http.TransportError) as exc:
            self._logger.warning("chatgpt session transport error: %s", exc)
            raise ChatgptLoginError(reason=_LOGIN_ERROR_NETWORK) from exc

        if not (200 <= response.status_code < 300):
            self._logger.warning(
                "chatgpt session non-2xx: status=%s", response.status_code
            )
            raise ChatgptLoginError(reason=_LOGIN_ERROR_NETWORK)

        try:
            payload = response.json()
        except ValueError as exc:
            self._logger.warning("chatgpt session invalid JSON: %s", exc)
            raise ChatgptLoginError(reason=_LOGIN_ERROR_NETWORK) from exc

        if not isinstance(payload, dict):
            self._logger.warning(
                "chatgpt session payload is not a dict: type=%s",
                type(payload).__name__,
            )
            raise ChatgptLoginError(reason=_LOGIN_ERROR_NETWORK)
        return payload

    @staticmethod
    def _extract_access_token(payload: dict[str, Any]) -> str | None:
        """Extract `accessToken` from a `/api/auth/session` payload.

        NextAuth may return `accessToken` top-level OR nested under
        `user.accessToken` — check both. Returns `None` if not found.
        """
        top_level = payload.get("accessToken")
        if isinstance(top_level, str) and top_level:
            return top_level
        user = payload.get("user")
        if isinstance(user, dict):
            nested = user.get("accessToken") or user.get("access_token")
            if isinstance(nested, str) and nested:
                return nested
        return None

    # ---- revalidate (R10.4) ---------------------------------------------

    async def revalidate(self, session: SessionBundle) -> bool:
        """Revalidate one cached `SessionBundle` — Requirement 10.4.

        Sends cookies from `session` via a manual ``Cookie`` header + hits
        `GET /api/auth/session`. Returns:
            - `True` when 2xx and payload has `accessToken` OR `user.id`
              (session still valid on ChatGPT's side).
            - `False` on 401/403 (session revoked), timeout, missing
              user/accessToken, or any transport error.

        NEVER raises (per Requirement 10.4) — the caller clears cache on `False`
        and falls back to a fresh `login()`.

        Isolation strategy (fix HTTP 431 — 2026-07): cookies are NOT restored
        into ``self._client.cookies.jar`` to avoid pollution if revalidate fails.
        Only on SUCCESS does ``_restore_cookies_scoped()`` populate the jar (with
        explicit ``domain=".chatgpt.com"``) for subsequent checkout steps.
        """
        cookie_header = _build_cookie_header(session.cookies)
        if not cookie_header:
            # Cache payload has no usable cookies → treat as invalid.
            self._logger.info(
                "chatgpt_revalidate: cache has no chatgpt.com cookie — email=%s",
                session.email,
            )
            return False

        # Attempt loop: attempt 1 warm CF + hit session. On 403 → warm again +
        # backoff + attempt 2. Non-403 (200/401/timeout/…) exits immediately (401
        # = truly dead session, timeout = transient → login fallback handles it).
        response: http.Response | None = None
        last_status: int | None = None
        for attempt in range(1, _REVALIDATE_MAX_ATTEMPTS + 1):
            # Warm CF BEFORE each attempt. The `__cf_bm` cookie is session-scoped
            # (goes into the current client jar), safe to keep even if revalidate
            # fails — the login fallback needs it too. A warm failure (transport
            # error) → skip warm, hit session directly (fail-soft).
            await self._warm_cf_challenge()

            try:
                response = await self._client.get(
                    f"{_CHATGPT_BASE_URL}{_ENDPOINT_SESSION}",
                    headers={
                        # No `Authorization: Bearer` — the NextAuth
                        # `/api/auth/session` endpoint reads only the
                        # `__Secure-next-auth.session-token` cookie. Sending a
                        # Bearer too is an anomaly that raises CF's bot score.
                        "Cookie": cookie_header,
                        "Accept": "application/json",
                        "Referer": f"{_CHATGPT_BASE_URL}/",
                    },
                )
            except (
                http.TimeoutException,
                http.NetworkError,
                http.TransportError,
            ) as exc:
                self._logger.info(
                    "chatgpt_revalidate: transport error, treating cache as invalid — email=%s (%s)",
                    session.email,
                    exc,
                )
                return False

            last_status = response.status_code
            # 403 → CF challenge not cleared, retry with backoff (if attempts left).
            if response.status_code == 403 and attempt < _REVALIDATE_MAX_ATTEMPTS:
                self._logger.info(
                    "chatgpt_revalidate: CF 403 attempt=%d/%d — retry after %.1fs (email=%s)",
                    attempt,
                    _REVALIDATE_MAX_ATTEMPTS,
                    _REVALIDATE_RETRY_BACKOFF_SECONDS,
                    session.email,
                )
                await asyncio.sleep(_REVALIDATE_RETRY_BACKOFF_SECONDS)
                continue
            # Non-403 or attempts exhausted → exit loop, handle response below.
            break

        # mypy guard: response is set in every non-returning branch.
        assert response is not None

        if not (200 <= response.status_code < 300):
            self._logger.info(
                "chatgpt_revalidate: non-2xx after %d attempt(s), treating cache as invalid — "
                "email=%s status=%s",
                _REVALIDATE_MAX_ATTEMPTS if last_status == 403 else 1,
                session.email,
                response.status_code,
            )
            return False

        try:
            payload = response.json()
        except ValueError as exc:
            self._logger.info(
                "chatgpt_revalidate: invalid JSON, treating cache as invalid — email=%s (%s)",
                session.email,
                exc,
            )
            return False

        if not isinstance(payload, dict):
            self._logger.info(
                "chatgpt_revalidate: payload is not a dict, treating cache as invalid — email=%s type=%s",
                session.email,
                type(payload).__name__,
            )
            return False

        # Session still valid when it has accessToken OR user.id.
        has_access_token = bool(self._extract_access_token(payload))
        user = payload.get("user")
        has_user_id = isinstance(user, dict) and bool(user.get("id"))
        is_valid = has_access_token or has_user_id

        self._logger.debug(
            "chatgpt_revalidate: email=%s valid=%s (has_access_token=%s, has_user_id=%s)",
            session.email,
            is_valid,
            has_access_token,
            has_user_id,
        )

        # Revalidate OK → ONLY NOW restore cookies into the shared jar with
        # explicit ``domain=".chatgpt.com"`` so later steps (`create_checkout` /
        # `snapshot` / `approve`) reuse the same ``AsyncSession`` without re-reading
        # cache. If invalid, do NOT touch the jar → login fallback starts from
        # zero-state (avoids 431).
        if is_valid:
            _restore_cookies_scoped(self._client, session.cookies)

        return is_valid

    # ---- warm_cf_challenge (helper for revalidate) ---------------------

    async def _warm_cf_challenge(self) -> None:
        """Warm up Cloudflare: `GET /auth/login` so CF issues `__cf_bm`.

        Context (bug fix 2026-07): the per-job `AsyncSession` is cold, without
        `__cf_bm`. Hitting `/api/auth/session` directly with manual cookies looks
        like a "strange" client (no bot-management token for this session) → 403.
        Especially bad with rotating proxies. `GET /auth/login` is a public HTML
        endpoint (no auth) and CF always sets `__cf_bm` in the response.

        Fail-soft: on failure (transport error, 5xx…) → no raise, no return
        status. The caller (`revalidate`) hits `/api/auth/session` anyway → on
        403 it retries or falls back to login.
        """
        try:
            response = await self._client.get(
                f"{_CHATGPT_BASE_URL}{_ENDPOINT_LOGIN_HTML}",
                headers={
                    # HTML navigation headers — CF whitelist browser pattern.
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,"
                        "image/avif,image/webp,*/*;q=0.8"
                    ),
                    "Accept-Language": "en-US,en;q=0.9,nl;q=0.8",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1",
                },
                allow_redirects=True,
            )
        except (
            http.TimeoutException,
            http.NetworkError,
            http.TransportError,
        ) as exc:
            # Warm fail-soft — log info, no raise. Caller handles fallback.
            self._logger.info(
                "chatgpt_revalidate: CF warm failed (transport), continuing anyway (%s)",
                exc,
            )
            return
        self._logger.debug(
            "chatgpt_revalidate: CF warm status=%s", response.status_code
        )

    # ---- hydrate_from_cache (skip-revalidate happy path) ----------------

    def hydrate_from_cache(self, session: SessionBundle) -> None:
        """Restore cookies from `session` into the jar WITHOUT any HTTP request.

        Used on the "skip revalidate when cache is fresh" path: the caller
        (`resolve_session`) already checked the cache age is under
        ``session_cache.skip_revalidate_if_fresh_hours`` → trusts the cache and
        just hydrates cookies for the later steps.

        Cookies are restored with explicit ``domain=".chatgpt.com"`` via the
        essential whitelist — safe against the 431 bug. If a stale cache turns out
        invalid, `create_checkout` fails early with 401 and the caller handles it
        as a normal flow error.
        """
        _restore_cookies_scoped(self._client, session.cookies)

    # ---- reset_openai_cookies (self-heal jar after revalidate fail) ------

    def reset_openai_cookies(self) -> int:
        """Remove every chatgpt.com / openai.com cookie from the jar.

        Since 2026-07 `revalidate()` no longer touches the jar on failure and
        `_restore_cookies_scoped()` always sets ``domain=".chatgpt.com"`` — this
        method is now defence-in-depth for jars polluted by another code path
        (e.g. a Set-Cookie from a Stripe/pay.ideal.nl redirect chain on the wrong
        domain).

        Removes two groups: (1) chatgpt.com/openai.com cookies (incl. subdomains),
        (2) empty-domain cookies (libcurl sends these with every request).

        Cookies are per-job (`run()` creates a fresh `AsyncSession` each time), so
        clearing all chatgpt/openai/empty-domain cookies is safe.

        Returns:
            Number of cookies removed. 0 if the jar is clean — idempotent.
        """
        jar = self._client.cookies.jar
        to_remove: list[Any] = []
        for cookie in jar:
            raw_domain = cookie.domain or ""
            domain = raw_domain.lstrip(".").lower()
            # Group 2: empty-domain cookies — remove preventively.
            if not domain:
                to_remove.append(cookie)
                continue
            # Group 1: chatgpt.com / openai.com cookies and subdomains.
            if (
                domain == "chatgpt.com"
                or domain.endswith(".chatgpt.com")
                or domain == "openai.com"
                or domain.endswith(".openai.com")
            ):
                to_remove.append(cookie)

        for cookie in to_remove:
            # `jar.clear(domain, path, name)` is the `http.cookiejar` standard —
            # curl_cffi.requests.Cookies.clear() matches exact domain only and
            # does NOT cover cookies with a "." prefix or subdomains.
            try:
                jar.clear(cookie.domain, cookie.path, cookie.name)
            except KeyError:
                # Cookie may have been GC'd or removed by another loop — safe skip.
                continue

        return len(to_remove)


__all__ = ["ChatgptLoginClient"]
