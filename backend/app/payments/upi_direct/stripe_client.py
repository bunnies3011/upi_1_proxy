"""Stripe init + elements(UPI) for UPI Direct.

Adapted from ideal stripe_client init/elements (2026-07-14). No ideal import.
Amount parsing is fail-closed via models.parse_amount_state.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from urllib.parse import quote
from typing import Any, Awaitable, Callable, Final, Sequence

from app.core import http_client as http
from app.core.settings_store import SettingsRepository
from app.payments._chatgpt.user_agent_profile import CHROME_145_WIN
from app.payments.upi_direct.errors import ConfirmFailedError, ElementsError, StripeInitError
from app.payments.upi_direct.models import (
    ConfirmAttempt,
    ElementsState,
    ExpectedPaymentState,
    StripeInitState,
    parse_elements_state,
    parse_stripe_init_state,
)
from app.payments.upi_direct.profile_generator import IndiaBillingProfile
from app.payments.upi_direct.stripe_token import (
    StripeTokenConfig,
    compute_js_checksum,
    compute_rv_timestamp,
    extract_config_live,
)

_STRIPE_API_BASE: Final[str] = "https://api.stripe.com"
_STRIPE_JS_ORIGIN: Final[str] = "https://js.stripe.com"
_STRIPE_VERSION: Final[str] = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)
_BROWSER_LOCALE: Final[str] = "en-IN"
_BROWSER_TIMEZONE: Final[str] = "Asia/Kolkata"
_INIT_PATH: Final[str] = "/v1/payment_pages/{checkout_session_id}/init"
_ELEMENTS_PATH: Final[str] = "/v1/elements/sessions"
_CONFIRM_PATH: Final[str] = "/v1/payment_pages/{checkout_session_id}/confirm"
_REFRESH_PATH: Final[str] = "/v1/payment_pages/{checkout_session_id}"
_CONFIRM_VARIANTS: Final[tuple[str, ...]] = ("qr_code", "empty", "flow_qr", "intent")
_SETTING_TIMEOUT: Final[str] = "upi_direct.stripe_request_timeout_seconds"
_SETTING_MAX_RETRY: Final[str] = "upi_direct.max_concurrent"  # not used for retry
_DEFAULT_TIMEOUT: Final[float] = 30.0
_DEFAULT_MAX_RETRY: Final[int] = 3
_DEFAULT_BACKOFF: Final[float] = 0.5
_BODY_SNIPPET_MAX: Final[int] = 300


def _confirm_has_intent(data: Any) -> bool:
    """Return True if confirm response payload contains a created intent or next_action."""
    if not isinstance(data, dict):
        return False
    if isinstance(data.get("payment_intent"), dict):
        return True
    if isinstance(data.get("setup_intent"), dict):
        return True
    if isinstance(data.get("next_action"), dict):
        return True
    return False


def flatten_form(payload: Any, prefix: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if isinstance(payload, dict):
        for k, v in payload.items():
            key = f"{prefix}[{k}]" if prefix else str(k)
            out.extend(flatten_form(v, key))
    elif isinstance(payload, (list, tuple)):
        for i, v in enumerate(payload):
            out.extend(flatten_form(v, f"{prefix}[{i}]"))
    else:
        if payload is None:
            return out
        if isinstance(payload, bool):
            value = "true" if payload else "false"
        else:
            value = str(payload)
        out.append((prefix, value))
    return out


def encode_form_urlencoded(pairs: list[tuple[str, str]]) -> bytes:
    from urllib.parse import urlencode

    return urlencode(pairs, doseq=False).encode("utf-8")


class StripeUpiClient:
    def __init__(
        self,
        http_client: http.AsyncSession,
        settings: SettingsRepository,
        logger: logging.Logger,
        *,
        timeout_setting_key: str = _SETTING_TIMEOUT,
        browser_locale: str = _BROWSER_LOCALE,
        browser_timezone: str = _BROWSER_TIMEZONE,
        locale: str = "en",
        confirm_error_label: str = "UPI",
    ) -> None:
        self._http_client = http_client
        self._settings = settings
        self._logger = logger
        self._timeout_setting_key = timeout_setting_key
        self._browser_locale = browser_locale
        self._browser_timezone = browser_timezone
        self._locale = locale
        self._confirm_error_label = confirm_error_label.strip() or "UPI"
        self._stripe_js_id: str | None = None
        self._token_config: StripeTokenConfig | None = None

    def _get_or_create_stripe_js_id(self) -> str:
        if self._stripe_js_id is None:
            self._stripe_js_id = f"{uuid.uuid4()}{uuid.uuid4().hex[:10]}"
        return self._stripe_js_id

    async def ensure_token_config(self) -> StripeTokenConfig | None:
        """Best-effort token config; returns None on extract failure."""
        if self._token_config is not None:
            return self._token_config
        try:
            self._token_config = await extract_config_live(
                self._http_client, use_cache=True
            )
            self._logger.info(
                "stripe token_config fetched hash=%s",
                self._token_config.bundle_hash[:12],
            )
            return self._token_config
        except Exception as exc:
            self._logger.warning("stripe token_config failed: %s", exc)
            return None

    async def init(
        self, checkout_session_id: str, publishable_key: str
    ) -> StripeInitState:
        if not checkout_session_id or not publishable_key:
            raise StripeInitError(detail="missing cs_id or pk")
        url = f"{_STRIPE_API_BASE}{_INIT_PATH.format(checkout_session_id=checkout_session_id)}"
        stripe_js_id = self._get_or_create_stripe_js_id()
        payload = {
            "browser_locale": self._browser_locale,
            "browser_timezone": self._browser_timezone,
            "elements_session_client": {
                "client_betas": [
                    "custom_checkout_server_updates_1",
                    "custom_checkout_manual_approval_1",
                ],
                "elements_init_source": "custom_checkout",
                "referrer_host": "chatgpt.com",
                "stripe_js_id": stripe_js_id,
                "locale": self._locale,
                "is_aggregation_expected": "false",
            },
            "elements_options_client": {
                "saved_payment_method": {
                    "enable_save": "auto",
                    "enable_redisplay": "auto",
                },
            },
            "key": publishable_key,
            "_stripe_version": _STRIPE_VERSION,
        }
        content = encode_form_urlencoded(flatten_form(payload))
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": _STRIPE_JS_ORIGIN,
            "Referer": f"{_STRIPE_JS_ORIGIN}/",
        }
        timeout = await self._read_timeout()
        response = await self._bounded_retry(
            "init",
            lambda: self._http_client.post(
                url, data=content, headers=headers, timeout=timeout
            ),
        )
        body = self._parse_json(response, "init")
        try:
            state = parse_stripe_init_state(body)
        except ValueError as exc:
            raise StripeInitError(detail=str(exc)) from exc
        self._logger.info(
            "stripe_init ok page_id=%s amount=%s currency=%s",
            state.page_id,
            state.amount.amount_minor,
            state.amount.currency,
        )
        return state

    async def elements_sessions(
        self,
        checkout_session_id: str,
        publishable_key: str,
        amount_minor: int,
        *,
        currency: str = "inr",
        payment_method_types: Sequence[str] | None = None,
        setup_future_usage: str | None = "off_session",
    ) -> ElementsState:
        if not checkout_session_id or not publishable_key:
            raise ElementsError(detail="missing cs_id or pk")
        if not isinstance(amount_minor, int) or isinstance(amount_minor, bool):
            raise ElementsError(detail="amount_minor must be int")
        url = f"{_STRIPE_API_BASE}{_ELEMENTS_PATH}"
        stripe_js_id = self._get_or_create_stripe_js_id()
        currency_s = currency.strip().lower() or "inr"
        methods = tuple(payment_method_types or ("card", "link", "upi"))
        query_params: list[tuple[str, str]] = [
            ("client_betas[0]", "custom_checkout_server_updates_1"),
            ("client_betas[1]", "custom_checkout_manual_approval_1"),
            ("deferred_intent[mode]", "subscription"),
            ("deferred_intent[amount]", str(amount_minor)),
            ("deferred_intent[currency]", currency_s),
            *[
                (f"deferred_intent[payment_method_types][{idx}]", method)
                for idx, method in enumerate(methods)
            ],
            ("currency", currency_s),
            ("key", publishable_key),
            ("_stripe_version", _STRIPE_VERSION),
            ("elements_init_source", "custom_checkout"),
            ("referrer_host", "chatgpt.com"),
            ("stripe_js_id", stripe_js_id),
            ("locale", self._locale),
            ("type", "deferred_intent"),
            ("checkout_session_id", checkout_session_id),
        ]
        if setup_future_usage:
            query_params.insert(
                5,
                ("deferred_intent[setup_future_usage]", setup_future_usage),
            )
        headers = {
            "Accept": "application/json",
            "Origin": _STRIPE_JS_ORIGIN,
            "Referer": f"{_STRIPE_JS_ORIGIN}/",
        }
        timeout = await self._read_timeout()
        response = await self._bounded_retry(
            "elements_sessions",
            lambda: self._http_client.get(
                url, params=query_params, headers=headers, timeout=timeout
            ),
        )
        body = self._parse_json(response, "elements_sessions")
        try:
            state = parse_elements_state(body)
        except ValueError as exc:
            raise ElementsError(detail=str(exc)) from exc
        self._logger.info("stripe_elements ok session_id=%s", state.session_id)
        return state

    def _upi_payload(self, variant: str) -> dict[str, Any]:
        if variant == "flow_qr":
            return {"flow": "qr_code"}
        if variant == "qr_code":
            return {"qr_code": {}}
        if variant == "intent":
            return {"intent": "qr_code"}
        return {}

    async def confirm_upi(
        self,
        expected: ExpectedPaymentState,
        profile: IndiaBillingProfile,
        email: str,
        *,
        token_config: StripeTokenConfig | None = None,
    ) -> ConfirmAttempt:
        """Try confirm variants in order; return variant with intent, or first 2xx."""
        first_accepted: ConfirmAttempt | None = None
        last: ConfirmAttempt | None = None
        for variant in _CONFIRM_VARIANTS:
            attempt = await self._confirm_one_variant(
                expected, profile, email, variant, token_config
            )
            last = attempt
            if attempt.ok and attempt.data is not None:
                if first_accepted is None:
                    first_accepted = attempt
                if _confirm_has_intent(attempt.data):
                    self._logger.info(
                        "confirm variant=%s produced intent status=%s",
                        variant,
                        attempt.http_status,
                    )
                    return attempt
            self._logger.info(
                "confirm variant=%s status=%s ok=%s has_intent=%s",
                variant,
                attempt.http_status,
                attempt.ok,
                _confirm_has_intent(attempt.data) if attempt.data else False,
            )
        if first_accepted is not None:
            self._logger.warning(
                "no variant produced intent; falling back to first accepted variant=%s",
                first_accepted.variant,
            )
            return first_accepted
        if last is None:
            raise self._confirm_failed("no_variants")
        raise self._confirm_failed(
            f"all_variants_failed last={last.variant}:{last.http_status}"
        )

    async def confirm_upi_variants(
        self,
        expected: ExpectedPaymentState,
        profile: IndiaBillingProfile,
        email: str,
        *,
        token_config: StripeTokenConfig | None = None,
    ) -> list[ConfirmAttempt]:
        """Try every UPI confirm variant and return all 2xx attempts.

        Some Stripe versions return a 200 payment-page config for one variant
        without the live UPI next_action. The QR flow needs to inspect every
        accepted variant instead of stopping at the first 200.
        """
        attempts: list[ConfirmAttempt] = []
        last: ConfirmAttempt | None = None
        for variant in _CONFIRM_VARIANTS:
            attempt = await self._confirm_one_variant(
                expected, profile, email, variant, token_config
            )
            last = attempt
            self._logger.info(
                "confirm variant=%s status=%s ok=%s",
                variant,
                attempt.http_status,
                attempt.ok,
            )
            if attempt.ok and attempt.data is not None:
                attempts.append(attempt)
        if attempts:
            return attempts
        if last is None:
            raise self._confirm_failed("no_variants")
        raise self._confirm_failed(
            f"all_variants_failed last={last.variant}:{last.http_status}"
        )

    async def confirm_kakao_pay(
        self,
        expected: ExpectedPaymentState,
        email: str,
        *,
        token_config: StripeTokenConfig | None = None,
        return_url: str | None = None,
        billing_details: dict[str, Any] | None = None,
    ) -> ConfirmAttempt:
        """Confirm Kakao Pay and return the Stripe redirect next_action payload."""
        return await self._confirm_one_redirect_method(
            expected,
            email,
            payment_method_type="kakao_pay",
            country="KR",
            locale="ko",
            token_config=token_config,
            return_url=return_url,
            billing_details=billing_details,
        )

    async def confirm_gcash(
        self,
        expected: ExpectedPaymentState,
        email: str,
        *,
        token_config: StripeTokenConfig | None = None,
        return_url: str | None = None,
        billing_details: dict[str, Any] | None = None,
    ) -> ConfirmAttempt:
        """Confirm GCash and return the Stripe redirect next_action payload."""
        return await self._confirm_one_redirect_method(
            expected,
            email,
            payment_method_type="gcash",
            country="PH",
            locale="en",
            token_config=token_config,
            return_url=return_url,
            billing_details=billing_details,
        )

    async def _confirm_one_redirect_method(
        self,
        expected: ExpectedPaymentState,
        email: str,
        *,
        payment_method_type: str,
        country: str,
        locale: str,
        token_config: StripeTokenConfig | None,
        return_url: str | None,
        billing_details: dict[str, Any] | None,
    ) -> ConfirmAttempt:
        url = f"{_STRIPE_API_BASE}{_CONFIRM_PATH.format(checkout_session_id=expected.checkout_session_id)}"
        stripe_js_id = self._get_or_create_stripe_js_id()
        js_checksum = None
        rv_timestamp = None
        if token_config is not None and expected.page_id:
            js_checksum = compute_js_checksum(
                expected.page_id, shift=token_config.shift
            )
            rv_timestamp = compute_rv_timestamp(token_config)

        client_attr = {
            "checkout_config_id": expected.init_config_id,
            "checkout_session_id": expected.checkout_session_id,
            "client_session_id": stripe_js_id,
            "elements_session_config_id": expected.elements_config_id or "",
            "elements_session_id": expected.elements_session_id,
            "merchant_integration_additional_elements": [
                "expressCheckout",
                "payment",
                "address",
            ],
            "merchant_integration_source": "checkout",
            "merchant_integration_subtype": "payment-element",
            "merchant_integration_version": "custom",
            "payment_intent_creation_flow": "deferred",
            "payment_method_selection_flow": "merchant_specified",
        }
        pmd_attr = dict(client_attr)
        pmd_attr["merchant_integration_source"] = "elements"
        pmd_attr["merchant_integration_version"] = "2021"
        default_return_url = (
            f"https://checkout.stripe.com/c/pay/{expected.checkout_session_id}"
            f"?returned_from_redirect=true&ui_mode=custom&return_url="
            f"{quote(f'https://chatgpt.com/checkout/verify?stripe_session_id={expected.checkout_session_id}&processor_entity=openai_llc&plan_type=plus', safe='')}"
        )

        billing = billing_details or {
            "address": {"country": country},
            "email": email,
        }

        payload: dict[str, Any] = {
            "_stripe_version": _STRIPE_VERSION,
            "client_attribution_metadata": client_attr,
            "elements_options_client": {
                "saved_payment_method": {
                    "enable_redisplay": "auto",
                    "enable_save": "auto",
                },
            },
            "elements_session_client": {
                "client_betas": [
                    "custom_checkout_server_updates_1",
                    "custom_checkout_manual_approval_1",
                ],
                "elements_init_source": "custom_checkout",
                "is_aggregation_expected": "false",
                "locale": locale,
                "referrer_host": "chatgpt.com",
                "session_id": expected.elements_session_id,
                "stripe_js_id": stripe_js_id,
            },
            "expected_amount": expected.amount_minor,
            "expected_payment_method_type": payment_method_type,
            "guid": f"{uuid.uuid4()}{uuid.uuid4().hex[:10]}",
            "init_checksum": expected.init_checksum,
            "js_checksum": js_checksum,
            "rv_timestamp": rv_timestamp,
            "passive_captcha_ekey": None,
            "passive_captcha_token": None,
            "key": expected.publishable_key,
            "muid": f"{uuid.uuid4()}{uuid.uuid4().hex[:10]}",
            "sid": f"{uuid.uuid4()}{uuid.uuid4().hex[:10]}",
            "payment_method_data": {
                "billing_details": billing,
                "allow_redisplay": "limited",
                "client_attribution_metadata": pmd_attr,
                "payment_user_agent": (
                    "stripe.js/e5ebd5e1e6; stripe-js-v3/e5ebd5e1e6; "
                    "payment-element; deferred-intent"
                ),
                "referrer": "https://chatgpt.com",
                "time_on_page": int(time.time() * 1000) % 100000,
                "type": payment_method_type,
                payment_method_type: {},
            },
            "return_url": return_url or default_return_url,
            "version": "e5ebd5e1e6",
            "link_brand": "link",
        }
        content = encode_form_urlencoded(flatten_form(payload))
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": _STRIPE_JS_ORIGIN,
            "Referer": f"{_STRIPE_JS_ORIGIN}/",
        }
        timeout = await self._read_timeout()
        try:
            response = await self._http_client.post(
                url, data=content, headers=headers, timeout=timeout
            )
        except (
            http.TimeoutException,
            http.NetworkError,
            http.TransportError,
        ) as exc:
            raise self._confirm_failed(
                f"{payment_method_type}:transport:{exc.__class__.__name__}"
            )
        try:
            data = response.json()
        except ValueError:
            data = None
        ok = 200 <= response.status_code < 300 and isinstance(data, dict)
        if ok:
            return ConfirmAttempt(
                variant=payment_method_type,
                http_status=response.status_code,
                ok=True,
                data=data,
            )
        error_detail = self._stripe_error_detail(response, data)
        raise self._confirm_failed(
            f"{payment_method_type}:http_{response.status_code}:{error_detail}"
        )

    def _confirm_failed(self, detail: str) -> ConfirmFailedError:
        return ConfirmFailedError(
            detail=detail,
            message=f"{self._confirm_error_label} confirm failed (detail={detail})",
        )

    @staticmethod
    def _stripe_error_detail(response: http.Response, data: Any) -> str:
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                parts = []
                for key in ("type", "code", "param", "message"):
                    value = err.get(key)
                    if isinstance(value, str) and value.strip():
                        parts.append(f"{key}={value.strip()}")
                if parts:
                    return " ".join(parts)[:_BODY_SNIPPET_MAX]
        try:
            text = response.text or ""
        except Exception:
            text = ""
        return text[:_BODY_SNIPPET_MAX]

    async def _confirm_one_variant(
        self,
        expected: ExpectedPaymentState,
        profile: IndiaBillingProfile,
        email: str,
        variant: str,
        token_config: StripeTokenConfig | None,
    ) -> ConfirmAttempt:
        url = f"{_STRIPE_API_BASE}{_CONFIRM_PATH.format(checkout_session_id=expected.checkout_session_id)}"
        stripe_js_id = self._get_or_create_stripe_js_id()
        js_checksum = None
        rv_timestamp = None
        if token_config is not None and expected.page_id:
            js_checksum = compute_js_checksum(
                expected.page_id, shift=token_config.shift
            )
            rv_timestamp = compute_rv_timestamp(token_config)

        client_attr = {
            "checkout_config_id": expected.init_config_id,
            "checkout_session_id": expected.checkout_session_id,
            "client_session_id": stripe_js_id,
            "elements_session_config_id": expected.elements_config_id or "",
            "elements_session_id": expected.elements_session_id,
            "merchant_integration_additional_elements": [
                "expressCheckout",
                "payment",
                "address",
            ],
            "merchant_integration_source": "checkout",
            "merchant_integration_subtype": "payment-element",
            "merchant_integration_version": "custom",
            "payment_intent_creation_flow": "deferred",
            "payment_method_selection_flow": "merchant_specified",
        }
        pmd_attr = dict(client_attr)
        pmd_attr["merchant_integration_source"] = "elements"
        pmd_attr["merchant_integration_version"] = "2021"

        payload: dict[str, Any] = {
            "_stripe_version": _STRIPE_VERSION,
            "client_attribution_metadata": client_attr,
            "elements_options_client": {
                "saved_payment_method": {
                    "enable_redisplay": "auto",
                    "enable_save": "auto",
                },
            },
            "elements_session_client": {
                "client_betas": [
                    "custom_checkout_server_updates_1",
                    "custom_checkout_manual_approval_1",
                ],
                "elements_init_source": "custom_checkout",
                "is_aggregation_expected": "false",
                "locale": "en",
                "referrer_host": "chatgpt.com",
                "session_id": expected.elements_session_id,
                "stripe_js_id": stripe_js_id,
            },
            "expected_amount": expected.amount_minor,
            "expected_payment_method_type": "upi",
            "guid": f"{uuid.uuid4()}{uuid.uuid4().hex[:10]}",
            "init_checksum": expected.init_checksum,
            "js_checksum": js_checksum,
            "rv_timestamp": rv_timestamp,
            "passive_captcha_ekey": None,
            "passive_captcha_token": None,
            "key": expected.publishable_key,
            "muid": f"{uuid.uuid4()}{uuid.uuid4().hex[:10]}",
            "sid": f"{uuid.uuid4()}{uuid.uuid4().hex[:10]}",
            "payment_method_data": {
                "billing_details": {
                    "address": {
                        "city": profile.city,
                        "country": "IN",
                        "line1": profile.address_line1,
                        "postal_code": profile.postal_code,
                        "state": profile.state,
                    },
                    "email": email,
                    "name": profile.name,
                },
                "client_attribution_metadata": pmd_attr,
                "payment_user_agent": (
                    "stripe.js/e5ebd5e1e6; stripe-js-v3/e5ebd5e1e6; "
                    "payment-element; deferred-intent"
                ),
                "referrer": "https://chatgpt.com",
                "type": "upi",
                "upi": self._upi_payload(variant),
            },
            "return_url": f"https://checkout.stripe.com/c/pay/{expected.checkout_session_id}",
            "version": "e5ebd5e1e6",
        }
        content = encode_form_urlencoded(flatten_form(payload))
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": _STRIPE_JS_ORIGIN,
            "Referer": f"{_STRIPE_JS_ORIGIN}/",
            "User-Agent": CHROME_145_WIN.user_agent,
            "sec-ch-ua": CHROME_145_WIN.sec_ch_ua or "",
            "sec-ch-ua-mobile": CHROME_145_WIN.sec_ch_ua_mobile or "",
            "sec-ch-ua-platform": CHROME_145_WIN.sec_ch_ua_platform or "",
            "Accept-Language": "en-IN,en;q=0.9",
        }
        timeout = await self._read_timeout()
        try:
            response = await self._http_client.post(
                url, data=content, headers=headers, timeout=timeout
            )
        except (
            http.TimeoutException,
            http.NetworkError,
            http.TransportError,
        ) as exc:
            return ConfirmAttempt(
                variant=variant,
                http_status=0,
                ok=False,
                error_detail=f"transport:{exc.__class__.__name__}",
            )
        try:
            data = response.json()
        except ValueError:
            data = None
        ok = 200 <= response.status_code < 300 and isinstance(data, dict)
        return ConfirmAttempt(
            variant=variant,
            http_status=response.status_code,
            ok=ok,
            data=data if ok else None,
            error_detail=None if ok else f"http_{response.status_code}",
        )

    async def payment_page_refresh(
        self, expected: ExpectedPaymentState
    ) -> dict[str, Any]:
        url = f"{_STRIPE_API_BASE}{_REFRESH_PATH.format(checkout_session_id=expected.checkout_session_id)}"
        stripe_js_id = self._get_or_create_stripe_js_id()
        query = flatten_form(
            {
                "key": expected.publishable_key,
                "_stripe_version": _STRIPE_VERSION,
                "elements_session_client": {
                    "client_betas": [
                        "custom_checkout_server_updates_1",
                        "custom_checkout_manual_approval_1",
                    ],
                    "session_id": expected.elements_session_id,
                    "stripe_js_id": stripe_js_id,
                    "elements_init_source": "custom_checkout",
                    "referrer_host": "chatgpt.com",
                    "locale": "en",
                    "is_aggregation_expected": "false",
                },
                "elements_options_client": {
                    "saved_payment_method": {
                        "enable_redisplay": "auto",
                        "enable_save": "auto",
                    },
                },
            }
        )
        headers = {
            "Accept": "application/json",
            "Origin": _STRIPE_JS_ORIGIN,
            "Referer": f"{_STRIPE_JS_ORIGIN}/",
            "User-Agent": CHROME_145_WIN.user_agent,
            "sec-ch-ua": CHROME_145_WIN.sec_ch_ua or "",
            "sec-ch-ua-mobile": CHROME_145_WIN.sec_ch_ua_mobile or "",
            "sec-ch-ua-platform": CHROME_145_WIN.sec_ch_ua_platform or "",
            "Accept-Language": "en-IN,en;q=0.9",
        }
        timeout = await self._read_timeout()
        response = await self._http_client.get(
            url, params=query, headers=headers, timeout=timeout
        )
        if not (200 <= response.status_code < 300):
            raise self._confirm_failed(f"refresh_http_{response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise self._confirm_failed("refresh_invalid_json") from exc
        if not isinstance(data, dict):
            raise self._confirm_failed("refresh_not_dict")
        return data

    async def _bounded_retry(
        self,
        name: str,
        do_request: Callable[[], Awaitable[http.Response]],
    ) -> http.Response:
        last_exc: Exception | None = None
        for attempt in range(1, _DEFAULT_MAX_RETRY + 1):
            try:
                response = await do_request()
            except (
                http.TimeoutException,
                http.NetworkError,
                http.TransportError,
            ) as exc:
                last_exc = exc
                if attempt >= _DEFAULT_MAX_RETRY:
                    break
                await asyncio.sleep(_DEFAULT_BACKOFF * attempt)
                continue
            if 500 <= response.status_code < 600:
                last_exc = RuntimeError(f"http_{response.status_code}")
                if attempt >= _DEFAULT_MAX_RETRY:
                    break
                await asyncio.sleep(_DEFAULT_BACKOFF * attempt)
                continue
            if 400 <= response.status_code < 500:
                snippet = (response.text or "")[:200]
                if name == "init":
                    raise StripeInitError(
                        detail=f"http_{response.status_code}:{snippet}"
                    )
                raise ElementsError(detail=f"http_{response.status_code}:{snippet}")
            return response
        detail = f"retry_exhausted:{last_exc}"
        if name == "init":
            raise StripeInitError(detail=detail)
        raise ElementsError(detail=detail)

    def _parse_json(self, response: http.Response, name: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            if name == "init":
                raise StripeInitError(detail="invalid_json") from exc
            raise ElementsError(detail="invalid_json") from exc
        if not isinstance(payload, dict):
            if name == "init":
                raise StripeInitError(detail="payload_not_dict")
            raise ElementsError(detail="payload_not_dict")
        return payload

    async def _read_timeout(self) -> float:
        value = await self._settings.get(self._timeout_setting_key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
        return _DEFAULT_TIMEOUT


__all__ = ["StripeUpiClient", "flatten_form", "encode_form_urlencoded"]
