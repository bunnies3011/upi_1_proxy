"""ChatGPT checkout + promo update + already-paid helpers for UPI Direct.

Copied/adapted from ideal chatgpt checkout surface (2026-07-14). No ideal import.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Final

from app.core import http_client as http
from app.payments._chatgpt.login_client import (
    _CHATGPT_BASE_URL,
    _restore_cookies_scoped,
)
from app.payments._chatgpt.models import SessionBundle
from app.payments._chatgpt.plan_status import parse_entitlement_plan
from app.payments._chatgpt.sentinel import get_sentinel_token
from app.payments._chatgpt.sentinel_quickjs import get_sentinel_pair_via_quickjs
from app.payments._chatgpt.user_agent_profile import CHROME_145_WIN
from app.payments.upi_direct.errors import CheckoutError, PromoUpdateError
from app.payments.upi_direct.models import (
    ApproveOutcome,
    CheckoutState,
    parse_checkout_state,
)

_ENDPOINT_CHECKOUT: Final[str] = "/backend-api/payments/checkout"
_ENDPOINT_CHECKOUT_UPDATE: Final[str] = "/backend-api/payments/checkout/update"
_ENDPOINT_APPROVE: Final[str] = "/backend-api/payments/checkout/approve"
_ENDPOINT_ENTITLEMENT: Final[str] = "/backend-api/accounts/check/v4-2023-04-27"
_PLAN_NAME: Final[str] = "chatgptplusplan"
_PROCESSOR: Final[str] = "openai_llc"
_PROMO_ID: Final[str] = "plus-1-month-free"
_ALREADY_PAID_MARKER: Final[str] = "user is already paid"
_BODY_SNIPPET_MAX: Final[int] = 256


def contains_already_paid_hint(payload: Any) -> bool:
    """Centralized marker classifier — hint only, never a plan claim."""
    needle = _ALREADY_PAID_MARKER

    def _walk(node: Any) -> bool:
        if isinstance(node, str):
            return needle in node.lower()
        if isinstance(node, dict):
            return any(_walk(v) for v in node.values())
        if isinstance(node, (list, tuple)):
            return any(_walk(v) for v in node)
        return False

    return _walk(payload)


def _mint_checkout_sentinel_pair(
    proxy_url: str | None,
    device_id: str,
    logger: logging.Logger,
) -> tuple[str | None, str | None]:
    """Mint a fresh OpenAI Sentinel pair (sentinel_token + so_token) for checkout."""
    try:
        from curl_cffi import requests as curl_requests

        sess = curl_requests.Session(impersonate="chrome136", proxy=proxy_url)
        try:
            token, so_token, diag = get_sentinel_pair_via_quickjs(
                sess, device_id, flow="chatgpt_checkout", log=logger.info
            )
            if token:
                logger.info(
                    "[sentinel] checkout pair minted successfully (so=%s, t=%s)",
                    bool(so_token),
                    diag.get("has_t"),
                )
                return token, so_token
        finally:
            try:
                sess.close()
            except Exception:
                pass
    except Exception as exc:
        logger.warning("[sentinel] QuickJS pair mint exception: %s", exc)
    return None, None


class ChatgptUpiClient:
    """ChatGPT payments client for UPI Direct (India/INR/openai_llc)."""

    def __init__(
        self,
        http_client: http.AsyncSession,
        logger: logging.Logger,
        proxy_url: str | None = None,
    ) -> None:
        self._client = http_client
        self._logger = logger
        self._proxy_url = proxy_url

    async def create_checkout(
        self,
        session: SessionBundle,
        *,
        billing_country: str = "IN",
        billing_currency: str = "INR",
        language: str = "en-IN",
        include_promo: bool = True,
    ) -> CheckoutState:
        _restore_cookies_scoped(self._client, session.cookies)
        url = f"{_CHATGPT_BASE_URL}{_ENDPOINT_CHECKOUT}"
        lang = language.strip() or "en-IN"
        body: dict[str, Any] = {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": _PLAN_NAME,
            "billing_details": {
                "country": billing_country.strip().upper() or "IN",
                "currency": billing_currency.strip().upper() or "INR",
            },
            "checkout_ui_mode": "custom",
        }
        if include_promo:
            body["promo_campaign"] = {
                "promo_campaign_id": _PROMO_ID,
                "is_coupon_from_query_param": False,
            }
        device_id = (
            session.cookies.get("oai-did")
            or session.cookies.get("oai-device-id")
            or str(uuid.uuid4())
        )

        proxy_url = self._proxy_url
        if not proxy_url:
            proxies = getattr(self._client, "proxies", None)
            if isinstance(proxies, dict):
                proxy_url = (
                    proxies.get("all")
                    or proxies.get("https")
                    or proxies.get("http")
                )

        sentinel_token, so_token = await asyncio.to_thread(
            _mint_checkout_sentinel_pair, proxy_url, device_id, self._logger
        )
        if not sentinel_token:
            self._logger.info(
                "[sentinel] pair mint unavailable, falling back to pure PoW"
            )
            sentinel_token = await get_sentinel_token(
                self._client, device_id, "checkout", self._logger
            )

        headers = {
            "Authorization": f"Bearer {session.access_token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Language": f"{lang},{lang.split('-', 1)[0]};q=0.9",
            "Origin": _CHATGPT_BASE_URL,
            "Referer": f"{_CHATGPT_BASE_URL}/?promo_campaign={_PROMO_ID}",
            "User-Agent": CHROME_145_WIN.user_agent,
            "sec-ch-ua": CHROME_145_WIN.sec_ch_ua or "",
            "sec-ch-ua-mobile": CHROME_145_WIN.sec_ch_ua_mobile or "",
            "sec-ch-ua-platform": CHROME_145_WIN.sec_ch_ua_platform or "",
            "OAI-Language": lang,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-openai-target-path": _ENDPOINT_CHECKOUT,
            "x-openai-target-route": _ENDPOINT_CHECKOUT,
            "oai-device-id": device_id,
            "openai-sentinel-token": sentinel_token,
        }
        if so_token:
            headers["openai-sentinel-so-token"] = so_token
        cookie_header = "; ".join(f"{k}={v}" for k, v in session.cookies.items() if v)
        if cookie_header:
            headers["Cookie"] = cookie_header

        payload = await self._post_json(url, body, headers, step="checkout")
        try:
            state = parse_checkout_state(payload)
        except ValueError as exc:
            raise CheckoutError(detail=str(exc)) from exc
        self._logger.info(
            "chatgpt_checkout ok cs_id=%s processor=%s",
            state.checkout_session_id,
            state.processor_entity,
        )
        return state

    async def update_checkout_billing(
        self,
        session: SessionBundle,
        checkout_session_id: str,
        *,
        billing_country: str,
        billing_currency: str,
        language: str = "en-IN",
    ) -> dict[str, Any]:
        _restore_cookies_scoped(self._client, session.cookies)
        url = f"{_CHATGPT_BASE_URL}{_ENDPOINT_CHECKOUT_UPDATE}"
        lang = language.strip() or "en-IN"
        country = billing_country.strip().upper() or "IN"
        currency = billing_currency.strip().upper() or "INR"
        body: dict[str, Any] = {
            "checkout_session_id": checkout_session_id,
            "processor_entity": _PROCESSOR,
            "plan_name": _PLAN_NAME,
            "price_interval": "month",
            "seat_quantity": 1,
            "billing_details": {
                "country": country,
                "currency": currency,
            },
        }
        headers = {
            "Authorization": f"Bearer {session.access_token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Language": f"{lang},{lang.split('-', 1)[0]};q=0.9",
            "Origin": _CHATGPT_BASE_URL,
            "Referer": f"{_CHATGPT_BASE_URL}/checkout/{_PROCESSOR}/{checkout_session_id}",
            "User-Agent": CHROME_145_WIN.user_agent,
            "sec-ch-ua": CHROME_145_WIN.sec_ch_ua or "",
            "sec-ch-ua-mobile": CHROME_145_WIN.sec_ch_ua_mobile or "",
            "sec-ch-ua-platform": CHROME_145_WIN.sec_ch_ua_platform or "",
            "OAI-Language": lang,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-openai-target-path": _ENDPOINT_CHECKOUT_UPDATE,
            "x-openai-target-route": _ENDPOINT_CHECKOUT_UPDATE,
        }
        cookie_header = "; ".join(f"{k}={v}" for k, v in session.cookies.items() if v)
        if cookie_header:
            headers["Cookie"] = cookie_header
        payload = await self._post_json(url, body, headers, step="promo_update")
        self._logger.info(
            "chatgpt_billing_update ok cs_id=%s billing_country=%s billing_currency=%s",
            checkout_session_id,
            country,
            currency,
        )
        return payload

    async def update_checkout_promo(
        self,
        session: SessionBundle,
        checkout_session_id: str,
        *,
        language: str = "en-IN",
        billing_country: str | None = None,
        billing_currency: str | None = None,
    ) -> dict[str, Any]:
        _restore_cookies_scoped(self._client, session.cookies)
        url = f"{_CHATGPT_BASE_URL}{_ENDPOINT_CHECKOUT_UPDATE}"
        lang = language.strip() or "en-IN"
        body: dict[str, Any] = {
            "checkout_session_id": checkout_session_id,
            "processor_entity": _PROCESSOR,
            "plan_name": _PLAN_NAME,
            "price_interval": "month",
            "seat_quantity": 1,
            "promo_campaign": {
                "promo_campaign_id": _PROMO_ID,
                "is_coupon_from_query_param": False,
            },
        }
        country = billing_country.strip().upper() if isinstance(billing_country, str) else ""
        currency = (
            billing_currency.strip().upper()
            if isinstance(billing_currency, str)
            else ""
        )
        if country or currency:
            body["billing_details"] = {}
            if country:
                body["billing_details"]["country"] = country
            if currency:
                body["billing_details"]["currency"] = currency
        headers = {
            "Authorization": f"Bearer {session.access_token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Language": f"{lang},{lang.split('-', 1)[0]};q=0.9",
            "Origin": _CHATGPT_BASE_URL,
            "Referer": f"{_CHATGPT_BASE_URL}/checkout/{_PROCESSOR}/{checkout_session_id}",
            "User-Agent": CHROME_145_WIN.user_agent,
            "sec-ch-ua": CHROME_145_WIN.sec_ch_ua or "",
            "sec-ch-ua-mobile": CHROME_145_WIN.sec_ch_ua_mobile or "",
            "sec-ch-ua-platform": CHROME_145_WIN.sec_ch_ua_platform or "",
            "OAI-Language": lang,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-openai-target-path": _ENDPOINT_CHECKOUT_UPDATE,
            "x-openai-target-route": _ENDPOINT_CHECKOUT_UPDATE,
        }
        cookie_header = "; ".join(f"{k}={v}" for k, v in session.cookies.items() if v)
        if cookie_header:
            headers["Cookie"] = cookie_header
        payload = await self._post_json(url, body, headers, step="promo_update")
        if country or currency:
            self._logger.info(
                "chatgpt_promo_update ok cs_id=%s billing_country=%s billing_currency=%s",
                checkout_session_id,
                country or "unchanged",
                currency or "unchanged",
            )
        else:
            self._logger.info("chatgpt_promo_update ok cs_id=%s", checkout_session_id)
        return payload

    async def approve(
        self,
        session: SessionBundle,
        checkout_session_id: str,
        *,
        language: str = "en-IN",
    ) -> ApproveOutcome:
        """Single approve POST — no internal retry."""
        _restore_cookies_scoped(self._client, session.cookies)
        url = f"{_CHATGPT_BASE_URL}{_ENDPOINT_APPROVE}"
        lang = language.strip() or "en-IN"
        body = {
            "checkout_session_id": checkout_session_id,
            "processor_entity": _PROCESSOR,
        }
        device_id = (
            session.cookies.get("oai-did")
            or session.cookies.get("oai-device-id")
            or str(uuid.uuid4())
        )
        headers = {
            "Authorization": f"Bearer {session.access_token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Language": f"{lang},{lang.split('-', 1)[0]};q=0.9",
            "Origin": _CHATGPT_BASE_URL,
            "Referer": f"{_CHATGPT_BASE_URL}/checkout/{_PROCESSOR}/{checkout_session_id}",
            "User-Agent": CHROME_145_WIN.user_agent,
            "sec-ch-ua": CHROME_145_WIN.sec_ch_ua or "",
            "sec-ch-ua-mobile": CHROME_145_WIN.sec_ch_ua_mobile or "",
            "sec-ch-ua-platform": CHROME_145_WIN.sec_ch_ua_platform or "",
            "OAI-Language": lang,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-openai-target-path": _ENDPOINT_APPROVE,
            "x-openai-target-route": _ENDPOINT_APPROVE,
            "oai-device-id": device_id,
        }
        cookie_header = "; ".join(f"{k}={v}" for k, v in session.cookies.items() if v)
        if cookie_header:
            headers["Cookie"] = cookie_header
        try:
            response = await self._client.post(url, json=body, headers=headers)
        except (
            http.TimeoutException,
            http.NetworkError,
            http.TransportError,
        ):
            return ApproveOutcome(
                http_status=0, result=None, ok=False, data=None, ambiguous=True
            )
        try:
            data = response.json() if response.content else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            data = None
        result = data.get("result") if data else None
        result_s = result if isinstance(result, str) else None
        if 200 <= response.status_code < 300 and result_s == "approved":
            self._logger.info(
                "chatgpt_approve ok status=%s result=%s",
                response.status_code,
                result_s,
            )
            return ApproveOutcome(
                http_status=response.status_code,
                result=result_s,
                ok=True,
                data=data,
                ambiguous=False,
            )
        if 200 <= response.status_code < 300 and result_s in {
            "blocked",
            "rejected",
            "failed",
            "declined",
            "needs_review",
            "pending_review",
            "review_required",
            "invalid_promotion",
        }:
            self._logger.warning(
                "chatgpt_approve rejected status=%s result=%s",
                response.status_code,
                result_s,
            )
            return ApproveOutcome(
                http_status=response.status_code,
                result=result_s,
                ok=False,
                data=data,
                ambiguous=False,
            )
        # 4xx explicit business rejection
        if 400 <= response.status_code < 500 and response.status_code != 429:
            self._logger.warning(
                "chatgpt_approve non_retryable status=%s result=%s payload_type=%s",
                response.status_code,
                result_s,
                type(data).__name__ if data is not None else "None",
            )
            return ApproveOutcome(
                http_status=response.status_code,
                result=result_s,
                ok=False,
                data=data,
                ambiguous=False,
            )
        # 5xx / transport-ish / missing result → ambiguous (read-reconcile only)
        self._logger.warning(
            "chatgpt_approve ambiguous status=%s result=%s payload_type=%s",
            response.status_code,
            result_s,
            type(data).__name__ if data is not None else "None",
        )
        return ApproveOutcome(
            http_status=response.status_code,
            result=result_s,
            ok=False,
            data=data,
            ambiguous=True,
        )

    async def verify_live_plus_entitlement(
        self, session: SessionBundle
    ) -> str:
        """Live entitlement only — returns plus|free|unknown. No session fallback."""
        _restore_cookies_scoped(self._client, session.cookies)
        url = f"{_CHATGPT_BASE_URL}{_ENDPOINT_ENTITLEMENT}"
        headers = {
            "Authorization": f"Bearer {session.access_token}",
            "Accept": "application/json",
            "Accept-Language": "en-IN,en;q=0.9",
            "OAI-Language": "en-IN",
        }
        try:
            response = await self._client.get(url, headers=headers)
        except (
            http.TimeoutException,
            http.NetworkError,
            http.TransportError,
        ) as exc:
            self._logger.warning("entitlement transport error: %s", exc)
            return "unknown"
        if not (200 <= response.status_code < 300):
            self._logger.warning(
                "entitlement non-2xx status=%s", response.status_code
            )
            return "unknown"
        try:
            data = response.json()
        except ValueError:
            return "unknown"
        if not isinstance(data, dict):
            return "unknown"
        parsed = parse_entitlement_plan(data)
        plan_label = parsed.get("plan")
        active = bool(parsed.get("has_active_subscription"))
        if active and isinstance(plan_label, str) and plan_label.lower() in {
            "plus",
            "pro",
            "team",
        }:
            return "plus"
        if active and isinstance(plan_label, str) and "plus" in plan_label.lower():
            return "plus"
        if plan_label is None and not active:
            return "free"
        if isinstance(plan_label, str) and plan_label.lower() == "free":
            return "free"
        if not active:
            return "free"
        return "unknown"

    async def _post_json(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
        *,
        step: str,
    ) -> dict[str, Any]:
        try:
            response = await self._client.post(url, json=body, headers=headers)
        except (
            http.TimeoutException,
            http.NetworkError,
            http.TransportError,
        ) as exc:
            err_cls = CheckoutError if step == "checkout" else PromoUpdateError
            raise err_cls(detail=f"transport:{exc.__class__.__name__}") from exc

        if not (200 <= response.status_code < 300):
            snippet = self._body_snippet(response)
            err_cls = CheckoutError if step == "checkout" else PromoUpdateError
            # Preserve body for already-paid classification by caller.
            err = err_cls(detail=f"http_{response.status_code}:{snippet}")
            setattr(err, "response_snippet", snippet)
            setattr(err, "status_code", response.status_code)
            try:
                setattr(err, "response_payload", response.json())
            except ValueError:
                setattr(err, "response_payload", {"raw": snippet})
            raise err

        try:
            payload = response.json()
        except ValueError as exc:
            err_cls = CheckoutError if step == "checkout" else PromoUpdateError
            raise err_cls(detail="invalid_json") from exc
        if not isinstance(payload, dict):
            err_cls = CheckoutError if step == "checkout" else PromoUpdateError
            raise err_cls(detail="payload_not_dict")
        return payload

    @staticmethod
    def _body_snippet(response: http.Response) -> str:
        try:
            text = response.text or ""
        except Exception:
            text = ""
        return text[:_BODY_SNIPPET_MAX]


__all__ = [
    "ChatgptUpiClient",
    "contains_already_paid_hint",
    "_PROCESSOR",
]
