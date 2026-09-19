"""Post-login checkout -> Stripe Kakao Pay confirm -> payment link output."""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
    AlreadyPaidConflictError,
    AlreadyPaidError,
    AlreadyPaidUnverifiedError,
    ApproveFailedError,
    ApproveOutcomeUnknownError,
    CheckoutError,
    ConfigError,
    ConfirmFailedError,
    ProcessorEntityMismatchError,
    PromoUpdateError,
    UpiDirectFlowError,
)
from app.payments.upi_direct.models import (
    CheckoutState,
    ElementsState,
    ExpectedPaymentState,
    ResolvedProxyPools,
    StripeInitState,
)
from app.payments.upi_direct.proxy_pools import pick_and_materialize, proxy_secret_values
from app.payments.upi_direct.stripe_client import StripeUpiClient
from app.payments.kakao_direct.profile_generator import generate_korea_profile

_DEFAULT_HTTP_TIMEOUT_SECONDS = 30.0
_SETTING_APPROVE_ERROR_RETRIES = "kakao_direct.approve_error_retries"
_DEFAULT_APPROVE_ERROR_RETRIES = 1
_COUNTRY = "KR"
_CURRENCY = "krw"
_LANGUAGE = "ko-KR"
_PAYMENT_METHOD = "kakao_pay"


class NoPaymentLinkFoundError(UpiDirectFlowError):
    def __init__(self, detail: str | None = None) -> None:
        self.detail = detail
        super().__init__(
            error_code="no_payment_link_found",
            step="payment_link",
            message=f"No Kakao Pay redirect link found (detail={detail})",
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
) -> JobResult:
    del qr_output_dir

    pick_a = pick_and_materialize(pools.checkout_lines)
    if pick_a is None:
        raise ConfigError(detail="proxy_checkout pool empty")
    for secret in proxy_secret_values([pick_a.raw_line, pick_a.materialized_url]):
        if secret not in known_secrets:
            known_secrets.append(secret)

    pick_b = pick_and_materialize(pools.promotion_lines)
    if pick_b is not None:
        for secret in proxy_secret_values([pick_b.raw_line, pick_b.materialized_url]):
            if secret not in known_secrets:
                known_secrets.append(secret)

    client_a = await stack.enter_async_context(
        http.create_async_client(
            allow_redirects=True,
            timeout=_DEFAULT_HTTP_TIMEOUT_SECONDS,
            proxy=pick_a.materialized_url,
        )
    )
    client_b = None
    if pick_b is not None:
        client_b = await stack.enter_async_context(
            http.create_async_client(
                allow_redirects=True,
                timeout=_DEFAULT_HTTP_TIMEOUT_SECONDS,
                proxy=pick_b.materialized_url,
            )
        )
    else:
        logger.info("promo proxy B empty; skipping promo update")

    stop = check_stop(job)
    if stop is not None:
        return JobResult(status=stop.status, proxy_lease_health=health_box[0])

    chatgpt_a = ChatgptUpiClient(client_a, logger)  # type: ignore[arg-type]
    try:
        checkout = await await_cancelable(
            job,
            chatgpt_a.create_checkout(
                session,
                billing_country=_COUNTRY,
                billing_currency=_CURRENCY.upper(),
                language=_LANGUAGE,
            ),
        )
    except CheckoutError as exc:
        await _maybe_handle_already_paid(exc, chatgpt_a, session, job, logger)
        raise

    if checkout.processor_entity and checkout.processor_entity != _PROCESSOR:
        raise ProcessorEntityMismatchError(actual=checkout.processor_entity)

    if contains_already_paid_hint(checkout.raw_payload):
        await _raise_already_paid_from_entitlement(chatgpt_a, session, job, logger)

    if client_b is not None:
        chatgpt_b = ChatgptUpiClient(client_b, logger)  # type: ignore[arg-type]
        try:
            promo_payload = await await_cancelable(
                job,
                chatgpt_b.update_checkout_promo(
                    session,
                    checkout.checkout_session_id,
                    language=_LANGUAGE,
                ),
            )
        except PromoUpdateError as exc:
            await _maybe_handle_already_paid(exc, chatgpt_b, session, job, logger)
            raise
        if contains_already_paid_hint(promo_payload):
            await _raise_already_paid_from_entitlement(
                chatgpt_b, session, job, logger
            )

    stripe = StripeUpiClient(
        client_a,
        settings,
        logger,  # type: ignore[arg-type]
        timeout_setting_key="kakao_direct.stripe_request_timeout_seconds",
        browser_locale=_LANGUAGE,
        browser_timezone="Asia/Seoul",
        locale="ko",
        confirm_error_label="Kakao Pay",
    )
    init_state = await await_cancelable(
        job,
        stripe.init(checkout.checkout_session_id, checkout.publishable_key),
    )
    if init_state.init_checksum and init_state.init_checksum not in known_secrets:
        known_secrets.append(init_state.init_checksum)

    amount = apply_amount_gates(
        init_state.amount,
        require_promo=require_promo,
        expected_currency=_CURRENCY,
    )
    assert amount.amount_minor is not None

    elements = await await_cancelable(
        job,
        stripe.elements_sessions(
            checkout.checkout_session_id,
            checkout.publishable_key,
            amount.amount_minor,
            currency=_CURRENCY,
            payment_method_types=(_PAYMENT_METHOD,),
        ),
    )
    token_cfg = await await_cancelable(job, stripe.ensure_token_config())
    if token_cfg is not None:
        for field in (
            getattr(token_cfg, "rv", None),
            getattr(token_cfg, "sv", None),
            getattr(token_cfg, "rv_ts", None),
            getattr(token_cfg, "bundle_hash", None),
        ):
            if isinstance(field, str) and field and field not in known_secrets:
                known_secrets.append(field)

    approve_error_retries = await _resolve_approve_error_retries(settings)
    return await _run_confirm_approve_link(
        job=job,
        session=session,
        chatgpt=chatgpt_a,
        stripe=stripe,
        checkout=checkout,
        init_state=init_state,
        elements=elements,
        amount_minor=amount.amount_minor,
        logger=logger,
        health_box=health_box,
        token_config=token_cfg,
        approve_error_retries=approve_error_retries,
    )


