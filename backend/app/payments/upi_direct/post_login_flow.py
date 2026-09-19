"""Post-login checkout → promo → Stripe init → elements for UPI Direct."""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from pathlib import Path

from app.core import http_client as http
from app.core.payment_flow import Job, JobResult, JobStatus, ProxyLeaseHealth
from app.core.settings_store import SettingsRepository
from app.payments._chatgpt.models import SessionBundle
from app.payments.upi_direct.amount_gate import apply_amount_gates
from app.payments.upi_direct.cancel_helpers import await_cancelable, check_stop
from app.payments.upi_direct.chatgpt_client import (
    ChatgptUpiClient,
    contains_already_paid_hint,
    _PROCESSOR,
)
from app.payments.upi_direct.errors import (
    AlreadyPaidConflictError,
    AlreadyPaidError,
    AlreadyPaidUnverifiedError,
    CheckoutError,
    ConfigError,
    ProcessorEntityMismatchError,
    PromoUpdateError,
)
from app.payments.upi_direct.models import ResolvedProxyPools
from app.payments.upi_direct.proxy_pools import pick_and_materialize, proxy_secret_values
from app.payments.upi_direct.confirm_qr_flow import run_confirm_approve_qr
from app.payments.upi_direct.stripe_client import StripeUpiClient

_DEFAULT_HTTP_TIMEOUT_SECONDS = 30.0
_SETTING_APPROVE_ERROR_RETRIES = "upi_direct.approve_error_retries"
_DEFAULT_APPROVE_ERROR_RETRIES = 1


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

    chatgpt_a = ChatgptUpiClient(
        client_a, logger, proxy_url=pick_a.materialized_url
    )  # type: ignore[arg-type]
    try:
        checkout = await await_cancelable(
            job, chatgpt_a.create_checkout(session, include_promo=require_promo)
        )
    except CheckoutError as exc:
        await _maybe_handle_already_paid(exc, chatgpt_a, session, job, logger)
        raise

    if checkout.processor_entity and checkout.processor_entity != _PROCESSOR:
        raise ProcessorEntityMismatchError(actual=checkout.processor_entity)

    if contains_already_paid_hint(checkout.raw_payload):
        await _raise_already_paid_from_entitlement(chatgpt_a, session, job, logger)

    stop = check_stop(job)
    if stop is not None:
        return JobResult(status=stop.status, proxy_lease_health=health_box[0])

    if require_promo and client_b is not None and pick_b is not None:
        chatgpt_b = ChatgptUpiClient(
            client_b, logger, proxy_url=pick_b.materialized_url
        )  # type: ignore[arg-type]
        try:
            promo_payload = await await_cancelable(
                job,
                chatgpt_b.update_checkout_promo(session, checkout.checkout_session_id),
            )
        except PromoUpdateError as exc:
            await _maybe_handle_already_paid(exc, chatgpt_b, session, job, logger)
            if "transport:" in str(exc):
                logger.warning(
                    "promo update on proxy B failed with transport error (%s); retrying on proxy A",
                    exc,
                )
                promo_payload = await await_cancelable(
                    job,
                    chatgpt_a.update_checkout_promo(
                        session, checkout.checkout_session_id
                    ),
                )
            else:
                raise
        if contains_already_paid_hint(promo_payload):
            await _raise_already_paid_from_entitlement(
                chatgpt_b, session, job, logger
            )

    stop = check_stop(job)
    if stop is not None:
        return JobResult(status=stop.status, proxy_lease_health=health_box[0])

    stripe = StripeUpiClient(client_a, settings, logger)  # type: ignore[arg-type]
    init_state = await await_cancelable(
        job,
        stripe.init(checkout.checkout_session_id, checkout.publishable_key),
    )
    if init_state.init_checksum and init_state.init_checksum not in known_secrets:
        known_secrets.append(init_state.init_checksum)

    amount = apply_amount_gates(init_state.amount, require_promo=require_promo)

    stop = check_stop(job)
    if stop is not None:
        return JobResult(status=stop.status, proxy_lease_health=health_box[0])

    assert amount.amount_minor is not None
    elements = await await_cancelable(
        job,
        stripe.elements_sessions(
            checkout.checkout_session_id,
            checkout.publishable_key,
            amount.amount_minor,
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

    stop = check_stop(job)
    if stop is not None:
        return JobResult(status=stop.status, proxy_lease_health=health_box[0])

    approve_error_retries = await _resolve_approve_error_retries(settings)
    return await run_confirm_approve_qr(
        job=job,
        session=session,
        chatgpt=chatgpt_a,
        stripe=stripe,
        client_a=client_a,
        proxy_a_url=pick_a.materialized_url,
        checkout=checkout,
        init_state=init_state,
        elements=elements,
        amount_minor=amount.amount_minor,
        logger=logger,
        known_secrets=known_secrets,
        health_box=health_box,
        qr_output_dir=qr_output_dir,
        token_config=token_cfg,
        approve_error_retries=approve_error_retries,
    )


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
