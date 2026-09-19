"""Confirm UPI + approve + exact QR extract/publish."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from pathlib import Path
from typing import Any

from app.core import http_client as http
from app.core.payment_flow import Job, JobResult, JobStatus, ProxyLeaseHealth
from app.payments._chatgpt.models import SessionBundle
from app.payments.upi_direct.cancel_helpers import await_cancelable, check_stop
from app.payments.upi_direct.chatgpt_client import (
    ChatgptUpiClient,
    contains_already_paid_hint,
)
from app.payments.upi_direct.errors import (
    AlreadyPaidConflictError,
    AlreadyPaidError,
    AlreadyPaidUnverifiedError,
    ApproveFailedError,
    ApproveOutcomeUnknownError,
    ConfirmFailedError,
    NoQrFoundError,
    QrFetchError,
)
from app.payments.upi_direct.models import (
    CheckoutState,
    ElementsState,
    ExpectedPaymentState,
    QrInstruction,
    StripeInitState,
)
from app.payments.upi_direct.network_safety import (
    _MAX_HTML_BYTES,
    _MAX_PNG_BYTES,
    extract_upi_uri_from_hosted_html,
    fetch_bounded_bytes,
    is_allowed_hosted_instructions_url,
    is_allowed_qr_png_url,
)
from app.payments.upi_direct.profile_generator import generate_india_profile
from app.payments.upi_direct.qr_extract import merge_qr_instructions, parse_upi_next_action
from app.payments.upi_direct.qr_renderer import QrRenderer
from app.payments.upi_direct.stripe_client import StripeUpiClient
from app.payments.upi_direct.stripe_token import StripeTokenConfig

_POST_APPROVE_REFRESH_ATTEMPTS = 8
_POST_APPROVE_REFRESH_DELAY_SECONDS = 1.0
_DEBUG_REDACT_KEY_RE = re.compile(
    r"(authorization|captcha|client_secret|cookie|csrf|ekey|pass|secret|token)",
    re.IGNORECASE,
)
_DEBUG_MAX_STRING = 4000


def _resolve_payment_link(instruction: QrInstruction) -> str | None:
    hosted_url = instruction.hosted_instructions_url
    if hosted_url and is_allowed_hosted_instructions_url(hosted_url):
        return hosted_url
    return None


def _append_qr_candidate(
    *,
    candidates: list[QrInstruction],
    payload: dict[str, Any] | None,
    expected: ExpectedPaymentState,
    phase: str,
    logger: logging.LoggerAdapter,
) -> bool:
    if not payload:
        return False
    try:
        instr = parse_upi_next_action(payload, expected)
    except ValueError as exc:
        logger.warning("%s next_action rejected: %s", phase, exc)
        return False
    if instr is None:
        return False
    candidates.append(instr)
    logger.info("%s next_action ok source=%s", phase, instr.source or "unknown")
    return True


def _payload_shape(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return "none"
    keys = ",".join(sorted(str(key) for key in payload.keys())[:16]) or "-"
    next_action_keys: list[str] = []
    for intent_key in ("setup_intent", "payment_intent"):
        intent = payload.get(intent_key)
        if not isinstance(intent, dict):
            continue
        next_action = intent.get("next_action")
        if isinstance(next_action, dict):
            nested = ",".join(sorted(str(key) for key in next_action.keys())[:8])
            next_action_keys.append(f"{intent_key}.next_action={nested or '-'}")
    next_action = payload.get("next_action")
    if isinstance(next_action, dict):
        nested = ",".join(sorted(str(key) for key in next_action.keys())[:8])
        next_action_keys.append(f"next_action={nested or '-'}")
    paths = _interesting_payload_paths(payload)
    suffixes = []
    if next_action_keys:
        suffixes.extend(next_action_keys)
    if paths:
        suffixes.append("paths=" + "|".join(paths))
    return keys + (" " + " ".join(suffixes) if suffixes else "")


def _stripe_no_qr_failure_detail(payloads: list[dict[str, Any] | None]) -> str | None:
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        setup_intent = payload.get("setup_intent")
        if not isinstance(setup_intent, dict):
            continue
        last_error = setup_intent.get("last_setup_error")
        status = setup_intent.get("status")
        next_action = setup_intent.get("next_action")
        if isinstance(last_error, dict):
            code = _short_detail_value(last_error.get("code"))
            decline_code = _short_detail_value(last_error.get("decline_code"))
            err_type = _short_detail_value(last_error.get("type"))
            parts = ["stripe_setup_failed"]
            if code:
                parts.append(f"code={code}")
            if decline_code:
                parts.append(f"decline={decline_code}")
            if err_type:
                parts.append(f"type={err_type}")
            if isinstance(status, str) and status.strip():
                parts.append(f"status={status.strip()[:80]}")
            return ":".join(parts)
        if next_action is None and isinstance(status, str) and status.strip():
            if status.strip() in {"requires_payment_method", "canceled"}:
                return f"stripe_setup_no_next_action:status={status.strip()[:80]}"
    return None


def _short_detail_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value_s = value.strip()
    if not value_s:
        return None
    return re.sub(r"\s+", "_", value_s)[:120]


def _interesting_payload_paths(payload: dict[str, Any]) -> list[str]:
    interesting = (
        "blob",
        "deferred_intent",
        "hosted_instructions",
        "image_url",
        "next_action",
        "payment_intent",
        "payment_method",
        "qr",
        "setup_intent",
        "upi",
    )
    found: list[str] = []

    def walk(obj: Any, path: str, depth: int) -> None:
        if len(found) >= 24 or depth > 8:
            return
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_s = str(key)
                next_path = f"{path}.{key_s}" if path else key_s
                if any(part in key_s.lower() for part in interesting):
                    found.append(_describe_path(next_path, value))
                    if len(found) >= 24:
                        return
                if key_s == "blob":
                    parsed = _parse_json_blob(value)
                    if parsed is not None:
                        walk(parsed, next_path, depth + 1)
                if isinstance(value, (dict, list)):
                    walk(value, next_path, depth + 1)
                if len(found) >= 24:
                    return
        elif isinstance(obj, list):
            for idx, item in enumerate(obj[:16]):
                walk(item, f"{path}[{idx}]", depth + 1)
                if len(found) >= 24:
                    return

    walk(payload, "", 0)
    return found


def _describe_path(path: str, value: Any) -> str:
    if isinstance(value, dict):
        keys = ",".join(sorted(str(key) for key in value.keys())[:6]) or "-"
        return f"{path}{{{keys}}}"
    if isinstance(value, list):
        return f"{path}[len={len(value)}]"
    if isinstance(value, str):
        parsed = _parse_json_blob(value)
        return f"{path}<str,len={len(value)},json={'yes' if parsed is not None else 'no'}>"
    return f"{path}<{type(value).__name__}>"


def _parse_json_blob(value: Any) -> Any | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 1_000_000 or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _sanitize_debug_payload(value: Any, *, key_hint: str = "") -> Any:
    if _DEBUG_REDACT_KEY_RE.search(key_hint):
        return "***REDACTED***"
    if isinstance(value, dict):
        return {
            str(key): _sanitize_debug_payload(item, key_hint=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _sanitize_debug_payload(item, key_hint=key_hint)
            for item in value[:200]
        ]
    if isinstance(value, tuple):
        return [
            _sanitize_debug_payload(item, key_hint=key_hint)
            for item in value[:200]
        ]
    if isinstance(value, str):
        if _DEBUG_REDACT_KEY_RE.search(value[:160]):
            return "***REDACTED_STRING***"
        if len(value) > _DEBUG_MAX_STRING:
            return value[:_DEBUG_MAX_STRING] + f"...<truncated,len={len(value)}>"
    return value


def _write_json_debug(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_payload = _sanitize_debug_payload(payload)
    path.write_text(
        json.dumps(safe_payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _dump_no_qr_debug(
    *,
    job: Job,
    qr_output_dir: Path,
    logger: logging.LoggerAdapter,
    confirm_attempts: list[Any],
    refresh_pre: dict[str, Any] | None,
    approve_debug: dict[str, Any],
    confirm_post_attempts: list[Any],
    refresh_post: dict[str, Any] | None,
) -> None:
    debug_dir = qr_output_dir.parent / "debug" / "upi_direct" / job.job_id
    try:
        meta = {
            "job_id": job.job_id,
            "confirm_attempts": [
                {
                    "variant": getattr(attempt, "variant", None),
                    "http_status": getattr(attempt, "http_status", None),
                    "ok": getattr(attempt, "ok", None),
                    "error_detail": getattr(attempt, "error_detail", None),
                    "shape": _payload_shape(getattr(attempt, "data", None)),
                }
                for attempt in confirm_attempts
            ],
            "post_confirm_attempts": [
                {
                    "variant": getattr(attempt, "variant", None),
                    "http_status": getattr(attempt, "http_status", None),
                    "ok": getattr(attempt, "ok", None),
                    "error_detail": getattr(attempt, "error_detail", None),
                    "shape": _payload_shape(getattr(attempt, "data", None)),
                }
                for attempt in confirm_post_attempts
            ],
            "pre_refresh_shape": _payload_shape(refresh_pre),
            "post_refresh_shape": _payload_shape(refresh_post),
            "approve": approve_debug,
        }
        _write_json_debug(debug_dir / "meta.json", meta)
        for index, attempt in enumerate(confirm_attempts, start=1):
            _write_json_debug(
                debug_dir
                / f"confirm_{index}_{getattr(attempt, 'variant', 'unknown')}.json",
                getattr(attempt, "data", None),
            )
        _write_json_debug(debug_dir / "pre_refresh.json", refresh_pre)
        _write_json_debug(debug_dir / "approve.json", approve_debug)
        for index, attempt in enumerate(confirm_post_attempts, start=1):
            _write_json_debug(
                debug_dir
                / f"post_confirm_{index}_{getattr(attempt, 'variant', 'unknown')}.json",
                getattr(attempt, "data", None),
            )
        _write_json_debug(debug_dir / "post_refresh.json", refresh_post)
        logger.warning("upi_no_qr_debug_dump dir=%s", debug_dir)
    except Exception as exc:
        logger.warning("upi_no_qr_debug_dump failed: %s", exc)


async def run_confirm_approve_qr(
    *,
    job: Job,
    session: SessionBundle,
    chatgpt: ChatgptUpiClient,
    stripe: StripeUpiClient,
    client_a: http.AsyncSession,
    proxy_a_url: str,
    checkout: CheckoutState,
    init_state: StripeInitState,
    elements: ElementsState,
    amount_minor: int,
    logger: logging.LoggerAdapter,
    known_secrets: list[str],
    health_box: list[ProxyLeaseHealth | None],
    qr_output_dir: Path,
    token_config: StripeTokenConfig | None,
    approve_error_retries: int = 1,
) -> JobResult:
    expected = ExpectedPaymentState(
        checkout_session_id=checkout.checkout_session_id,
        page_id=init_state.page_id,
        elements_session_id=elements.session_id,
        amount_minor=amount_minor,
        currency="inr",
        publishable_key=checkout.publishable_key,
        init_checksum=init_state.init_checksum,
        init_config_id=init_state.config_id,
        elements_config_id=elements.config_id,
    )
    profile = generate_india_profile()
    email = session.email

    stop = check_stop(job)
    if stop is not None:
        return JobResult(status=stop.status, proxy_lease_health=health_box[0])

    candidates: list[QrInstruction] = []
    confirm_attempts = await _confirm_variants_for_qr(
        job=job,
        stripe=stripe,
        expected=expected,
        profile=profile,
        email=email,
        token_config=token_config,
        phase="confirm",
        candidates=candidates,
        logger=logger,
    )
    confirm = confirm_attempts[0] if confirm_attempts else None
    if confirm and contains_already_paid_hint(getattr(confirm, "data", None)):
        await _raise_already_paid(chatgpt, session, job, logger)

    stop = check_stop(job)
    if stop is not None:
        return JobResult(status=stop.status, proxy_lease_health=health_box[0])

    refresh_pre: dict[str, Any] | None = None
    try:
        refresh_pre = await await_cancelable(
            job, stripe.payment_page_refresh(expected)
        )
        _append_qr_candidate(
            candidates=candidates,
            payload=refresh_pre,
            expected=expected,
            phase="pre-approve refresh",
            logger=logger,
        )
    except ConfirmFailedError as exc:
        logger.warning("pre-approve refresh failed: %s", exc)

    stop = check_stop(job)
    if stop is not None:
        return JobResult(status=stop.status, proxy_lease_health=health_box[0])

    approve_debug: dict[str, Any] = {}
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
        approve_debug=approve_debug,
    )
    if approve_stop is not None:
        return approve_stop

    # Post-approve poll for QR
    refresh_post = await _poll_post_approve_refresh_for_qr(
        job=job,
        stripe=stripe,
        expected=expected,
        candidates=candidates,
        logger=logger,
    )

    confirm_post_attempts: list[Any] = []
    if not candidates:
        logger.info("no QR candidates yet, attempting post-approve confirm variants...")
        confirm_post_attempts = await _post_approve_confirm_for_qr(
            job=job,
            stripe=stripe,
            expected=expected,
            profile=profile,
            email=email,
            token_config=token_config,
            candidates=candidates,
            logger=logger,
        )
        if not candidates:
            refresh_post = await _poll_post_approve_refresh_for_qr(
                job=job,
                stripe=stripe,
                expected=expected,
                candidates=candidates,
                logger=logger,
            )

    if not candidates:
        _dump_no_qr_debug(
            job=job,
            qr_output_dir=qr_output_dir,
            logger=logger,
            confirm_attempts=confirm_attempts,
            refresh_pre=refresh_pre,
            approve_debug=approve_debug,
            confirm_post_attempts=confirm_post_attempts,
            refresh_post=refresh_post,
        )
        failure_detail = _stripe_no_qr_failure_detail(
            [refresh_post, refresh_pre]
            + [getattr(a, "data", None) for a in confirm_attempts]
        )
        logger.warning(
            "upi_qr_candidates empty detail=%s confirm_shape=%s pre_refresh_shape=%s post_refresh_shape=%s",
            failure_detail,
            _payload_shape(getattr(confirm, "data", None) if confirm else None),
            _payload_shape(refresh_pre),
            _payload_shape(refresh_post),
        )
        raise NoQrFoundError(detail=failure_detail or "no_next_action")

    try:
        instruction = merge_qr_instructions(candidates)
    except ValueError as exc:
        raise NoQrFoundError(detail=str(exc)) from exc

    renderer = QrRenderer()
    payment_link = _resolve_payment_link(instruction)
    artifact: Path | None = None

    if instruction.image_url_png and is_allowed_qr_png_url(instruction.image_url_png):
        raw = await await_cancelable(
            job,
            fetch_bounded_bytes(
                client_a,
                instruction.image_url_png,
                max_bytes=_MAX_PNG_BYTES,
                proxy=proxy_a_url,
            ),
        )
        # Pillow decode/re-encode + fsync write are blocking CPU/IO — run off
        # the event loop and race cancellation so a stop stays responsive.
        artifact = await await_cancelable(
            job,
            asyncio.to_thread(
                renderer.publish_png_bytes, raw, job.job_id, qr_output_dir
            ),
        )
    elif instruction.upi_uri and instruction.upi_uri.lower().startswith("upi:"):
        artifact = await await_cancelable(
            job,
            asyncio.to_thread(
                renderer.render_upi_uri,
                instruction.upi_uri,
                job.job_id,
                qr_output_dir,
            ),
        )
    elif instruction.hosted_instructions_url and is_allowed_hosted_instructions_url(
        instruction.hosted_instructions_url
    ):
        html = await await_cancelable(
            job,
            fetch_bounded_bytes(
                client_a,
                instruction.hosted_instructions_url,
                max_bytes=_MAX_HTML_BYTES,
                proxy=proxy_a_url,
            ),
        )
        upi_uri = extract_upi_uri_from_hosted_html(html)
        if not upi_uri:
            raise NoQrFoundError(detail="hosted_html_no_upi_uri")
        artifact = await await_cancelable(
            job,
            asyncio.to_thread(
                renderer.render_upi_uri, upi_uri, job.job_id, qr_output_dir
            ),
        )
    else:
        raise NoQrFoundError(detail="no_valid_qr_source")

    logger.info("qr_ready artifact=%s payment_link=%s", artifact, payment_link)
    return JobResult(
        status=JobStatus.QR_READY,
        artifact_path=str(artifact),
        payment_link=payment_link,
        qr_expires_at=instruction.expires_at,
        proxy_lease_health=health_box[0],
    )


async def _raise_already_paid(
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


async def _confirm_variants_for_qr(
    *,
    job: Job,
    stripe: StripeUpiClient,
    expected: ExpectedPaymentState,
    profile: Any,
    email: str,
    token_config: StripeTokenConfig | None,
    phase: str,
    candidates: list[QrInstruction],
    logger: logging.LoggerAdapter,
) -> list[Any]:
    confirm_variants = getattr(stripe, "confirm_upi_variants", None)
    if inspect.iscoroutinefunction(confirm_variants):
        attempts = await await_cancelable(
            job,
            confirm_variants(
                expected, profile, email, token_config=token_config
            ),
        )
    else:
        attempts = [
            await await_cancelable(
                job,
                stripe.confirm_upi(expected, profile, email, token_config=token_config),
            )
        ]
    for attempt in attempts:
        _append_qr_candidate(
            candidates=candidates,
            payload=getattr(attempt, "data", None),
            expected=expected,
            phase=f"{phase}:{getattr(attempt, 'variant', '?')}",
            logger=logger,
        )
    return list(attempts)


async def _post_approve_confirm_for_qr(
    *,
    job: Job,
    stripe: StripeUpiClient,
    expected: ExpectedPaymentState,
    profile: Any,
    email: str,
    token_config: StripeTokenConfig | None,
    candidates: list[QrInstruction],
    logger: logging.LoggerAdapter,
) -> list[Any]:
    try:
        return await _confirm_variants_for_qr(
            job=job,
            stripe=stripe,
            expected=expected,
            profile=profile,
            email=email,
            token_config=token_config,
            phase="post-approve confirm",
            candidates=candidates,
            logger=logger,
        )
    except ConfirmFailedError as exc:
        logger.warning("post-approve confirm failed: %s", exc)
        return []


async def _poll_post_approve_refresh_for_qr(
    *,
    job: Job,
    stripe: StripeUpiClient,
    expected: ExpectedPaymentState,
    candidates: list[QrInstruction],
    logger: logging.LoggerAdapter,
) -> dict[str, Any] | None:
    last_payload: dict[str, Any] | None = None
    for attempt in range(1, _POST_APPROVE_REFRESH_ATTEMPTS + 1):
        stop = check_stop(job)
        if stop is not None:
            return last_payload
        try:
            payload = await await_cancelable(
                job, stripe.payment_page_refresh(expected)
            )
            last_payload = payload
        except ConfirmFailedError as exc:
            logger.warning(
                "post-approve refresh failed attempt=%s/%s: %s",
                attempt,
                _POST_APPROVE_REFRESH_ATTEMPTS,
                exc,
            )
            payload = None

        if _append_qr_candidate(
            candidates=candidates,
            payload=payload,
            expected=expected,
            phase=f"post-approve refresh attempt={attempt}/{_POST_APPROVE_REFRESH_ATTEMPTS}",
            logger=logger,
        ):
            return last_payload

        if attempt < _POST_APPROVE_REFRESH_ATTEMPTS:
            await await_cancelable(
                job,
                asyncio.sleep(_POST_APPROVE_REFRESH_DELAY_SECONDS),
            )
    return last_payload


async def _approve_checkout_with_error_retries(
    *,
    job: Job,
    session: SessionBundle,
    chatgpt: ChatgptUpiClient,
    stripe: StripeUpiClient,
    expected: ExpectedPaymentState,
    checkout_session_id: str,
    candidates: list[QrInstruction],
    logger: logging.LoggerAdapter,
    health_box: list[ProxyLeaseHealth | None],
    approve_error_retries: int,
    approve_debug: dict[str, Any] | None = None,
) -> JobResult | None:
    max_attempts = max(1, int(approve_error_retries))
    for attempt in range(1, max_attempts + 1):
        stop = check_stop(job)
        if stop is not None:
            return JobResult(status=stop.status, proxy_lease_health=health_box[0])

        logger.info("approve attempt %s/%s", attempt, max_attempts)
        approve = await await_cancelable(
            job, chatgpt.approve(session, checkout_session_id)
        )
        if approve_debug is not None:
            approve_debug[f"attempt_{attempt}"] = {
                "http_status": approve.http_status,
                "result": approve.result,
                "ok": approve.ok,
                "ambiguous": approve.ambiguous,
                "shape": _payload_shape(approve.data),
                "data": approve.data,
            }
        if contains_already_paid_hint(approve.data) or contains_already_paid_hint(
            approve.result or ""
        ):
            await _raise_already_paid(chatgpt, session, job, logger)

        if approve.ok:
            _append_qr_candidate(
                candidates=candidates,
                payload=approve.data,
                expected=expected,
                phase="approve",
                logger=logger,
            )
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
    candidates: list[QrInstruction],
    logger: logging.LoggerAdapter,
) -> None:
    try:
        refresh_rec = await await_cancelable(job, stripe.payment_page_refresh(expected))
    except Exception as exc:
        raise ApproveOutcomeUnknownError(
            message=f"approve ambiguous; refresh failed: {exc}"
        ) from exc
    if contains_already_paid_hint(refresh_rec):
        await _raise_already_paid(chatgpt, session, job, logger)
    try:
        instr = parse_upi_next_action(refresh_rec, expected)
        if instr is not None:
            candidates.append(instr)
            logger.info("approve reconciled via refresh next_action")
            return
        status = str(refresh_rec.get("status") or "").lower()
        if status in {"complete", "succeeded", "approved"}:
            logger.info("approve reconciled via status=%s", status)
            return
        raise ApproveOutcomeUnknownError()
    except ValueError as exc:
        raise ApproveOutcomeUnknownError(message=str(exc)) from exc


__all__ = ["run_confirm_approve_qr"]