async def _run_confirm_approve_link(
    *,
    job: Job,
    session: SessionBundle,
    chatgpt: ChatgptUpiClient,
    stripe: StripeUpiClient,
    checkout: CheckoutState,
    init_state: StripeInitState,
    elements: ElementsState,
    amount_minor: int,
    logger: logging.LoggerAdapter,
    health_box: list[ProxyLeaseHealth | None],
    token_config: Any,
    approve_error_retries: int,
) -> JobResult:
    expected = ExpectedPaymentState(
        checkout_session_id=checkout.checkout_session_id,
        page_id=init_state.page_id,
        elements_session_id=elements.session_id,
        amount_minor=amount_minor,
        currency=_CURRENCY,
        publishable_key=checkout.publishable_key,
        init_checksum=init_state.init_checksum,
        init_config_id=init_state.config_id,
        elements_config_id=elements.config_id,
    )

    stop = check_stop(job)
    if stop is not None:
        return JobResult(status=stop.status, proxy_lease_health=health_box[0])

    confirm = await await_cancelable(
        job,
        stripe.confirm_kakao_pay(
            expected,
            session.email,
            token_config=token_config,
            billing_details=_build_korea_billing_details(session.email),
        ),
    )
    if contains_already_paid_hint(confirm.data):
        await _raise_already_paid_from_entitlement(chatgpt, session, job, logger)

    candidates = [confirm.data] if confirm.data else []
    if _extract_redirect_url(*candidates):
        logger.info("confirm redirect link candidate found")
    try:
        refresh_pre = await await_cancelable(job, stripe.payment_page_refresh(expected))
        candidates.append(refresh_pre)
        if _extract_redirect_url(refresh_pre):
            logger.info("pre-approve refresh redirect link candidate found")
    except ConfirmFailedError as exc:
        logger.warning("pre-approve refresh failed: %s", exc)

    approve_stop = await _approve_checkout_with_error_retries(
        job=job,
        session=session,
        chatgpt=chatgpt,
        stripe=stripe,
        expected=expected,
        checkout_session_id=checkout.checkout_session_id,
        candidates=candidates,
        logger=logger,
        health_box=health_box,
        approve_error_retries=approve_error_retries,
    )
    if approve_stop is not None:
        return approve_stop

    try:
        refresh_post = await await_cancelable(job, stripe.payment_page_refresh(expected))
        candidates.append(refresh_post)
    except ConfirmFailedError as exc:
        logger.warning("post-approve refresh failed: %s", exc)

    payment_link = _extract_redirect_url(*candidates)
    if not payment_link:
        raise NoPaymentLinkFoundError(detail="missing redirect_to_url.url")

    logger.info("payment_link_ready payment_link=%s", payment_link)
    return JobResult(
        status=JobStatus.QR_READY,
        payment_link=payment_link,
        proxy_lease_health=health_box[0],
    )


