"""OaipayClient — captcha config, token-info, long-link-stream SSE.

Payment_Module_Boundary: only `app.core.*` + `app.payments.upi_oaipay.*`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any, Callable

from app.core import http_client as http
from app.core.redaction import redact_message
from app.payments.upi_oaipay.errors import (
    CaptchaConfigError,
    OaipayFlowError,
    RunCancelledError,
    StreamError,
)
from app.payments.upi_oaipay.models import (
    CaptchaConfig,
    LongLinkResult,
    OaipayProgressEvent,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.payment_flow import CancellationToken

_DEFAULT_BASE_URL = "https://oaipay.12001234.xyz"
_ENDPOINT_CAPTCHA_CONFIG = "/api/captcha/config"
_ENDPOINT_TOKEN_INFO = "/api/token-info"
_ENDPOINT_LONG_LINK_STREAM = "/api/long-link-stream"
_CANCEL_POLL_INTERVAL_SECONDS = 0.05
# Bound stream teardown so fail-fast (e.g. already-paid) does not wait for
# OaiPay server-side retries to finish draining the SSE body.
_STREAM_CLOSE_TIMEOUT_SECONDS = 1.0
_BODY_SNIPPET_MAX_CHARS = 300
_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

ProgressCallback = Callable[[OaipayProgressEvent], None]


class OaipayClient:
    """Client for OaiPay long-link-stream (mode 5 UPI)."""

    def __init__(
        self,
        session: "http.AsyncSession",
        *,
        base_url: str = _DEFAULT_BASE_URL,
        logger: logging.Logger,
        known_secrets: list[str] | None = None,
    ) -> None:
        self._client = session
        self._base_url = base_url.rstrip("/")
        self._logger = logger
        self._known_secrets: list[str] = list(known_secrets or [])

    async def get_captcha_config(self) -> CaptchaConfig:
        url = f"{self._base_url}{_ENDPOINT_CAPTCHA_CONFIG}"
        try:
            resp = await self._client.get(
                url, headers={"referer": f"{self._base_url}/"}
            )
        except (http.TimeoutException, http.NetworkError, http.TransportError) as exc:
            detail = self._redact(f"transport_error: {exc.__class__.__name__}: {exc}")
            raise CaptchaConfigError(detail=detail) from exc

        if not (200 <= resp.status_code < 300):
            raise CaptchaConfigError(
                detail=self._redact(
                    f"http_{resp.status_code}: {self._body_snippet(resp)}"
                )
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise CaptchaConfigError(
                detail=self._redact(f"invalid_json: {exc}")
            ) from exc
        if not isinstance(payload, dict):
            raise CaptchaConfigError(detail="payload_not_dict")
        provider = self._as_str(payload.get("provider")) or "local"
        site_key = self._as_str(payload.get("siteKey"))
        return CaptchaConfig(provider=provider, site_key=site_key)

    async def token_info(
        self,
        access_token: str,
        proxy_pools: dict[str, str],
        *,
        turnstile_token: str = "",
        user_agent: str | None = None,
    ) -> dict[str, Any] | None:
        """Best-effort POST /api/token-info — never raises to the caller."""
        url = f"{self._base_url}{_ENDPOINT_TOKEN_INFO}"
        body = {
            "accessToken": access_token,
            "proxy": "",
            "proxyPools": proxy_pools,
        }
        headers = self._browser_headers(turnstile_token, user_agent)
        try:
            resp = await self._client.post(url, json=body, headers=headers)
            if not (200 <= resp.status_code < 300):
                self._logger.warning(
                    "oaipay token_info non-2xx: status=%s", resp.status_code
                )
                return None
            payload = resp.json()
            if not isinstance(payload, dict):
                return None
            return payload
        except Exception as exc:  # noqa: BLE001 - best-effort
            self._logger.warning(
                "oaipay token_info failed: %s",
                self._redact(f"{exc.__class__.__name__}: {exc}"),
            )
            return None

    async def long_link_stream(
        self,
        access_token: str,
        proxy_pools: dict[str, str],
        *,
        turnstile_token: str = "",
        user_agent: str | None = None,
        cancellation_token: "CancellationToken | None" = None,
        on_progress: ProgressCallback | None = None,
    ) -> LongLinkResult:
        """POST /api/long-link-stream — SSE consume → LongLinkResult."""
        url = f"{self._base_url}{_ENDPOINT_LONG_LINK_STREAM}"
        body = self._build_payload(access_token, proxy_pools)
        headers = self._browser_headers(turnstile_token, user_agent)

        self._logger.info("oaipay long-link-stream: opening SSE")
        result_obj: dict[str, Any] | None = None
        try:
            async with self._client.stream(
                "POST",
                url,
                json=body,
                headers=headers,
                timeout=None,
            ) as resp:
                status = getattr(resp, "status_code", 200)
                if status is not None and not (200 <= status < 300):
                    raise StreamError(detail=f"http_{status}")
                try:
                    result_obj = await self._consume_stream(
                        resp, cancellation_token, on_progress
                    )
                except BaseException:
                    # Drop the socket fast so context-manager __aexit__ does
                    # not block on OaiPay still retrying checkout.
                    await self._force_close_response(resp)
                    raise
        except OaipayFlowError:
            raise
        except (http.TimeoutException, http.NetworkError, http.TransportError) as exc:
            detail = self._redact(f"transport_error: {exc.__class__.__name__}: {exc}")
            self._logger.warning("oaipay stream transport error: %s", detail)
            raise StreamError(detail=detail) from exc

        if result_obj is None:
            raise StreamError(detail="stream ended without done")
        return self._parse_run_result(result_obj)

    # ------------------------------------------------------------------
    # SSE stream engine
    # ------------------------------------------------------------------

    async def _consume_stream(
        self,
        resp: Any,
        token: "CancellationToken | None",
        on_progress: ProgressCallback | None,
    ) -> dict[str, Any] | None:
        buffer = b""
        data_lines: list[str] = []
        result_obj: dict[str, Any] | None = None
        abort_exc: BaseException | None = None
        agen = resp.aiter_content()
        cancel_wait: asyncio.Task[None] | None = (
            asyncio.ensure_future(self._wait_for_cancel(token))
            if token is not None
            else None
        )
        read_task: asyncio.Task[bytes] | None = None
        try:
            while True:
                read_task = asyncio.ensure_future(agen.__anext__())
                if cancel_wait is not None:
                    done, _pending = await asyncio.wait(
                        {read_task, cancel_wait},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if cancel_wait in done:
                        await self._cancel_task(read_task)
                        read_task = None
                        raise RunCancelledError()
                else:
                    await asyncio.wait({read_task})

                try:
                    chunk = read_task.result()
                except StopAsyncIteration:
                    read_task = None
                    break
                read_task = None

                if not chunk:
                    continue
                buffer += chunk
                buffer, events, data_lines = self._extract_sse_events(
                    buffer, data_lines
                )
                stop_reading = False
                for obj in events:
                    if obj.get("type") == "done":
                        result_obj = obj.get("result") if "result" in obj else obj
                        if not isinstance(result_obj, dict):
                            result_obj = obj
                        # Terminal event — do not wait for server close / retries.
                        stop_reading = True
                        break
                    if on_progress is None:
                        continue
                    try:
                        on_progress(self._to_progress(obj))
                    except BaseException as exc:
                        # Fail-fast from flow (already paid, etc.): stop reading
                        # immediately so we free the concurrency slot.
                        abort_exc = exc
                        stop_reading = True
                        break
                if stop_reading:
                    break
        finally:
            if cancel_wait is not None:
                cancel_wait.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_wait
            if read_task is not None and not read_task.done():
                await self._cancel_task(read_task)
            await self._safe_aclose(agen)

        if abort_exc is not None:
            raise abort_exc

        # Flush remaining data only when stream ended without abort/done.
        if result_obj is None:
            leftover = buffer.decode("utf-8", errors="replace")
            for line in leftover.split("\n"):
                line = line.rstrip("\r")
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                raw = line[5:].lstrip()
                if raw:
                    data_lines.append(raw)
            if data_lines:
                obj = self._try_parse_event(data_lines)
                data_lines.clear()
                if obj is not None:
                    if obj.get("type") == "done":
                        result_obj = obj.get("result") if "result" in obj else obj
                        if not isinstance(result_obj, dict):
                            result_obj = obj
                    elif on_progress is not None:
                        on_progress(self._to_progress(obj))
        return result_obj

    async def _safe_aclose(self, agen: Any) -> None:
        """Close async iterator without hanging on a slow vendor stream."""
        close = getattr(agen, "aclose", None)
        if close is None:
            return
        try:
            await asyncio.wait_for(close(), timeout=_STREAM_CLOSE_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 — teardown best-effort
            pass

    async def _force_close_response(self, resp: Any) -> None:
        """Best-effort force-close of the HTTP response body/socket."""
        for name in ("aclose", "close"):
            close = getattr(resp, name, None)
            if close is None:
                continue
            try:
                result = close()
                if asyncio.iscoroutine(result):
                    await asyncio.wait_for(
                        result, timeout=_STREAM_CLOSE_TIMEOUT_SECONDS
                    )
            except Exception:  # noqa: BLE001 — teardown best-effort
                pass
            return

    def _extract_sse_events(
        self, buffer: bytes, data_lines: list[str]
    ) -> tuple[bytes, list[dict[str, Any]], list[str]]:
        """Split complete lines; frame multi-line `data:` events on blank lines."""
        events: list[dict[str, Any]] = []
        while b"\n" in buffer:
            raw, buffer = buffer.split(b"\n", 1)
            line = raw.decode("utf-8", errors="replace").rstrip("\r")
            if not line:
                # Blank line → end of logical SSE event.
                if data_lines:
                    obj = self._try_parse_event(data_lines)
                    data_lines = []
                    if obj is not None:
                        events.append(obj)
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:") or line.startswith("id:") or line.startswith(
                "retry:"
            ):
                continue
            if not line.startswith("data:"):
                continue
            payload = line[5:].lstrip()
            if payload:
                data_lines.append(payload)
        return buffer, events, data_lines

    def _try_parse_event(self, data_lines: list[str]) -> dict[str, Any] | None:
        """Join multi-line data payloads and JSON-parse; skip on failure (F-G)."""
        if not data_lines:
            return None
        # Prefer empty join first (chunk-split single JSON across data: lines),
        # then LF join (SSE multi-line / pretty-printed JSON).
        candidates = ["".join(data_lines)]
        if len(data_lines) > 1:
            candidates.append("\n".join(data_lines))
        for raw in candidates:
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(obj, dict):
                return obj
        self._logger.debug(
            "oaipay sse skip non-json data: %s", candidates[0][:200]
        )
        return None

    async def _wait_for_cancel(self, token: "CancellationToken") -> None:
        while not token.is_cancelled():
            await asyncio.sleep(_CANCEL_POLL_INTERVAL_SECONDS)

    @staticmethod
    async def _cancel_task(task: "asyncio.Task[Any]") -> None:
        task.cancel()
        with contextlib.suppress(
            asyncio.CancelledError, StopAsyncIteration, http.TransportError
        ):
            await task

    def _to_progress(self, obj: dict[str, Any]) -> OaipayProgressEvent:
        return OaipayProgressEvent(
            type=self._as_str(obj.get("type")) or "",
            step=self._as_int(obj.get("step"), None),
            total=self._as_int(obj.get("total"), None),
            desc=self._as_str(obj.get("desc")),
            active=self._as_int(obj.get("active"), None),
            max_active=self._as_int(obj.get("maxActive"), None),
            queued=self._as_int(obj.get("queued"), None),
        )

    def _parse_run_result(self, done_obj: dict[str, Any]) -> LongLinkResult:
        result = done_obj if "ok" in done_obj else done_obj.get("result", done_obj)
        if not isinstance(result, dict):
            result = {}
        ok = bool(result.get("ok"))
        provider_error = self._as_str(result.get("provider_error"))
        if provider_error == "":
            provider_error = None
        return LongLinkResult(
            ok=ok,
            provider_redirect_url=self._as_str(result.get("provider_redirect_url")),
            long_url=self._as_str(result.get("long_url")),
            stripe_redirect_url=self._as_str(result.get("stripe_redirect_url")),
            cs_id=self._as_str(result.get("cs_id")),
            upi_intent_id=self._as_str(result.get("upi_intent_id")),
            upi_expires_at=self._as_str(result.get("upi_expires_at")),
            fallback=bool(result.get("fallback")),
            provider_error=provider_error,
        )

    # ------------------------------------------------------------------
    # Request builders
    # ------------------------------------------------------------------

    def _build_payload(
        self, access_token: str, proxy_pools: dict[str, str]
    ) -> dict[str, Any]:
        return {
            "accessToken": access_token,
            "link_type": "upi",
            "proxy": "",
            "proxyPools": {
                "checkout": proxy_pools.get("checkout", ""),
                "promotion": proxy_pools.get("promotion", ""),
            },
            "billing_country": "IN",
            "checkout_ui_mode": "hosted",
            "payment_locale": "en",
            "stripe_publishable_key": "",
            "payment_email": "",
            "device_id": "",
            "user_agent": "",
            "approvalUrl": "",
            "ppPhoneNumber": "",
            "ppOtp": "",
            "captchaToken": "",
            "mode": 5,
            "mode6ProxyMode": "",
        }

    def _browser_headers(
        self, turnstile_token: str = "", user_agent: str | None = None
    ) -> dict[str, str]:
        ua = user_agent or _DEFAULT_UA
        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": self._base_url,
            "referer": f"{self._base_url}/",
            "user-agent": ua,
            "x-captcha-id": "",
            "x-captcha-answer": "",
            "sec-ch-ua-platform": '"macOS"',
        }
        if "Windows" in ua:
            headers["sec-ch-ua-platform"] = '"Windows"'
        if turnstile_token:
            headers["x-turnstile-token"] = turnstile_token
        return headers

    def _redact(self, message: str) -> str:
        return redact_message(message, self._known_secrets)

    def _body_snippet(self, resp: Any) -> str:
        text = getattr(resp, "text", "") or ""
        return text[:_BODY_SNIPPET_MAX_CHARS]

    @staticmethod
    def _as_str(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return str(value)

    @staticmethod
    def _as_int(value: Any, default: int | None) -> int | None:
        if value is None:
            return default
        if isinstance(value, bool):
            return default
        if isinstance(value, int):
            return value
        try:
            return int(value)
        except (TypeError, ValueError):
            return default


__all__ = ["OaipayClient"]
