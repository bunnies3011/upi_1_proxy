"""Post-login GCash native checkout -> Adyen redirect link output.

GCash is OpenAI-native for PH/PHP checkout. The expected checkout id is
`oaics_*`, not Stripe `cs_*`; do not send it to Stripe payment_pages.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Awaitable, Callable, Final

from app.core import http_client as http
from app.core.payment_flow import Job, JobResult, JobStatus, ProxyLeaseHealth
from app.core.proxy_pool import ProxyLease
from app.core.settings_store import SettingsRepository
from app.payments._chatgpt.login_client import (
    _CHATGPT_BASE_URL,
    _restore_cookies_scoped,
)
from app.payments._chatgpt.models import SessionBundle
from app.payments.gcash_direct.browser_qr import (
    GcashBrowserQrError,
    capture_gcash_browser_qr,
)
from app.payments.gcash_direct.headless_qr import (
    GcashHeadlessQrError,
    capture_gcash_headless_qr,
)
from app.payments.upi_direct.cancel_helpers import await_cancelable, check_stop
from app.payments.upi_direct.chatgpt_client import (
    ChatgptUpiClient,
    _PROCESSOR,
    contains_already_paid_hint,
)
from app.payments.upi_direct.errors import (
    AlreadyPaidConflictError,
    AlreadyPaidError,
    AlreadyPaidUnverifiedError,
    CheckoutError,
    NoFreeOfferError,
    UpiDirectFlowError,
)
from app.payments.upi_direct.models import ProxyPoolSelection, ResolvedProxyPools
from app.payments.upi_direct.proxy_pools import pick_and_materialize, proxy_secret_values

_DEFAULT_HTTP_TIMEOUT_SECONDS: Final[float] = 30.0
_COUNTRY: Final[str] = "PH"
_CURRENCY: Final[str] = "PHP"
_LANGUAGE: Final[str] = "en-PH"
_PROMO_ID: Final[str] = "plus-1-month-free"
_PLAN_NAME: Final[str] = "chatgptplusplan"
_ENDPOINT_CHECKOUT: Final[str] = "/backend-api/payments/checkout"
_ENDPOINT_CHECKOUT_UPDATE: Final[str] = "/backend-api/payments/checkout/update"
_ENDPOINT_TAXES: Final[str] = "/backend-api/payments/checkout/taxes"
_ENDPOINT_CONFIRM: Final[str] = "/backend-api/payments/checkout/confirm"
_ENDPOINT_START: Final[str] = (
    "/backend-api/payments/checkout/custom_payment_method/start"
)
_BODY_SNIPPET_MAX: Final[int] = 300
_DEFAULT_BILLING_PH: Final[dict[str, str]] = {
    "name": "Juan Dela Cruz",
    "line1": "Col. Bonny Serrano Avenue",
    "city": "Quezon City",
    "state": "Metro Manila",
    "postal_code": "1500",
    "country": _COUNTRY,
}


class GcashNotOfferedError(UpiDirectFlowError):
    def __init__(self, detail: str | None = None) -> None:
        super().__init__(
            error_code="gcash_not_offered",
            step="checkout",
            message=(
                "GCash not offered "
                f"(detail={detail or 'no custom_payment_methods'})"
            ),
        )


class GcashNativeError(UpiDirectFlowError):
    def __init__(
        self,
        *,
        step: str,
        detail: str | None = None,
        error_code: str = "gcash_native_failed",
    ) -> None:
        super().__init__(
            error_code=error_code,
            step=step,
            message=f"GCash native flow failed (detail={detail})",
        )


class GcashRedirectMissingError(UpiDirectFlowError):
    def __init__(self, detail: str | None = None) -> None:
        super().__init__(
            error_code="gcash_redirect_missing",
            step="payment_link",
            message=f"GCash start OK but redirect URL missing (detail={detail})",
        )


async def run_post_login(
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
    proxy_lease: ProxyLease | None = None,
    plus_signal_callback: Callable[[str, str], Awaitable[Any]] | None = None,
) -> JobResult:
    pick_a = pick_and_materialize(pools.checkout_lines)
    pick_b = pick_and_materialize(pools.promotion_lines)
    pick_lease = (
        ProxyPoolSelection(
            raw_line=proxy_lease.proxy_id,
            materialized_url=proxy_lease.materialized_url,
        )
        if proxy_lease is not None
        else None
    )
    pick_main = pick_lease or pick_b
    pick_promo = pick_b or pick_main
    lease_origin = "job_lease"
    if proxy_lease is not None:
        lease_raw = proxy_lease.proxy_id.strip()
        if lease_raw in {line.strip() for line in pools.promotion_lines}:
            lease_origin = "promotion_B_lease"
        elif lease_raw in {line.strip() for line in pools.checkout_lines}:
            lease_origin = "checkout_A_lease"
    logger.info(
        "gcash proxy main=%s promo=%s",
        (
            lease_origin
            if pick_lease is not None
            else "promotion_B"
            if pick_b is not None
            else "direct"
        ),
        (
            "promotion_B"
            if pick_b is not None
            else "main_reuse"
            if pick_main is not None
            else "direct"
        ),
    )

    for pick in (pick_main, pick_a, pick_b, pick_promo):
        if pick is None:
            continue
        for secret in proxy_secret_values([pick.raw_line, pick.materialized_url]):
            if secret not in known_secrets:
                known_secrets.append(secret)

    request_timeout = await _setting_int(
        settings,
        "gcash_direct.stripe_request_timeout_seconds",
        int(_DEFAULT_HTTP_TIMEOUT_SECONDS),
    )
    approve_error_retries = await _setting_int(
        settings,
        "gcash_direct.approve_error_retries",
        1,
    )
    client = await stack.enter_async_context(
        http.create_async_client(
            allow_redirects=True,
            timeout=float(request_timeout),
            proxy=pick_main.materialized_url if pick_main is not None else None,
        )
    )
    promo_client = client
    if (
        pick_promo is not None
        and (
            pick_main is None
            or pick_promo.materialized_url != pick_main.materialized_url
        )
    ):
        promo_client = await stack.enter_async_context(
            http.create_async_client(
                allow_redirects=True,
                timeout=float(request_timeout),
                proxy=pick_promo.materialized_url,
            )
        )

    stop = check_stop(job)
    if stop is not None:
        return JobResult(status=stop.status, proxy_lease_health=health_box[0])

    chatgpt = ChatgptUpiClient(client, logger)  # type: ignore[arg-type]
    try:
        checkout = await await_cancelable(
            job,
            _create_gcash_checkout(client, session, logger, include_promo=False),
        )
    except CheckoutError as exc:
        await _maybe_handle_already_paid(exc, chatgpt, session, job, logger)
        if _is_billing_country_mismatch(exc):
            logger.error(
                "chatgpt_checkout billing_country_mismatch on main checkout path; "
                "GCash checkout country must stay PH before confirm"
            )
        raise

    if contains_already_paid_hint(checkout):
        await _raise_already_paid_from_entitlement(chatgpt, session, job, logger)

    session_id = _require_str(checkout, "checkout_session_id", step="checkout")
    processor_entity = _read_processor_entity(checkout)
    logger.info(
        "chatgpt_checkout ok checkout_session_id=%s provider=%s currency=%s",
        session_id,
        checkout.get("checkout_provider") or "-",
        _read_billing_currency(checkout) or "-",
    )

    cpm_id = _extract_custom_payment_method_id(checkout)
    if not cpm_id:
        logger.info(
            "gcash_check available=false payment_method_types=%s",
            checkout.get("payment_method_types") or "none",
        )
        raise GcashNotOfferedError(
            detail=f"payment_method_types={checkout.get('payment_method_types')}"
        )
    logger.info("gcash_check available=true custom_payment_method_id=%s", cpm_id)

    email = _read_checkout_email(checkout) or session.email
    taxes_bare = await await_cancelable(
        job,
        _submit_gcash_taxes(
            client,
            session,
            session_id,
            processor_entity=processor_entity,
            checkout_email=email,
        ),
    )
    bare_amount_minor, bare_amount_label = _extract_gcash_amount(taxes_bare)
    logger.info(
        "gcash_taxes_bare ok checkout_session_id=%s amount=%s label=%s",
        session_id,
        bare_amount_minor,
        bare_amount_label or "-",
    )

    promo_payload: dict[str, Any] | None = None
    if require_promo:
        promo_payload = await await_cancelable(
            job,
            _update_gcash_checkout_promo(
                promo_client,
                session,
                session_id,
                processor_entity=processor_entity,
            ),
        )
        promo_cpm_id = _extract_custom_payment_method_id(promo_payload)
        if promo_cpm_id:
            cpm_id = promo_cpm_id
        logger.info(
            "chatgpt_promo_update ok checkout_session_id=%s promo_pool=%s cpmt=%s",
            session_id,
            "promotion_B" if pick_b is not None else "main_reuse",
            promo_cpm_id or "retained",
        )
    else:
        logger.info("chatgpt_promo_update skipped require_promo=false")

    taxes_final = await await_cancelable(
        job,
        _submit_gcash_taxes(
            client,
            session,
            session_id,
            processor_entity=processor_entity,
            checkout_email=email,
        ),
    )
    amount_minor, amount_label = _extract_gcash_amount(taxes_final)
    logger.info(
        "gcash_taxes_final ok checkout_session_id=%s amount=%s label=%s bare_amount=%s",
        session_id,
        amount_minor,
        amount_label or "-",
        bare_amount_minor,
    )
    if require_promo and amount_minor is not None and amount_minor > 0:
        label = amount_label or str(amount_minor)
        raise NoFreeOfferError(
            amount_minor=amount_minor,
            message=f"No free offer for GCash (fee={label})",
        )

    confirm_payload = await _gcash_confirm_with_error_retries(
        job=job,
        base_client=client,
        session=session,
        session_id=session_id,
        processor_entity=processor_entity,
        custom_payment_method_id=cpm_id,
        chatgpt=chatgpt,
        logger=logger,
        pools=pools,
        known_secrets=known_secrets,
        request_timeout=float(request_timeout),
        approve_error_retries=approve_error_retries,
    )
    if contains_already_paid_hint(confirm_payload):
        await _raise_already_paid_from_entitlement(chatgpt, session, job, logger)
    confirm_status = (
        confirm_payload.get("status") if isinstance(confirm_payload, dict) else None
    )
    logger.info(
        "gcash_confirm ok checkout_session_id=%s status=%s keys=%s",
        session_id,
        confirm_status,
        _payload_keys(confirm_payload),
    )

    start_payload = await await_cancelable(
        job,
        _gcash_start_native(
            client,
            session,
            session_id,
            cpm_id,
            processor_entity=processor_entity,
        ),
    )
    payment_link = _find_gcash_redirect_url(start_payload)
    if not payment_link:
        raise GcashRedirectMissingError(detail=f"keys={_payload_keys(start_payload)}")

    logger.info("payment_link_ready payment_link=%s", payment_link)
    final_payment_link = payment_link
    browser_payment_link = payment_link
    artifact_path: str | None = None
    qr_expires_at: float | None = None
    if await _setting_bool(settings, "gcash_direct.headless_qr_enabled", True):
        try:
            poll_seconds = await _setting_int(
                settings, "gcash_direct.headless_poll_seconds", 300
            )
            headless_result = await await_cancelable(
                job,
                capture_gcash_headless_qr(
                    payment_link=payment_link,
                    session=session,
                    job_id=job.job_id,
                    checkout_session_id=session_id,
                    processor_entity=processor_entity,
                    output_dir=qr_output_dir,
                    proxy_url=pick_main.materialized_url if pick_main is not None else None,
                    request_timeout=float(request_timeout),
                    poll_seconds=poll_seconds,
                    logger=logger,
                    plus_signal_callback=plus_signal_callback,
                ),
            )
            artifact_path = str(headless_result.artifact_path)
            final_payment_link = headless_result.payment_link
            browser_payment_link = headless_result.gcash_page_url
            qr_expires_at = headless_result.expires_at
            logger.info(
                "payment_link_resolved payment_link=%s source=gcash_headless uuid=%s",
                final_payment_link,
                headless_result.consult_uuid,
            )
        except GcashHeadlessQrError as exc:
            logger.warning("gcash_headless_qr failed: %s", exc)
        except Exception as exc:  # noqa: BLE001 - fallback to browser capture.
            logger.warning(
                "gcash_headless_qr failed: %s:%s",
                type(exc).__name__,
                exc,
            )
    if await _setting_bool(settings, "gcash_direct.browser_qr_enabled", True):
        try:
            timeout_seconds = await _setting_int(
                settings, "gcash_direct.browser_qr_timeout_seconds", 60
            )
            headless = await _setting_bool(
                settings, "gcash_direct.browser_headless", True
            )
            capture_qr = await _setting_bool(
                settings, "gcash_direct.browser_qr_capture_enabled", True
            )
            if artifact_path is not None:
                logger.info(
                    "gcash_browser_qr %s source=gcash_headless link=gcash_page_url",
                    "capture enabled" if capture_qr else "capture disabled",
                )
            hold_seconds = await _setting_int(
                settings, "gcash_direct.browser_hold_seconds", 300
            )
            hold_max_active = await _setting_int(
                settings, "gcash_direct.browser_hold_max_active", 50
            )
            # Open the exact GCash page used for the QR when headless produced it.
            # Reopening the original Adyen link can mint a different GCash auth id,
            # so the scanned QR would never callback into the held browser page.
            browser_proxy_url = None
            browser_result = await await_cancelable(
                job,
                asyncio.wait_for(
                    capture_gcash_browser_qr(
                        payment_link=browser_payment_link,
                        session=session,
                        job_id=job.job_id,
                        output_dir=qr_output_dir,
                        proxy_url=browser_proxy_url,
                        timeout_seconds=timeout_seconds,
                        headless=headless,
                        capture_qr=capture_qr,
                        hold_seconds=hold_seconds,
                        hold_max_active=hold_max_active,
                        logger=logger,
                        plus_signal_callback=plus_signal_callback,
                    ),
                    timeout=max(5, timeout_seconds) + 25,
                ),
            )
            if browser_result.final_url:
                final_payment_link = browser_result.final_url
                logger.info("payment_link_resolved payment_link=%s", final_payment_link)
            if browser_result.artifact_path is not None:
                artifact_path = str(browser_result.artifact_path)
        except asyncio.TimeoutError:
            logger.warning(
                "gcash_browser_qr failed: browser_timeout after=%ss",
                max(5, timeout_seconds) + 25,
            )
        except GcashBrowserQrError as exc:
            logger.warning("gcash_browser_qr failed: %s", exc)
        except Exception as exc:  # noqa: BLE001 - QR capture must never hide link.
            logger.warning(
                "gcash_browser_qr failed: %s:%s",
                type(exc).__name__,
                exc,
            )

    return JobResult(
        status=JobStatus.QR_READY,
        artifact_path=artifact_path,
        payment_link=final_payment_link,
        qr_expires_at=qr_expires_at,
        plan="free",
        proxy_lease_health=health_box[0],
    )


async def _create_gcash_checkout(
    client: http.AsyncSession,
    session: SessionBundle,
    logger: logging.LoggerAdapter,
    *,
    include_promo: bool,
) -> dict[str, Any]:
    _restore_cookies_scoped(client, session.cookies)
    body: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": _PLAN_NAME,
        "billing_details": {
            "country": _COUNTRY,
            "currency": _CURRENCY,
        },
        "checkout_ui_mode": "custom",
    }
    if include_promo:
        body["promo_campaign"] = {
            "promo_campaign_id": _PROMO_ID,
            "is_coupon_from_query_param": False,
        }
    payload = await _post_chatgpt_json(
        client,
        session,
        _ENDPOINT_CHECKOUT,
        body,
        language=_LANGUAGE,
        referer=f"{_CHATGPT_BASE_URL}/",
        step="checkout",
    )
    session_id = payload.get("checkout_session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise CheckoutError(detail="missing checkout_session_id")
    if not session_id.strip().startswith("oaics_"):
        logger.warning(
            "gcash checkout returned non-native session id prefix=%s",
            _session_prefix(session_id),
        )
    return payload


async def _update_gcash_checkout_promo(
    client: http.AsyncSession,
    session: SessionBundle,
    session_id: str,
    *,
    processor_entity: str,
) -> dict[str, Any]:
    body = {
        "checkout_session_id": session_id,
        "processor_entity": processor_entity,
        "plan_name": _PLAN_NAME,
        "price_interval": "month",
        "seat_quantity": 1,
        "promo_campaign": {
            "promo_campaign_id": _PROMO_ID,
            "is_coupon_from_query_param": False,
        },
    }
    return await _post_chatgpt_json(
        client,
        session,
        _ENDPOINT_CHECKOUT_UPDATE,
        body,
        language=_LANGUAGE,
        referer=f"{_CHATGPT_BASE_URL}/checkout/{processor_entity}/{session_id}",
        step="promo_update",
    )


async def _submit_gcash_taxes(
    client: http.AsyncSession,
    session: SessionBundle,
    session_id: str,
    *,
    processor_entity: str,
    checkout_email: str,
) -> dict[str, Any]:
    billing = _DEFAULT_BILLING_PH
    body = {
        "checkout_session_id": session_id,
        "checkout_email": checkout_email,
        "billing_country": billing["country"],
        "billing_name": billing["name"],
        "currency": _CURRENCY.lower(),
        "processor_entity": processor_entity,
        "billing_address": {
            "line1": billing["line1"],
            "city": billing["city"],
            "country": billing["country"],
            "postal_code": billing["postal_code"],
            "state": billing["state"],
        },
    }
    return await _post_chatgpt_json(
        client,
        session,
        _ENDPOINT_TAXES,
        body,
        language=_LANGUAGE,
        referer=f"{_CHATGPT_BASE_URL}/checkout/{processor_entity}/{session_id}",
        step="taxes",
    )


async def _gcash_confirm_native(
    client: http.AsyncSession,
    session: SessionBundle,
    session_id: str,
    custom_payment_method_id: str,
    *,
    processor_entity: str,
) -> dict[str, Any]:
    body = {
        "checkout_session_id": session_id,
        "selected_payment_method_type": custom_payment_method_id,
    }
    return await _post_chatgpt_json(
        client,
        session,
        _ENDPOINT_CONFIRM,
        body,
        language=_LANGUAGE,
        referer=f"{_CHATGPT_BASE_URL}/checkout/{processor_entity}/{session_id}",
        step="gcash_confirm",
    )


def _pick_confirm_proxy_for_attempt(
    pools: ResolvedProxyPools,
    attempt: int,
) -> ProxyPoolSelection | None:
    lines = [line.strip() for line in pools.checkout_lines if line.strip()]
    if not lines:
        return None
    raw = lines[(max(1, attempt) - 1) % len(lines)]
    return pick_and_materialize([raw])


async def _gcash_confirm_with_error_retries(
    *,
    job: Job,
    base_client: http.AsyncSession,
    session: SessionBundle,
    session_id: str,
    processor_entity: str,
    custom_payment_method_id: str,
    chatgpt: ChatgptUpiClient,
    logger: logging.LoggerAdapter,
    pools: ResolvedProxyPools,
    known_secrets: list[str],
    request_timeout: float,
    approve_error_retries: int,
) -> dict[str, Any]:
    max_attempts = max(1, int(approve_error_retries))
    last_detail = "unknown"
    for attempt in range(1, max_attempts + 1):
        attempt_client = base_client
        pick = _pick_confirm_proxy_for_attempt(pools, attempt)
        proxy_label = "direct"
        if pick is not None:
            promotion_set = {line.strip() for line in pools.promotion_lines}
            proxy_label = (
                "promotion_B"
                if pick.raw_line.strip() in promotion_set
                else "checkout_A"
            )
            for secret in proxy_secret_values([pick.raw_line, pick.materialized_url]):
                if secret not in known_secrets:
                    known_secrets.append(secret)
        logger.info(
            "gcash_confirm attempt %s/%s proxy=%s",
            attempt,
            max_attempts,
            proxy_label,
        )
        try:
            if pick is not None:
                async with http.create_async_client(
                    allow_redirects=True,
                    timeout=float(request_timeout),
                    proxy=pick.materialized_url,
                ) as rotated_client:
                    attempt_client = rotated_client
                    payload = await await_cancelable(
                        job,
                        _gcash_confirm_native(
                            attempt_client,
                            session,
                            session_id,
                            custom_payment_method_id,
                            processor_entity=processor_entity,
                        ),
                    )
            else:
                async with http.create_async_client(
                    allow_redirects=True,
                    timeout=float(request_timeout),
                    proxy=None,
                ) as direct_client:
                    attempt_client = direct_client
                    payload = await await_cancelable(
                        job,
                        _gcash_confirm_native(
                            attempt_client,
                            session,
                            session_id,
                            custom_payment_method_id,
                            processor_entity=processor_entity,
                        ),
                    )
        except GcashNativeError as exc:
            last_detail = str(exc)
            if attempt >= max_attempts:
                raise
            logger.warning(
                "gcash_confirm failed attempt=%s/%s; retrying detail=%s",
                attempt,
                max_attempts,
                last_detail,
            )
            continue

        if contains_already_paid_hint(payload):
            await _raise_already_paid_from_entitlement(chatgpt, session, job, logger)

        status = payload.get("status") if isinstance(payload, dict) else None
        if isinstance(status, str) and status.lower() in {
            "blocked",
            "rejected",
            "failed",
            "declined",
        }:
            last_detail = f"status={status} keys={_payload_keys(payload)}"
            if attempt >= max_attempts:
                raise GcashNativeError(step="gcash_confirm", detail=last_detail)
            logger.warning(
                "gcash_confirm blocked attempt=%s/%s; retrying detail=%s",
                attempt,
                max_attempts,
                last_detail,
            )
            continue

        return payload

    raise GcashNativeError(step="gcash_confirm", detail=last_detail)


async def _gcash_start_native(
    client: http.AsyncSession,
    session: SessionBundle,
    session_id: str,
    custom_payment_method_id: str,
    *,
    processor_entity: str,
) -> dict[str, Any]:
    body = {
        "checkout_session_id": session_id,
        "custom_payment_method_type_id": custom_payment_method_id,
    }
    return await _post_chatgpt_json(
        client,
        session,
        _ENDPOINT_START,
        body,
        language=_LANGUAGE,
        referer=f"{_CHATGPT_BASE_URL}/checkout/{processor_entity}/{session_id}",
        step="gcash_start",
    )


async def _post_chatgpt_json(
    client: http.AsyncSession,
    session: SessionBundle,
    target: str,
    body: dict[str, Any],
    *,
    language: str,
    referer: str,
    step: str,
) -> dict[str, Any]:
    _restore_cookies_scoped(client, session.cookies)
    lang = language.strip() or _LANGUAGE
    headers = {
        "Authorization": f"Bearer {session.access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": f"{lang},{lang.split('-', 1)[0]};q=0.9",
        "Origin": _CHATGPT_BASE_URL,
        "Referer": referer,
        "OAI-Language": lang,
        "x-openai-target-path": target,
        "x-openai-target-route": target,
    }
    try:
        response = await client.post(
            f"{_CHATGPT_BASE_URL}{target}",
            json=body,
            headers=headers,
        )
    except (
        http.TimeoutException,
        http.NetworkError,
        http.TransportError,
    ) as exc:
        if step == "checkout":
            raise CheckoutError(detail=f"transport:{exc.__class__.__name__}") from exc
        raise GcashNativeError(step=step, detail=f"transport:{exc.__class__.__name__}") from exc

    if not (200 <= response.status_code < 300):
        snippet = _body_snippet(response)
        if step == "checkout":
            err = CheckoutError(detail=f"http_{response.status_code}:{snippet}")
            setattr(err, "response_snippet", snippet)
            setattr(err, "status_code", response.status_code)
            try:
                setattr(err, "response_payload", response.json())
            except ValueError:
                setattr(err, "response_payload", {"raw": snippet})
            raise err
        raise GcashNativeError(
            step=step,
            detail=f"http_{response.status_code}:{snippet}",
        )

    try:
        payload = response.json()
    except ValueError as exc:
        if step == "checkout":
            raise CheckoutError(detail="invalid_json") from exc
        raise GcashNativeError(step=step, detail="invalid_json") from exc
    if not isinstance(payload, dict):
        if step == "checkout":
            raise CheckoutError(detail="payload_not_dict")
        raise GcashNativeError(step=step, detail="payload_not_dict")
    return payload


def _extract_custom_payment_method_id(payload: dict[str, Any]) -> str | None:
    cpms = payload.get("custom_payment_methods")
    if not isinstance(cpms, list):
        nested = payload.get("checkout_session")
        if isinstance(nested, dict):
            cpms = nested.get("custom_payment_methods")
    if not isinstance(cpms, list):
        return None
    for item in cpms:
        if not isinstance(item, dict):
            continue
        cpm_id = item.get("id")
        if isinstance(cpm_id, str) and cpm_id.strip().startswith("cpmt_"):
            return cpm_id.strip()
    return None


def _extract_gcash_amount(payload: dict[str, Any]) -> tuple[int | None, str | None]:
    source = payload
    nested = payload.get("checkout_session")
    if isinstance(nested, dict):
        source = nested
    candidates: list[Any] = [
        _dig(source, ("checkout_state", "total", "total", "minorUnitsAmount")),
        _dig(source, ("total", "total", "minorUnitsAmount")),
        _dig(source, ("total_summary", "due")),
        _dig(source, ("total_details", "amount_total")),
        _dig(source, ("pricing", "amount_total")),
        _dig(source, ("amount", "total")),
        source.get("amount_total"),
    ]
    amount: int | None = None
    for value in candidates:
        if isinstance(value, int) and not isinstance(value, bool):
            amount = value
            break
        if isinstance(value, str) and value.strip().isdigit():
            amount = int(value.strip())
            break

    labels: list[Any] = [
        _dig(source, ("checkout_state", "total", "total", "localizedString")),
        _dig(source, ("checkout_state", "lineItems", 0, "discount", "amount")),
        _dig(source, ("total", "total", "localizedString")),
        _dig(source, ("total_summary", "localizedString")),
    ]
    label = next((value for value in labels if isinstance(value, str) and value), None)
    return amount, label


def _find_gcash_redirect_url(data: Any) -> str | None:
    if isinstance(data, dict):
        next_action = data.get("next_action")
        if isinstance(next_action, dict):
            url = next_action.get("url")
            if isinstance(url, str) and url.startswith("http"):
                return url.strip()

    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            lower = node.lower()
            if lower.startswith("http") and any(
                marker in lower
                for marker in ("adyen", "gcash", "checkoutshopper", "redirect")
            ):
                found.append(node.strip())
            return
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
            return
        if isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return found[0] if found else None


def _dig(node: Any, path: tuple[Any, ...]) -> Any:
    cur = node
    for key in path:
        if isinstance(key, int):
            if not isinstance(cur, list) or key >= len(cur):
                return None
            cur = cur[key]
            continue
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _require_str(payload: dict[str, Any], key: str, *, step: str) -> str:
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise GcashNativeError(step=step, detail=f"missing {key}")


def _read_billing_currency(payload: dict[str, Any]) -> str | None:
    value = _dig(payload, ("billing_details", "currency"))
    return value if isinstance(value, str) else None


def _read_processor_entity(payload: dict[str, Any]) -> str:
    value = payload.get("processor_entity")
    return value.strip() if isinstance(value, str) and value.strip() else _PROCESSOR


def _read_checkout_email(payload: dict[str, Any]) -> str | None:
    value = _dig(payload, ("checkout_state", "email"))
    if isinstance(value, str) and value.strip():
        return value.strip()
    value = payload.get("email")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _payload_keys(payload: Any) -> str:
    if isinstance(payload, dict):
        return ",".join(str(key) for key in list(payload)[:20])
    return type(payload).__name__


def _body_snippet(response: http.Response) -> str:
    try:
        text = response.text or ""
    except Exception:
        text = ""
    return text[:_BODY_SNIPPET_MAX]


def _is_billing_country_mismatch(exc: CheckoutError) -> bool:
    detail = str(getattr(exc, "detail", "") or "").lower()
    if "billing country must match request country" in detail:
        return True
    payload = getattr(exc, "response_payload", None)
    return isinstance(payload, dict) and (
        "billing country must match request country"
        in str(payload).lower()
    )


def _session_prefix(session_id: str) -> str:
    return session_id.split("_", 1)[0] if "_" in session_id else session_id[:12]


async def _setting_bool(
    settings: SettingsRepository, key: str, default: bool
) -> bool:
    value = await settings.get(key)
    return value if isinstance(value, bool) else default


async def _setting_int(settings: SettingsRepository, key: str, default: int) -> int:
    value = await settings.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


async def _maybe_handle_already_paid(
    exc: Exception,
    client: ChatgptUpiClient,
    session: SessionBundle,
    job: Job,
    logger: logging.LoggerAdapter,
) -> None:
    payload = getattr(exc, "response_payload", None)
    snippet = getattr(exc, "response_snippet", "") or str(exc)
    if contains_already_paid_hint(payload) or contains_already_paid_hint(snippet):
        await _raise_already_paid_from_entitlement(client, session, job, logger)


async def _raise_already_paid_from_entitlement(
    client: ChatgptUpiClient,
    session: SessionBundle,
    job: Job,
    logger: logging.LoggerAdapter,
) -> None:
    evidence = await await_cancelable(
        job, client.verify_live_plus_entitlement(session)
    )
    logger.info("already_paid hint; live entitlement=%s", evidence)
    if evidence == "plus":
        raise AlreadyPaidError()
    if evidence == "free":
        raise AlreadyPaidConflictError()
    raise AlreadyPaidUnverifiedError()


__all__ = ["run_post_login"]
