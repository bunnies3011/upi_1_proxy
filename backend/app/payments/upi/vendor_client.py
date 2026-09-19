"""CapybaraClient — narrow wrapper over the four `pix.capybara.cv` endpoints.

This is the ONLY place (with `models.py`) that knows the vendor's wire schema:
the endpoint paths, the JSON request/response field names, and the NDJSON stream
shape of `run`. Isolating that knowledge here makes a host/schema/rate-limit
change a single-file swap (the tool's #1 dependency risk).

Endpoints:
    * `POST /api/account/check`   {accessToken}                   → AccountCheck
    * `POST /api/v1/key/verify`   {codes, channel}               → KeyVerify
    * `POST /api/chatgpt/challenge` {code}                        → Challenge
    * `POST /api/chatgpt/run`     {accessToken, code, challenge}  → VendorRunResult
      (an `application/x-ndjson` stream: progress objects then a final
       `{"stage":"done","result":{…}}`)

Streaming: `run()` opens `session.stream("POST", …)` (curl_cffi's async CM) and
reads `aiter_content()` chunks into a line buffer, so a JSON object split across
two network chunks is reassembled before parsing. Cancellation is
interrupt-driven — each chunk read is raced against the cancellation token, so a
`.cancel()` during the vendor's long silent `fetching` hold is honoured within
tens of milliseconds rather than only between lines.

Redaction (Security): the `access_token` VALUE is passed via `known_secrets`;
every log line and error message is run through `redact_message`, and the raw
request body (which carries the token) is NEVER logged.

Payment_Module_Boundary: imports only `app.core.*` and `app.payments.upi.*` —
NOTHING from `app.payments.ideal`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any, Callable, Iterable

from app.core import http_client as http
from app.core.redaction import redact_message
from app.payments.upi.errors import (
    ChallengeError,
    EligibilityError,
    LicenseError,
    UpiFlowError,
    VendorRunCancelled,
    VendorStreamError,
)
from app.payments.upi.models import (
    AccountCheck,
    Challenge,
    KeyVerify,
    KeyVerifyItem,
    ProgressEvent,
    VendorRunResult,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.payment_flow import CancellationToken

# ---------------------------------------------------------------------------
# Constants — vendor endpoints + client tunables
# ---------------------------------------------------------------------------

_DEFAULT_BASE_URL = "https://pix.capybara.cv"
_DEFAULT_REFERER = "/dev/upi"

_ENDPOINT_ACCOUNT_CHECK = "/api/account/check"
_ENDPOINT_KEY_VERIFY = "/api/v1/key/verify"
_ENDPOINT_CHALLENGE = "/api/chatgpt/challenge"
_ENDPOINT_RUN = "/api/chatgpt/run"

#: How often the cancel-watcher re-checks `is_cancelled()` while racing a chunk
#: read. Small enough that a cancel during a silent stream gap is honoured almost
#: immediately; large enough to avoid a busy-spin.
_CANCEL_POLL_INTERVAL_SECONDS = 0.05

#: Max chars of a response body kept in an error/log message (avoid spam/leak).
_BODY_SNIPPET_MAX_CHARS = 300

ProgressCallback = Callable[[ProgressEvent], None]


class CapybaraClient:
    """Client for the `pix.capybara.cv` UPI vendor API."""

    def __init__(
        self,
        session: "http.AsyncSession",
        *,
        base_url: str = _DEFAULT_BASE_URL,
        referer: str = _DEFAULT_REFERER,
        logger: logging.Logger,
        known_secrets: list[str] | None = None,
    ) -> None:
        self._client = session
        self._base_url = base_url.rstrip("/")
        self._referer = referer
        self._logger = logger
        #: The `access_token` VALUE (and any other job secret) is appended here
        #: so every free-form log/error string can be value-redacted.
        self._known_secrets: list[str] = list(known_secrets or [])
        #: Page-session prime state. The vendor's JSON/stream endpoints require a
        #: `pix_chatgpt_session` cookie that is only issued by GETting the page
        #: first; we do that GET once per client (lazily) and let curl_cffi's
        #: cookie jar carry the cookie to every later request. Guarded by a lock
        #: so concurrent first-calls prime exactly once.
        self._primed: bool = False
        self._prime_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Page-session prime — lazy, idempotent, best-effort
    # ------------------------------------------------------------------

    async def _ensure_page_session(self) -> None:
        """GET the vendor page ONCE per client so curl_cffi's cookie jar captures
        the `pix_chatgpt_session` cookie the JSON/stream endpoints require.

        A browser that already has `/dev/upi` open carries that cookie implicitly;
        this client never opened the page, so `key/verify` et al. reject the call
        with `page_session_required`. Priming the session here makes the cookie
        travel automatically on every later request.

        Idempotent: the `self._primed` flag is double-checked under a lock, so
        concurrent first-calls prime exactly once and the rest are no-ops.

        Best-effort: a prime that raises (transport error) or returns non-2xx is
        logged and swallowed — never re-raised — so the subsequent real endpoint
        call remains the authoritative source of truth (and callers/fakes that do
        not serve the page route keep working). A failed prime is NOT latched, so
        a later call may retry it. The GET carries no secrets (a page fetch only),
        so the URL is not a redaction concern; the exception text is still redacted
        defensively.
        """
        if self._primed:
            return
        async with self._prime_lock:
            if self._primed:
                return
            url = f"{self._base_url}{self._referer}"
            try:
                resp = await self._client.get(url, headers=self._page_headers())
            except Exception as exc:  # noqa: BLE001 - best-effort prime; see docstring
                # ANY failure here (transport error, or an unrouted fake) must not
                # change the outcome of the real endpoint call that follows.
                self._logger.warning(
                    "upi session prime failed: %s",
                    self._redact(f"{exc.__class__.__name__}: {exc}"),
                )
                return
            status = getattr(resp, "status_code", 200)
            if status is not None and not (200 <= status < 300):
                self._logger.warning("upi session prime non-2xx: status=%s", status)
                return
            self._primed = True
            self._logger.info("upi session primed: page GET ok")

    # ------------------------------------------------------------------
    # JSON endpoints (1-3)
    # ------------------------------------------------------------------

    async def account_check(self, access_token: str) -> AccountCheck:
        """`POST /api/account/check` — eligibility gate for the vendor flow."""
        await self._ensure_page_session()
        payload = await self._post_json(
            f"{self._base_url}{_ENDPOINT_ACCOUNT_CHECK}",
            {"accessToken": access_token},
            lambda detail: EligibilityError(detail=detail),
            name="account_check",
        )
        plan_type = payload.get("plan_type")
        result = AccountCheck(
            eligible=bool(payload.get("eligible")),
            is_paid=bool(payload.get("is_paid")),
            plan_type=plan_type if isinstance(plan_type, str) else None,
        )
        self._logger.info(
            "upi account_check: eligible=%s is_paid=%s plan_type=%s",
            result.eligible,
            result.is_paid,
            result.plan_type,
        )
        return result

    async def key_verify(
        self, codes: Iterable[str], channel: str = "upi"
    ) -> KeyVerify:
        """`POST /api/v1/key/verify` — remaining credit per PK code."""
        await self._ensure_page_session()
        payload = await self._post_json(
            f"{self._base_url}{_ENDPOINT_KEY_VERIFY}",
            {"codes": list(codes), "channel": channel},
            lambda detail: LicenseError(detail=detail),
            name="key_verify",
        )
        items: list[KeyVerifyItem] = []
        raw_items = payload.get("items")
        if isinstance(raw_items, list):
            for entry in raw_items:
                if not isinstance(entry, dict):
                    continue
                items.append(
                    KeyVerifyItem(
                        code=self._as_str(entry.get("code")) or "",
                        remaining=self._as_int(entry.get("remaining"), 0),
                        total=self._as_int(entry.get("total"), 0),
                        valid=bool(entry.get("valid")),
                    )
                )
        result = KeyVerify(
            channel=self._as_str(payload.get("channel")) or channel,
            items=tuple(items),
            total=self._as_int(payload.get("total"), 0),
            valid=bool(payload.get("valid")),
        )
        self._logger.info(
            "upi key_verify: channel=%s items=%d total=%s valid=%s",
            result.channel,
            len(result.items),
            result.total,
            result.valid,
        )
        return result

    async def challenge(self, code: str) -> Challenge:
        """`POST /api/chatgpt/challenge` — anti-abuse token echoed into `run`."""
        await self._ensure_page_session()
        payload = await self._post_json(
            f"{self._base_url}{_ENDPOINT_CHALLENGE}",
            {"code": code},
            lambda detail: ChallengeError(detail=detail),
            name="challenge",
        )
        nonce = payload.get("nonce")
        mac = payload.get("mac")
        if not (isinstance(nonce, str) and nonce) or not (isinstance(mac, str) and mac):
            raise ChallengeError(detail="challenge response missing 'nonce'/'mac'")
        result = Challenge(
            nonce=nonce,
            expires=self._as_int(payload.get("expires"), 0),
            mac=mac,
        )
        self._logger.info("upi challenge: ok expires=%s", result.expires)
        return result

    # ------------------------------------------------------------------
    # Streaming endpoint (4) — run
    # ------------------------------------------------------------------

    async def run(
        self,
        access_token: str,
        code: str,
        challenge: Challenge,
        *,
        cancellation_token: "CancellationToken | None" = None,
        on_progress: ProgressCallback | None = None,
    ) -> VendorRunResult:
        """`POST /api/chatgpt/run` — stream progress + final QR result.

        Opens the NDJSON stream, buffers lines across chunks, invokes
        `on_progress` once per progress line, and returns the `VendorRunResult`
        parsed from the final `done` event. A business failure (`result.ok=false`)
        is returned as `VendorRunResult(ok=False)` — NOT raised. A truncated /
        malformed / transport-broken stream raises `VendorStreamError`. A set
        cancellation token raises `VendorRunCancelled`.

        The token is sent inside the request body and is NEVER logged.
        """
        await self._ensure_page_session()
        url = f"{self._base_url}{_ENDPOINT_RUN}"
        body: dict[str, Any] = {
            "accessToken": access_token,
            "code": code,
            "challenge": {
                "nonce": challenge.nonce,
                "expires": challenge.expires,
                "mac": challenge.mac,
            },
        }

        self._logger.info("upi vendor run: opening NDJSON stream")
        result_obj: dict[str, Any] | None = None
        try:
            # timeout=None: disable curl_cffi stream low-speed abort (session
            # default 30s would kill the silent "polling for UPI QR" gap).
            # Wall-clock cap is asyncio.wait_for(run_timeout) in the handler.
            async with self._client.stream(
                "POST",
                url,
                json=body,
                headers=self._stream_headers(),
                timeout=None,
            ) as resp:
                status = getattr(resp, "status_code", 200)
                if status is not None and not (200 <= status < 300):
                    self._logger.warning("upi vendor run non-2xx: status=%s", status)
                    raise VendorStreamError(detail=f"http_{status}")
                result_obj = await self._consume_stream(
                    resp, cancellation_token, on_progress
                )
        except UpiFlowError:
            # VendorStreamError / VendorRunCancelled / VendorRunError already
            # carry a stable code + redacted message — re-raise unchanged.
            raise
        except (http.TimeoutException, http.NetworkError, http.TransportError) as exc:
            detail = self._redact(f"transport_error: {exc.__class__.__name__}: {exc}")
            self._logger.warning("upi vendor run transport error: %s", detail)
            raise VendorStreamError(detail=detail) from exc

        if result_obj is None:
            raise VendorStreamError(
                detail="stream ended without a final 'done' result event"
            )
        return self._parse_run_result(result_obj)

    # ------------------------------------------------------------------
    # Stream internals
    # ------------------------------------------------------------------

    async def _consume_stream(
        self,
        resp: Any,
        token: "CancellationToken | None",
        on_progress: ProgressCallback | None,
    ) -> dict[str, Any] | None:
        """Read `aiter_content()` into a line buffer, racing each read against
        the cancellation token; return the final `done` event dict (or None if
        the stream ended without one).
        """
        buffer = b""
        result_obj: dict[str, Any] | None = None
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
                        # Cancellation wins the race — interrupt the pending read
                        # (even mid silent-gap) and stop.
                        await self._cancel_task(read_task)
                        read_task = None
                        raise VendorRunCancelled()
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
                buffer, events = self._extract_lines(buffer)
                for obj in events:
                    if obj.get("stage") == "done":
                        result_obj = obj
                    elif on_progress is not None:
                        on_progress(self._to_progress(obj))
        finally:
            if cancel_wait is not None:
                cancel_wait.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_wait
            if read_task is not None and not read_task.done():
                await self._cancel_task(read_task)
            with contextlib.suppress(Exception):
                await agen.aclose()

        # A final line with no trailing newline stays in the buffer. If it fails
        # to parse, the stream was cut mid-object → truncation error.
        leftover = buffer.strip()
        if leftover:
            try:
                obj = json.loads(leftover)
            except (ValueError, json.JSONDecodeError) as exc:
                raise VendorStreamError(
                    detail=self._redact(f"truncated_final_line: {exc}")
                ) from exc
            if isinstance(obj, dict):
                if obj.get("stage") == "done":
                    result_obj = obj
                elif on_progress is not None:
                    on_progress(self._to_progress(obj))
        return result_obj

    async def _wait_for_cancel(self, token: "CancellationToken") -> None:
        """Resolve as soon as the token is cancelled — polled at a small interval
        so the loop wakes to honour a cancel even during a silent stream gap.
        The token exposes only `is_cancelled()` (no awaitable event)."""
        while not token.is_cancelled():
            await asyncio.sleep(_CANCEL_POLL_INTERVAL_SECONDS)

    @staticmethod
    async def _cancel_task(task: "asyncio.Task[Any]") -> None:
        task.cancel()
        with contextlib.suppress(
            asyncio.CancelledError, StopAsyncIteration, http.TransportError
        ):
            await task

    def _extract_lines(
        self, buffer: bytes
    ) -> tuple[bytes, list[dict[str, Any]]]:
        """Split complete `\\n`-terminated lines out of `buffer`, JSON-parsing
        each; return `(remaining_partial_buffer, parsed_objects)`. A complete
        line that fails to parse is a malformed stream → `VendorStreamError`.
        """
        events: list[dict[str, Any]] = []
        while b"\n" in buffer:
            raw, buffer = buffer.split(b"\n", 1)
            text = raw.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except (ValueError, json.JSONDecodeError) as exc:
                raise VendorStreamError(
                    detail=self._redact(f"malformed_ndjson_line: {exc}")
                ) from exc
            if isinstance(obj, dict):
                events.append(obj)
        return buffer, events

    def _to_progress(self, obj: dict[str, Any]) -> ProgressEvent:
        return ProgressEvent(
            stage=self._as_str(obj.get("stage")) or "",
            percent=self._as_int(obj.get("percent"), 0),
            label=self._as_str(obj.get("label")),
            attempts=self._as_int(obj.get("attempts"), None),
        )

    def _parse_run_result(self, done_obj: dict[str, Any]) -> VendorRunResult:
        result = done_obj.get("result")
        if not isinstance(result, dict):
            raise VendorStreamError(
                detail="final 'done' event is missing a 'result' object"
            )
        ok = bool(result.get("ok"))
        amount = result.get("amount")
        return VendorRunResult(
            ok=ok,
            code=self._as_str(result.get("code")),
            qr_image_png=self._as_str(result.get("qr_image_png")) if ok else None,
            qr_image_svg=self._as_str(result.get("qr_image_svg")) if ok else None,
            hosted_url=self._as_str(result.get("hosted_url")) if ok else None,
            amount=amount if isinstance(amount, (int, float)) and not isinstance(amount, bool) else None,
            currency=self._as_str(result.get("currency")) if ok else None,
            intent_type=self._as_str(result.get("intent_type")) if ok else None,
            expires_at=self._as_str(result.get("expires_at")) if ok else None,
            elapsed_ms=self._as_int(result.get("elapsed_ms"), None),
            attempts=self._as_int(result.get("attempts"), None),
        )

    # ------------------------------------------------------------------
    # Shared HTTP + parsing helpers
    # ------------------------------------------------------------------

    async def _post_json(
        self,
        url: str,
        body: dict[str, Any],
        make_error: Callable[[str], UpiFlowError],
        *,
        name: str,
    ) -> dict[str, Any]:
        """POST `body` as JSON and return the parsed dict, raising `make_error`
        (redacted detail) on transport error, non-2xx, invalid JSON, or a
        non-dict payload. The request body is never logged."""
        try:
            resp = await self._client.post(
                url, json=body, headers=self._json_headers()
            )
        except (http.TimeoutException, http.NetworkError, http.TransportError) as exc:
            detail = self._redact(f"transport_error: {exc.__class__.__name__}: {exc}")
            self._logger.warning("upi %s transport error: %s", name, detail)
            raise make_error(detail) from exc

        if not (200 <= resp.status_code < 300):
            detail = self._redact(f"http_{resp.status_code}: {self._body_snippet(resp)}")
            self._logger.warning("upi %s non-2xx: status=%s", name, resp.status_code)
            raise make_error(detail)

        try:
            payload = resp.json()
        except ValueError as exc:
            self._logger.warning("upi %s invalid JSON", name)
            raise make_error(self._redact(f"invalid_json: {exc}")) from exc

        if not isinstance(payload, dict):
            raise make_error(
                self._redact(f"payload_not_dict: {type(payload).__name__}")
            )
        return payload

    def _json_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": self._base_url,
            "Referer": f"{self._base_url}{self._referer}",
        }

    def _stream_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/x-ndjson",
            "Origin": self._base_url,
            "Referer": f"{self._base_url}{self._referer}",
        }

    def _page_headers(self) -> dict[str, str]:
        """Browser-ish headers for the session-prime page GET (no secrets)."""
        return {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Origin": self._base_url,
            "Referer": f"{self._base_url}{self._referer}",
        }

    def _body_snippet(self, resp: Any) -> str:
        try:
            text = resp.text
        except (UnicodeDecodeError, http.DecodingError):
            return ""
        if not text:
            return ""
        return text[:_BODY_SNIPPET_MAX_CHARS]

    def _redact(self, text: str) -> str:
        return redact_message(text, self._known_secrets)

    @staticmethod
    def _as_str(value: Any) -> str | None:
        return value if isinstance(value, str) else None

    @staticmethod
    def _as_int(value: Any, default: int | None) -> int | None:
        if isinstance(value, bool):
            return default
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        return default


__all__ = ["CapybaraClient", "ProgressCallback"]