async def _approve_checkout_with_error_retries(
    *,
    job: Job,
    session: SessionBundle,
    chatgpt: ChatgptUpiClient,
    stripe: StripeUpiClient,
    expected: ExpectedPaymentState,
    checkout_session_id: str,
    candidates: list[dict[str, Any] | None],
    logger: logging.LoggerAdapter,
    health_box: list[ProxyLeaseHealth | None],
    approve_error_retries: int,
) -> JobResult | None:
    max_attempts = max(1, int(approve_error_retries))
    for attempt in range(1, max_attempts + 1):
        stop = check_stop(job)
        if stop is not None:
            return JobResult(status=stop.status, proxy_lease_health=health_box[0])

        logger.info("approve attempt %s/%s", attempt, max_attempts)
        approve = await await_cancelable(
            job,
            chatgpt.approve(
                session,
                checkout_session_id,
                language=_LANGUAGE,
            ),
        )
        if contains_already_paid_hint(approve.data) or contains_already_paid_hint(
            approve.result or ""
        ):
            await _raise_already_paid_from_entitlement(chatgpt, session, job, logger)

        if approve.ok:
            logger.info("approve ok attempt=%s/%s", attempt, max_attempts)
            return None
        if not approve.ambiguous:
            detail = f"status={approve.http_status} result={approve.result}"
            raise ApproveFailedError.from_outcome(
                http_status=approve.http_status,
                result=approve.result,
                detail=detail,
            )

        try:
            await _reconcile_ambiguous_approve(
                job=job,
                session=session,
                chatgpt=chatgpt,
                stripe=stripe,
                expected=expected,
                candidates=candidates,
                logger=logger,
            )
            return None
        except ApproveOutcomeUnknownError:
            if attempt >= max_attempts:
                raise
            logger.warning(
                "approve ambiguous unresolved attempt=%s/%s; retrying approve",
                attempt,
                max_attempts,
            )

    return None


async def _reconcile_ambiguous_approve(
    *,
    job: Job,
    session: SessionBundle,
    chatgpt: ChatgptUpiClient,
    stripe: StripeUpiClient,
    expected: ExpectedPaymentState,
    candidates: list[dict[str, Any] | None],
    logger: logging.LoggerAdapter,
) -> None:
    if _extract_redirect_url(*candidates):
        logger.info("approve ambiguous reconciled via existing redirect link")
        return

    try:
        refresh_rec = await await_cancelable(job, stripe.payment_page_refresh(expected))
    except Exception as exc:
        raise ApproveOutcomeUnknownError(
            message=f"approve ambiguous; refresh failed: {exc}"
        ) from exc

    if contains_already_paid_hint(refresh_rec):
        await _raise_already_paid_from_entitlement(chatgpt, session, job, logger)
    candidates.append(refresh_rec)
    if _extract_redirect_url(refresh_rec):
        logger.info("approve reconciled via refresh redirect link")
        return

    status = str(refresh_rec.get("status") or "").lower()
    if status in {"complete", "succeeded", "approved"}:
        logger.info("approve reconciled via status=%s", status)
        return
    raise ApproveOutcomeUnknownError()


def _extract_redirect_url(*payloads: dict[str, Any] | None) -> str | None:
    for payload in payloads:
        found = _find_redirect_url(payload)
        if found:
            return found
    return None


def _find_redirect_url(node: Any) -> str | None:
    if isinstance(node, dict):
        next_action = node.get("next_action")
        if isinstance(next_action, dict):
            redirect = next_action.get("redirect_to_url")
            if isinstance(redirect, dict):
                url = redirect.get("url")
                if isinstance(url, str) and _is_allowed_payment_link(url):
                    return url.strip()
        for value in node.values():
            found = _find_redirect_url(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_redirect_url(value)
            if found:
                return found
    return None


def _is_allowed_payment_link(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme == "https" and bool(parsed.hostname)


def _build_korea_billing_details(email: str) -> dict[str, Any]:
    profile = generate_korea_profile()
    return {
        "name": profile.name,
        "email": email,
        "address": {
            "line1": profile.address_line1,
            "city": profile.city,
            "state": profile.state,
            "postal_code": profile.postal_code,
            "country": profile.country_code,
        },
    }


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


async def _resolve_approve_error_retries(settings: SettingsRepository) -> int:
    value = await settings.get(_SETTING_APPROVE_ERROR_RETRIES)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return _DEFAULT_APPROVE_ERROR_RETRIES


__all__ = ["run_post_login"]
