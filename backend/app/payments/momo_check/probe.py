"""MoMo availability probe via ChatGPT checkout + Stripe elements.

This mode stops before confirm/approve. A job succeeds only when Stripe exposes
MoMo in the checkout/elements payload.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Final

from app.core import http_client as http
from app.core.payment_flow import Job, JobResult, JobStatus, ProxyLeaseHealth
from app.core.settings_store import SettingsRepository
from app.payments._chatgpt.models import SessionBundle
from app.payments.upi_direct.amount_gate import apply_amount_gates
from app.payments.upi_direct.cancel_helpers import await_cancelable, check_stop
from app.payments.upi_direct.chatgpt_client import (
    ChatgptUpiClient,
    _PROCESSOR,
    contains_already_paid_hint,
)
from app.payments.upi_direct.errors import (
    CheckoutError,
    ProcessorEntityMismatchError,
    PromoUpdateError,
    UpiDirectFlowError,
)
from app.payments.upi_direct.models import CheckoutState, ResolvedProxyPools
from app.payments.upi_direct.post_login_flow import (
    _maybe_handle_already_paid,
    _raise_already_paid_from_entitlement,
)
from app.payments.upi_direct.proxy_pools import pick_and_materialize, proxy_secret_values
from app.payments.upi_direct.stripe_client import StripeUpiClient

_DEFAULT_HTTP_TIMEOUT_SECONDS: Final[float] = 30.0
_SETTING_STRIPE_TIMEOUT: Final[str] = "momo_check.stripe_request_timeout_seconds"
_PAYMENT_LINK_BASE: Final[str] = "https://checkout.stripe.com/c/pay"
_BILLING_COUNTRY: Final[str] = "VN"
_BILLING_CURRENCY: Final[str] = "VND"
_LANGUAGE: Final[str] = "vi-VN"
_CHECKOUT_MAX_IN_FLIGHT: Final[int] = 2
_CHECKOUT_RETRY_DELAYS_SECONDS: Final[tuple[float, ...]] = (5.0, 10.0)
_CHECKOUT_GATE = asyncio.Semaphore(_CHECKOUT_MAX_IN_FLIGHT)

_KNOWN_METHOD_MARKERS: Final[tuple[str, ...]] = (
    "momo",
    "apple_pay",
    "google_pay",
    "gpay",
    "card",
    "link",
    "upi",
    "ideal",
    "paypal",
    "cashapp",
    "paynow",
    "grabpay",
    "alipay",
    "wechat_pay",
)


class MomoUnavailableError(UpiDirectFlowError):
    def __init__(self, methods: list[str]) -> None:
        label = ", ".join(methods) if methods else "none"
        super().__init__(
            step="momo_probe",
            error_code="momo_not_available",
            message=f"MoMo payment method not available (methods={label})",
        )
        self.methods = methods


class MomoCheckConfigError(UpiDirectFlowError):
    def __init__(self, detail: str) -> None:
        super().__init__(
            step="validate_config",
            error_code="momo_check_config_invalid",
            message=f"MoMo check config invalid (detail={detail})",
        )


class MomoCheckoutConcurrencyLimitError(UpiDirectFlowError):
    def __init__(self, attempts: int) -> None:
        self.attempts = attempts
        super().__init__(
            step="checkout",
            error_code="checkout_concurrency_limited",
            message=(
                "Checkout temporarily rejected by upstream concurrency limit "
                f"after {attempts} attempt(s)"
            ),
        )


_AUTHORITATIVE_METHOD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "available_payment_method_types",
        "eligible_payment_method_types",
        "ordered_payment_method_types",
        "payment_method_types",
    }
)
_NON_AVAILABLE_PATH_MARKERS: Final[tuple[str, ...]] = (
    "deferred_intent",
    "disabled",
    "external_payment_method_types",
    "ineligible",
    "payment_method_specs",
    "unactivated",
    "unavailable",
    "unsupported",
)


def collect_payment_method_tokens(*payloads: Any) -> list[str]:
    """Read only Stripe's authoritative available-method lists.

    Do not scan arbitrary values or payment method specs. Those payload
    sections contain Stripe's full method catalogue and request echoes, which
    can mention MoMo even when the hosted Checkout page cannot display it.
    """

    found: set[str] = set()

    def path_allows(path: tuple[str, ...], key: str) -> bool:
        joined = ".".join(path).lower()
        if any(marker in joined for marker in _NON_AVAILABLE_PATH_MARKERS):
            return False
        return key.lower() in _AUTHORITATIVE_METHOD_KEYS

    def add_text(value: str) -> None:
        lower = value.strip().lower()
        if not lower:
            return
        for marker in _KNOWN_METHOD_MARKERS:
            if marker == lower.replace("-", "_"):
                found.add(marker)

    def walk(node: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child_path = (*path, str(key))
                if path_allows(child_path, str(key)):
                    if isinstance(value, (list, tuple)):
                        for item in value:
                            if isinstance(item, str):
                                add_text(item)
                    elif isinstance(value, str):
                        add_text(value)
                walk(value, child_path)
            return
        if isinstance(node, (list, tuple)):
            for index, item in enumerate(node):
                walk(item, (*path, str(index)))

    for payload in payloads:
        walk(payload)
    return sorted(found)


def _is_checkout_concurrency_limited(exc: CheckoutError) -> bool:
    status_code = getattr(exc, "status_code", None)
    detail = str(getattr(exc, "detail", "") or "").lower()
    return status_code == 503 and "concurrency limit" in detail


async def _create_checkout_with_backoff(
    *,
    job: Job,
    chatgpt: ChatgptUpiClient,
    session: SessionBundle,
    logger: logging.LoggerAdapter,
    retry_delays: tuple[float, ...] = _CHECKOUT_RETRY_DELAYS_SECONDS,
) -> CheckoutState:
    attempts = len(retry_delays) + 1
    for attempt in range(1, attempts + 1):
        await await_cancelable(job, _CHECKOUT_GATE.acquire())
        try:
            return await await_cancelable(
                job,
                chatgpt.create_checkout(
                    session,
                    billing_country=_BILLING_COUNTRY,
                    billing_currency=_BILLING_CURRENCY,
                    language=_LANGUAGE,
                ),
            )
        except CheckoutError as exc:
            if not _is_checkout_concurrency_limited(exc):
                raise
            if attempt >= attempts:
                raise MomoCheckoutConcurrencyLimitError(attempts) from exc
            delay = retry_delays[attempt - 1]
            logger.warning(
                "momo_check checkout concurrency limited; "
                "retrying in %.1fs (attempt %d/%d)",
                delay,
                attempt + 1,
                attempts,
            )
        finally:
            _CHECKOUT_GATE.release()

        await await_cancelable(job, asyncio.sleep(delay))

    raise AssertionError("unreachable")


async def run_momo_check_post_login(
    *,
    job: Job,
    session: SessionBundle,
    stack: AsyncExitStack,
    logger: logging.LoggerAdapter,
    known_secrets: list[str],
    health_box: list[ProxyLeaseHealth | None],
    pools: ResolvedProxyPools,
    settings: SettingsRepository,
    require_promo: bool,
    qr_output_dir: Path,
) -> JobResult:
    del qr_output_dir

    pick_a = pick_and_materialize(pools.checkout_lines)
    if pick_a is not None:
        for secret in proxy_secret_values([pick_a.raw_line, pick_a.materialized_url]):
            if secret not in known_secrets:
                known_secrets.append(secret)
    else:
        logger.info("momo_check proxy A empty; using direct connection")

    pick_b = pick_and_materialize(pools.promotion_lines)
    if pick_b is not None:
        for secret in proxy_secret_values([pick_b.raw_line, pick_b.materialized_url]):
            if secret not in known_secrets:
                known_secrets.append(secret)

    client_a = await stack.enter_async_context(
        http.create_async_client(
            allow_redirects=True,
            timeout=_DEFAULT_HTTP_TIMEOUT_SECONDS,
            proxy=pick_a.materialized_url if pick_a is not None else None,
        )
    )
    client_b = client_a
    if pick_b is not None:
        client_b = await stack.enter_async_context(
            http.create_async_client(
                allow_redirects=True,
                timeout=_DEFAULT_HTTP_TIMEOUT_SECONDS,
                proxy=pick_b.materialized_url,
            )
        )
    else:
        logger.info(
            "momo_check proxy B empty; using checkout connection for promo update"
        )

    stop = check_stop(job)
    if stop is not None:
        return JobResult(status=stop.status, proxy_lease_health=health_box[0])

    chatgpt_a = ChatgptUpiClient(client_a, logger)  # type: ignore[arg-type]
    try:
        checkout = await _create_checkout_with_backoff(
            job=job,
            chatgpt=chatgpt_a,
            session=session,
            logger=logger,
        )
    except CheckoutError as exc:
        await _maybe_handle_already_paid(exc, chatgpt_a, session, job, logger)
        raise

    if checkout.processor_entity and checkout.processor_entity != _PROCESSOR:
        raise ProcessorEntityMismatchError(actual=checkout.processor_entity)

    if contains_already_paid_hint(checkout.raw_payload):
        await _raise_already_paid_from_entitlement(chatgpt_a, session, job, logger)

    promo_payload: dict[str, Any] | None = None
    if client_b is not None:
        chatgpt_b = ChatgptUpiClient(client_b, logger)  # type: ignore[arg-type]
        try:
            promo_payload = await await_cancelable(
                job,
                chatgpt_b.update_checkout_promo(session, checkout.checkout_session_id),
            )
        except PromoUpdateError as exc:
            await _maybe_handle_already_paid(exc, chatgpt_b, session, job, logger)
            raise
        if contains_already_paid_hint(promo_payload):
            await _raise_already_paid_from_entitlement(
                chatgpt_b, session, job, logger
            )

    stop = check_stop(job)
    if stop is not None:
        return JobResult(status=stop.status, proxy_lease_health=health_box[0])

    stripe = StripeUpiClient(
        client_a,
        settings,
        logger,  # type: ignore[arg-type]
        timeout_setting_key=_SETTING_STRIPE_TIMEOUT,
        browser_locale=_LANGUAGE,
        browser_timezone="Asia/Ho_Chi_Minh",
        locale="vi",
    )
    init_state = await await_cancelable(
        job,
        stripe.init(checkout.checkout_session_id, checkout.publishable_key),
    )
    if init_state.init_checksum and init_state.init_checksum not in known_secrets:
        known_secrets.append(init_state.init_checksum)

    apply_amount_gates(
        init_state.amount,
        require_promo=require_promo,
        expected_currency=_BILLING_CURRENCY.lower(),
    )

    methods = collect_payment_method_tokens(init_state.raw_payload)
    logger.info("momo_check payment_methods seen: %s", methods or "none")
    if not any(method == "momo" or "momo" in method for method in methods):
        raise MomoUnavailableError(methods)

    payment_link = f"{_PAYMENT_LINK_BASE}/{checkout.checkout_session_id}"
    logger.info("momo_check ok payment_link=%s", payment_link)
    return JobResult(
        status=JobStatus.QR_READY,
        payment_link=payment_link,
        plan="free",
        proxy_lease_health=health_box[0],
    )


__all__ = [
    "MomoCheckConfigError",
    "MomoCheckoutConcurrencyLimitError",
    "MomoUnavailableError",
    "collect_payment_method_tokens",
    "run_momo_check_post_login",
]
