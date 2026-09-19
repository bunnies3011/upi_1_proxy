"""Headless GCash MGW QR + settle loop.

This mirrors the dual-proxy relay script's GCash-native leg:
Adyen redirect -> m.gcash.com query -> stateless.consult QR -> query.result
poll -> Adyen return -> ChatGPT custom_payment_method/continue.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

from app.core import http_client as http
from app.payments._chatgpt.login_client import _CHATGPT_BASE_URL, _restore_cookies_scoped
from app.payments._chatgpt.models import SessionBundle
from app.payments.upi_direct.network_safety import atomic_write_png

GCASH_MGW = "https://mgs-gw.paas.mynt.xyz/mgw.htm"
GCASH_APP_ID = "D54528A131559"
GCASH_WORKSPACE = "PROD"
GCASH_TENANT = "MYNTPH"
GCASH_MGW_VERSION = "2.0"
OP_STATELESS_CONSULT = "ap.mobilewallet.gka.authorisation.stateless.consult"
OP_SHORT_LINK = "ap.mobilewallet.short.dynamic.link"
OP_QUERY_RESULT = "ap.mobilewallet.gka.query.result"
GCASH_QR_EXPIRE_SECONDS = 300
_PROCESSOR_ENTITY = "openai_llc"
_DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)


class GcashHeadlessQrError(Exception):
    """Raised when the MGW headless path cannot produce a QR."""


@dataclass(frozen=True)
class GcashHeadlessQrResult:
    artifact_path: Path
    payment_link: str
    gcash_page_url: str
    qr_payload: str
    consult_uuid: str
    expires_at: float


async def capture_gcash_headless_qr(
    *,
    payment_link: str,
    session: SessionBundle,
    job_id: str,
    checkout_session_id: str,
    processor_entity: str,
    output_dir: Path,
    proxy_url: str | None,
    request_timeout: float,
    poll_seconds: int,
    logger: logging.LoggerAdapter,
    plus_signal_callback: Callable[[str, str], Awaitable[Any]] | None = None,
) -> GcashHeadlessQrResult:
    async with http.create_async_client(
        allow_redirects=True,
        timeout=float(request_timeout),
        proxy=proxy_url,
    ) as client:
        _restore_cookies_scoped(client, session.cookies)
        page_url = await _follow_to_gcash_page(client, payment_link)
        page_url_for_api = page_url if "#" in page_url else page_url + "#/"
        query = urlparse(page_url.split("#", 1)[0]).query
        if not query:
            raise GcashHeadlessQrError("gcash_page_missing_query")

        env_info = {
            "tokenId": str(uuid.uuid4()),
            "osType": "macOS",
            "osVersion": "10.15.7",
            "browserType": "Chrome",
            "browserVersion": "151",
            "terminalType": "WEB",
        }
        consult = await _mgw_post(
            client,
            OP_STATELESS_CONSULT,
            {
                "envInfo": env_info,
                "channel": "aggregator",
                "urlParameters": query,
                "originalUrl": page_url_for_api,
                "expireSeconds": GCASH_QR_EXPIRE_SECONDS,
                "bizType": "ACQUIRING",
                "extParams": {},
            },
        )
        result = consult.get("result") if isinstance(consult, dict) else None
        if not isinstance(result, dict) or not result.get("success"):
            raise GcashHeadlessQrError(f"consult_failed:{_short_json(consult)}")
        qr_payload = result.get("qrCode")
        consult_uuid = result.get("uuid")
        if not isinstance(qr_payload, str) or not qr_payload:
            raise GcashHeadlessQrError("consult_missing_qrCode")
        if not isinstance(consult_uuid, str) or not consult_uuid:
            raise GcashHeadlessQrError("consult_missing_uuid")

        short_url = await _try_short_link(client, page_url, page_url_for_api, env_info, consult_uuid)
        png = _render_qr_png(qr_payload)
        artifact = atomic_write_png(output_dir / f"{job_id}.png", png)
        expires_at = time.time() + GCASH_QR_EXPIRE_SECONDS
        logger.info(
            "gcash_headless_qr ok uuid=%s artifact=%s short_url=%s",
            consult_uuid,
            artifact,
            short_url or "-",
        )

    if poll_seconds > 0:
        task = asyncio.create_task(
            _poll_and_settle(
                session=session,
                job_id=job_id,
                checkout_session_id=checkout_session_id,
                processor_entity=processor_entity,
                consult_uuid=consult_uuid,
                env_info=env_info,
                proxy_url=proxy_url,
                request_timeout=request_timeout,
                poll_seconds=poll_seconds,
                logger=logger,
                plus_signal_callback=plus_signal_callback,
            ),
            name="gcash-headless-settle",
        )

        def _done(done: asyncio.Task[None]) -> None:
            try:
                done.result()
            except asyncio.CancelledError:
                logger.info("gcash_headless_poll cancelled uuid=%s", consult_uuid)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "gcash_headless_poll error uuid=%s type=%s msg=%s",
                    consult_uuid,
                    type(exc).__name__,
                    exc,
                )

        task.add_done_callback(_done)

    return GcashHeadlessQrResult(
        artifact_path=artifact,
        payment_link=short_url or page_url_for_api,
        gcash_page_url=page_url_for_api,
        qr_payload=qr_payload,
        consult_uuid=consult_uuid,
        expires_at=expires_at,
    )


async def _follow_to_gcash_page(client: http.AsyncSession, payment_link: str) -> str:
    response = await client.get(
        payment_link,
        headers={
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
            "sec-fetch-site": "cross-site",
            "upgrade-insecure-requests": "1",
            "user-agent": _DESKTOP_UA,
        },
        allow_redirects=True,
    )
    page_url = str(response.url)
    if "gcash.com" not in page_url:
        for prev in getattr(response, "history", []) or []:
            loc = prev.headers.get("Location") or prev.headers.get("location")
            if loc and "gcash.com" in loc:
                page_url = str(loc)
                break
    if "gcash.com" not in page_url:
        raise GcashHeadlessQrError(f"adyen_not_gcash:{response.status_code}:{page_url[:160]}")
    return page_url


async def _mgw_post(
    client: http.AsyncSession,
    operation_type: str,
    request_obj: dict[str, Any],
) -> dict[str, Any]:
    form = {
        "operationType": operation_type,
        "requestData": json.dumps([request_obj], separators=(",", ":")),
        "version": GCASH_MGW_VERSION,
        "workspaceId": GCASH_WORKSPACE,
        "appId": GCASH_APP_ID,
        "tenantId": GCASH_TENANT,
    }
    response = await client.post(
        GCASH_MGW,
        data=form,
        headers={
            "origin": "https://m.gcash.com",
            "referer": "https://m.gcash.com/",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "accept": "*/*",
            f"x-cors-{GCASH_APP_ID.lower()}-prod": "",
            "user-agent": _DESKTOP_UA,
        },
    )
    if not (200 <= response.status_code < 300):
        raise GcashHeadlessQrError(
            f"mgw_http_{response.status_code}:{(response.text or '')[:240]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise GcashHeadlessQrError("mgw_invalid_json") from exc
    if not isinstance(payload, dict):
        raise GcashHeadlessQrError("mgw_payload_not_dict")
    return payload


async def _try_short_link(
    client: http.AsyncSession,
    page_url: str,
    page_url_for_api: str,
    env_info: dict[str, Any],
    consult_uuid: str,
) -> str | None:
    base_no_hash = page_url.split("#", 1)[0]
    original_for_short = _append_query_param(base_no_hash, "uuid", consult_uuid) + "#/"
    try:
        payload = await _mgw_post(
            client,
            OP_SHORT_LINK,
            {
                "envInfo": env_info,
                "originalUrl": original_for_short or page_url_for_api,
                "bizType": "ACQUIRING",
                "expireSeconds": GCASH_QR_EXPIRE_SECONDS,
                "extendInfo": {},
                "extParams": {},
            },
        )
    except Exception:
        return None
    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, dict) and result.get("success"):
        short_url = result.get("shortUrl")
        return short_url if isinstance(short_url, str) and short_url else None
    return None


async def _poll_and_settle(
    *,
    session: SessionBundle,
    job_id: str,
    checkout_session_id: str,
    processor_entity: str,
    consult_uuid: str,
    env_info: dict[str, Any],
    proxy_url: str | None,
    request_timeout: float,
    poll_seconds: int,
    logger: logging.LoggerAdapter,
    plus_signal_callback: Callable[[str, str], Awaitable[Any]] | None,
) -> None:
    deadline = time.time() + max(1, int(poll_seconds))
    async with http.create_async_client(
        allow_redirects=True,
        timeout=float(request_timeout),
        proxy=proxy_url,
    ) as client:
        _restore_cookies_scoped(client, session.cookies)
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            raw = await _mgw_post(
                client,
                OP_QUERY_RESULT,
                {"envInfo": env_info, "uuid": consult_uuid, "extParams": {}},
            )
            result = raw.get("result") if isinstance(raw, dict) else None
            redirect_url = (
                result.get("redirectUrl") or result.get("redirect_url")
                if isinstance(result, dict)
                else None
            )
            if isinstance(redirect_url, str) and redirect_url:
                logger.info(
                    "gcash_headless_poll paid uuid=%s attempt=%s",
                    consult_uuid,
                    attempt,
                )
                verify = await _follow_adyen_return(client, redirect_url)
                redirect_result = verify.get("redirect_result")
                verify_url = verify.get("verify_url")
                if isinstance(redirect_result, str) and redirect_result:
                    cont = await _continue_custom_payment_method(
                        client,
                        session,
                        checkout_session_id,
                        redirect_result,
                        verify_url=verify_url if isinstance(verify_url, str) else None,
                    )
                    logger.info(
                        "gcash_headless_continue ok=%s body=%s",
                        cont.get("ok"),
                        cont.get("matched_body") or "-",
                    )
                    await _hit_payments_success(
                        client,
                        session,
                        checkout_session_id,
                        processor_entity,
                        verify_url=verify_url if isinstance(verify_url, str) else None,
                    )
                    if plus_signal_callback is not None:
                        await plus_signal_callback(
                            job_id,
                            verify_url
                            if isinstance(verify_url, str) and verify_url
                            else f"gcash_headless:{consult_uuid}",
                        )
                return
            logger.info(
                "gcash_headless_poll waiting uuid=%s attempt=%s status=%s",
                consult_uuid,
                attempt,
                raw.get("resultStatus") if isinstance(raw, dict) else "-",
            )
            await asyncio.sleep(min(5.0, max(0.1, deadline - time.time())))
    logger.info("gcash_headless_poll timeout uuid=%s", consult_uuid)


async def _follow_adyen_return(client: http.AsyncSession, redirect_url: str) -> dict[str, Any]:
    response = await client.get(
        redirect_url,
        headers={
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
            "sec-fetch-site": "cross-site",
            "upgrade-insecure-requests": "1",
            "user-agent": _DESKTOP_UA,
        },
        allow_redirects=True,
    )
    verify_url = str(response.url)
    if "chatgpt.com/checkout/verify" not in verify_url:
        for prev in getattr(response, "history", []) or []:
            loc = prev.headers.get("Location") or prev.headers.get("location")
            if loc and "chatgpt.com/checkout/verify" in loc:
                verify_url = str(loc)
                break
    query = dict(parse_qsl(urlparse(verify_url).query, keep_blank_values=True))
    raw_rr = query.get("redirectResult") or query.get("redirect_result")
    return {
        "verify_url": verify_url,
        "redirect_result": unquote(raw_rr) if isinstance(raw_rr, str) else None,
        "query": query,
    }


async def _continue_custom_payment_method(
    client: http.AsyncSession,
    session: SessionBundle,
    checkout_session_id: str,
    redirect_result: str,
    *,
    verify_url: str | None,
) -> dict[str, Any]:
    path = "/backend-api/payments/checkout/custom_payment_method/continue"
    headers = _chatgpt_headers(session, path, referer=verify_url or f"{_CHATGPT_BASE_URL}/checkout/verify")
    candidates = [
        (
            "action_result_nested_redirectResult",
            {
                "checkout_session_id": checkout_session_id,
                "action_result": {"redirectResult": redirect_result},
            },
        ),
        (
            "action_result_nested_redirectResult+entity",
            {
                "checkout_session_id": checkout_session_id,
                "processor_entity": _PROCESSOR_ENTITY,
                "currency": "PHP",
                "action_result": {"redirectResult": redirect_result},
            },
        ),
    ]
    trials: list[dict[str, Any]] = []
    for name, body in candidates:
        response = await client.post(
            f"{_CHATGPT_BASE_URL}{path}",
            json=body,
            headers=headers,
        )
        parsed: Any
        try:
            parsed = response.json()
        except ValueError:
            parsed = (response.text or "")[:240]
        trials.append({"name": name, "status_code": response.status_code})
        if 200 <= response.status_code < 300:
            return {"ok": True, "matched_body": name, "response": parsed, "trials": trials}
    return {"ok": False, "matched_body": None, "trials": trials}


async def _hit_payments_success(
    client: http.AsyncSession,
    session: SessionBundle,
    checkout_session_id: str,
    processor_entity: str,
    *,
    verify_url: str | None,
) -> None:
    await client.get(
        f"{_CHATGPT_BASE_URL}/payments/success.data",
        params={
            "stripe_session_id": checkout_session_id,
            "plan_type": "plus",
            "processor_entity": processor_entity or _PROCESSOR_ENTITY,
            "_routes": "routes/payments.success",
        },
        headers=_chatgpt_headers(
            session,
            "/payments/success.data",
            referer=verify_url or f"{_CHATGPT_BASE_URL}/checkout/verify",
        ),
    )


def _chatgpt_headers(session: SessionBundle, target: str, *, referer: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {session.access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": _CHATGPT_BASE_URL,
        "Referer": referer,
        "OAI-Language": "en-PH",
        "x-openai-target-path": target,
        "x-openai-target-route": target,
        "user-agent": _DESKTOP_UA,
    }


def _render_qr_png(payload: str) -> bytes:
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M

    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=8, border=1)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _append_query_param(url: str, key: str, value: str) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query[key] = value
    return urlunparse(parsed._replace(query=urlencode(query)))


def _short_json(data: Any, limit: int = 300) -> str:
    try:
        text = json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        text = str(data)
    return text if len(text) <= limit else text[:limit] + "..."


__all__ = [
    "GcashHeadlessQrError",
    "GcashHeadlessQrResult",
    "capture_gcash_headless_qr",
]
