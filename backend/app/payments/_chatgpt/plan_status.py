"""Shared ChatGPT plan check (`payments/_chatgpt/`).

Used by payment handlers that need "Check Plus" without importing
`payments/ideal` (Payment_Module_Boundary). Pipeline matches iDEAL's
`ChatgptClient.check_plan_status`:

    1. GET `/api/auth/session` with cached cookies + Bearer → fresh token
    2. GET `/backend-api/accounts/check/v4-…` entitlement (live plan)
    3. Fallback: classify planType from session JSON if entitlement fails

Never raises — returns `{plan: "plus"|"free"|"unknown", ...}`.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from app.core import http_client as http
from app.payments._chatgpt.login_client import (
    _CHATGPT_BASE_URL,
    _ENDPOINT_SESSION,
    _restore_cookies_scoped,
)
from app.payments._chatgpt.models import SessionBundle

_ENDPOINT_ENTITLEMENT = "/backend-api/accounts/check/v4-2023-04-27"


def decode_jwt_claims(token: str) -> dict[str, Any] | None:
    """Decode JWT claims without signature verification."""
    if not isinstance(token, str) or "." not in token:
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        raw_b64 = parts[1]
        pad = "=" * (-len(raw_b64) % 4)
        decoded = base64.urlsafe_b64decode(raw_b64 + pad)
        claims = json.loads(decoded.decode("utf-8", "ignore"))
        if isinstance(claims, dict):
            return claims
    except Exception:
        pass
    return None


def parse_entitlement_plan(data: dict[str, Any]) -> dict[str, Any]:
    """Parse entitlement block from accounts/check → plan fields (no network)."""
    blank: dict[str, Any] = {
        "plan": None,
        "has_active_subscription": False,
        "expires_at": None,
    }
    if not isinstance(data, dict):
        return blank
    accounts = data.get("accounts")
    if not isinstance(accounts, dict) or not accounts:
        return blank
    acct = accounts.get("default")
    if not isinstance(acct, dict):
        acct = next((v for v in accounts.values() if isinstance(v, dict)), None)
    if not isinstance(acct, dict):
        return blank
    ent = acct.get("entitlement")
    if not isinstance(ent, dict):
        return blank

    raw_plan = ent.get("subscription_plan")
    label: str | None = None
    if isinstance(raw_plan, str) and raw_plan.strip():
        s = raw_plan.strip().lower()
        if s.startswith("chatgpt"):
            s = s[len("chatgpt") :]
        if s.endswith("plan"):
            s = s[: -len("plan")]
        label = s or None

    return {
        "plan": label,
        "has_active_subscription": bool(ent.get("has_active_subscription")),
        "expires_at": ent.get("expires_at"),
    }


def classify_plan_from_session(payload: dict[str, Any]) -> tuple[str, str]:
    """Heuristic plan from `/api/auth/session` JSON — fallback only."""
    plan_indicators: list[tuple[str, Any]] = []

    # 1. Top-level planType
    if isinstance(payload.get("planType"), str):
        plan_indicators.append(("planType", payload["planType"]))

    # 2. Modern ChatGPT account dict: {"planType": "plus"|"free", "structure": "personal", ...}
    account = payload.get("account")
    if isinstance(account, dict):
        for key in ("planType", "plan", "plan_type", "structure"):
            v = account.get(key)
            if isinstance(v, str):
                plan_indicators.append((f"account.{key}", v))

    # 3. accounts list or dict (multi-workspace/account checks)
    accounts = payload.get("accounts")
    if isinstance(accounts, dict):
        for k, v in accounts.items():
            if isinstance(v, dict):
                p = v.get("planType") or v.get("plan")
                if isinstance(p, str):
                    plan_indicators.append((f"accounts.{k}.plan", p))
    elif isinstance(accounts, list):
        for idx, item in enumerate(accounts):
            if isinstance(item, dict):
                p = item.get("planType") or item.get("plan")
                if isinstance(p, str):
                    plan_indicators.append((f"accounts[{idx}].plan", p))

    # 4. user dict
    user = payload.get("user")
    if isinstance(user, dict):
        for key in ("planType", "plan", "planName", "plan_name"):
            v = user.get(key)
            if isinstance(v, str):
                plan_indicators.append((f"user.{key}", v))

    # 5. subscription dict
    subscription = payload.get("subscription")
    if isinstance(subscription, dict):
        for key in ("plan", "planName", "status", "product_name"):
            v = subscription.get(key)
            if isinstance(v, str):
                plan_indicators.append((f"subscription.{key}", v))

    # 6. accessToken / access_token JWT claims
    token = payload.get("accessToken") or payload.get("access_token")
    if isinstance(token, str):
        claims = decode_jwt_claims(token)
        if claims:
            auth_claims = claims.get("https://api.openai.com/auth")
            if isinstance(auth_claims, dict):
                p = auth_claims.get("chatgpt_plan_type")
                if isinstance(p, str):
                    plan_indicators.append(("jwt.auth.chatgpt_plan_type", p))
            for key in ("chatgpt_plan_type", "planType", "plan"):
                p = claims.get(key)
                if isinstance(p, str):
                    plan_indicators.append((f"jwt.{key}", p))

    raw_plan_str = ""
    classified = "unknown"
    for source, value in plan_indicators:
        lower = value.lower()
        if not raw_plan_str:
            raw_plan_str = f"{source}={value}"
        if any(kw in lower for kw in ("plus", "pro", "team", "chatgptplus")):
            classified = "plus"
            raw_plan_str = f"{source}={value}"
            break
        if "free" in lower:
            classified = "free"
            raw_plan_str = f"{source}={value}"
    return classified, raw_plan_str


async def check_plan_status(
    client: http.AsyncSession,
    session: SessionBundle,
    logger: logging.Logger,
) -> dict[str, Any]:
    """Two-step plan check for one cached session. Never raises."""
    _restore_cookies_scoped(client, session.cookies)

    session_url = f"{_CHATGPT_BASE_URL}{_ENDPOINT_SESSION}"
    session_headers = {
        "Authorization": f"Bearer {session.access_token}",
        "Accept": "application/json",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
        "Referer": f"{_CHATGPT_BASE_URL}/",
    }

    try:
        session_resp = await client.get(session_url, headers=session_headers)
    except (
        http.TimeoutException,
        http.NetworkError,
        http.TransportError,
    ) as exc:
        logger.warning(
            "chatgpt_check_plan session transport error: email=%s exc=%s",
            session.email,
            exc,
        )
        if session.access_token:
            fallback_cls, fallback_raw = classify_plan_from_session(
                {"accessToken": session.access_token}
            )
            if fallback_cls in ("plus", "free"):
                return {
                    "plan": fallback_cls,
                    "email": session.email,
                    "raw_plan": f"cached_token:{fallback_raw}",
                    "warning": f"transport: {exc.__class__.__name__}",
                }
        return {"plan": "unknown", "error": f"transport: {exc.__class__.__name__}"}

    if not (200 <= session_resp.status_code < 300):
        logger.warning(
            "chatgpt_check_plan session non-2xx: email=%s status=%s",
            session.email,
            session_resp.status_code,
        )
        if session.access_token:
            fallback_cls, fallback_raw = classify_plan_from_session(
                {"accessToken": session.access_token}
            )
            if fallback_cls in ("plus", "free"):
                return {
                    "plan": fallback_cls,
                    "email": session.email,
                    "raw_plan": f"cached_token:{fallback_raw}",
                    "warning": f"session_http_{session_resp.status_code}",
                }
        return {"plan": "unknown", "error": f"session_http_{session_resp.status_code}"}

    try:
        session_payload = session_resp.json()
    except ValueError:
        return {"plan": "unknown", "error": "session_invalid_json"}

    if not isinstance(session_payload, dict):
        return {"plan": "unknown", "error": "session_not_dict"}

    result: dict[str, Any] = {"plan": "unknown"}

    user = session_payload.get("user")
    if isinstance(user, dict):
        email = user.get("email")
        if isinstance(email, str):
            result["email"] = email

    fresh_token = session_payload.get("accessToken")
    if not isinstance(fresh_token, str) or not fresh_token:
        fresh_token = session.access_token

    entitlement = await _fetch_entitlement(
        client, access_token=fresh_token, referer_email=session.email, logger=logger
    )

    if entitlement.get("ok") is True:
        plan_label = entitlement.get("plan")
        has_active = bool(entitlement.get("has_active_subscription"))
        if isinstance(plan_label, str) and plan_label:
            plan_lower = plan_label.lower()
            if any(marker in plan_lower for marker in ("plus", "pro", "team")):
                result["plan"] = "plus"
            elif "free" in plan_lower or not has_active:
                result["plan"] = "free"
            else:
                result["plan"] = "free"
            result["raw_plan"] = f"entitlement.subscription_plan={plan_label}"
        expires_at = entitlement.get("expires_at")
        if expires_at:
            result["expires_at"] = expires_at

        logger.info(
            "chatgpt_check_plan entitlement ok: email=%s plan=%s raw=%s",
            session.email,
            result["plan"],
            result.get("raw_plan", "n/a"),
        )
        return result

    classified, raw_plan_str = classify_plan_from_session(session_payload)
    result["plan"] = classified
    if raw_plan_str:
        result["raw_plan"] = raw_plan_str

    if classified == "unknown" and session.access_token:
        cached_cls, cached_raw = classify_plan_from_session(
            {"accessToken": session.access_token}
        )
        if cached_cls in ("plus", "free"):
            result["plan"] = cached_cls
            result["raw_plan"] = f"cached_token:{cached_raw}"

    entitlement_error = entitlement.get("error")
    if entitlement_error and result["plan"] == "unknown":
        result["error"] = f"entitlement: {entitlement_error}"

    logger.info(
        "chatgpt_check_plan fallback session: email=%s plan=%s raw=%s ent_err=%s",
        session.email,
        result["plan"],
        result.get("raw_plan", "n/a"),
        entitlement_error or "n/a",
    )
    return result


async def _fetch_entitlement(
    client: http.AsyncSession,
    *,
    access_token: str,
    referer_email: str,
    logger: logging.Logger,
) -> dict[str, Any]:
    if not isinstance(access_token, str) or not access_token.strip():
        return {"ok": False, "error": "access_token_empty"}

    url = f"{_CHATGPT_BASE_URL}{_ENDPOINT_ENTITLEMENT}"
    target = _ENDPOINT_ENTITLEMENT
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "*/*",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
        "Origin": _CHATGPT_BASE_URL,
        "Referer": f"{_CHATGPT_BASE_URL}/",
        "OAI-Language": "nl-NL",
        "x-openai-target-path": target,
        "x-openai-target-route": target,
    }

    try:
        response = await client.get(url, headers=headers)
    except (
        http.TimeoutException,
        http.NetworkError,
        http.TransportError,
    ) as exc:
        logger.warning(
            "entitlement transport error: email=%s exc=%s",
            referer_email,
            exc,
        )
        return {"ok": False, "error": f"transport: {exc.__class__.__name__}"}

    if not (200 <= response.status_code < 300):
        logger.warning(
            "entitlement non-2xx: email=%s status=%s",
            referer_email,
            response.status_code,
        )
        return {"ok": False, "error": f"http_{response.status_code}"}

    try:
        payload = response.json()
    except ValueError:
        return {"ok": False, "error": "invalid_json"}

    if not isinstance(payload, dict):
        return {"ok": False, "error": "payload_not_dict"}

    parsed = parse_entitlement_plan(payload)
    return {"ok": True, **parsed}


__all__ = [
    "check_plan_status",
    "classify_plan_from_session",
    "decode_jwt_claims",
    "parse_entitlement_plan",
]

