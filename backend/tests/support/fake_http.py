"""FakeAsyncSession + FakeResponse cho test — thay thế respx/httpx.MockTransport.

Sau khi migrate `httpx` → `curl_cffi`, các test cũ dùng `respx` (mock ở tầng
`httpx.AsyncTransport` toàn cục) không còn hoạt động vì curl_cffi có
transport hoàn toàn khác. Thay bằng inject `FakeAsyncSession` trực tiếp vào
constructor của client under test (`StripeClient`, `ChatgptClient`,
`TransactionClient`) hoặc monkey-patch `app.core.http_client.
create_async_client` cho integration test.

Design:
- `FakeAsyncSession` triển khai subset API của `curl_cffi.requests.
  AsyncSession` mà production code thực sự gọi: `.get`, `.post`, `.request`,
  `.close`, `__aenter__/__aexit__`. Cookies API mock qua `.cookies` = dict.
- `FakeResponse` triển khai subset của `curl_cffi.requests.Response`:
  `.status_code`, `.text`, `.content`, `.headers` (dict), `.json()`,
  `.cookies` (dict), `.url`, `.history`, `.raise_for_status()`.
- Route matching: exact URL, hoặc URL prefix (dùng `re.match`). Nhiều
  response cho cùng route → consume theo thứ tự (đầu tiên → cuối).

Không cố gắng chính xác wire-level — chỉ đủ cho tests verify high-level
contract (URL đúng, body đúng, response được parse đúng).
"""
from __future__ import annotations

import asyncio
import http.cookiejar as _cookiejar
import json
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional

from app.core import http_client as _http
from app.core.http_client import CookieConflict as _CookieConflict


@dataclass
class Call:
    """Bản ghi 1 request gửi qua FakeAsyncSession."""

    method: str
    url: str
    params: Optional[dict[str, Any]] = None
    data: Any = None
    json_body: Any = None
    headers: Optional[dict[str, str]] = None
    cookies: Optional[dict[str, str]] = None
    files: Any = None
    timeout: Optional[float] = None
    allow_redirects: Optional[bool] = None


class FakeResponse:
    """Thin response object — chỉ có các attribute production code truy cập.

    Cách dùng phổ biến:
        FakeResponse(status_code=200, json_body={"id": "abc"})
        FakeResponse(status_code=302, headers={"location": "..."})
        FakeResponse(status_code=200, text="raw body")
    """

    def __init__(
        self,
        status_code: int = 200,
        *,
        text: str = "",
        content: bytes | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        url: str = "",
        history: list["FakeResponse"] | None = None,
    ) -> None:
        self.status_code = status_code
        # Xác định `_content` + `_text` theo priority: content > json_body > text.
        # `_json_cache` là single-source-of-truth cho `.json()` — set 1 lần
        # cuối cùng để tránh dead-assignment gây confusing.
        if content is not None:
            self._content = content
            self._text = text or content.decode("utf-8", errors="replace")
        elif json_body is not None:
            self._text = json.dumps(json_body)
            self._content = self._text.encode("utf-8")
        else:
            self._text = text
            self._content = text.encode("utf-8")
        # `_json_cache` — chỉ set nếu json_body truyền trực tiếp, hoặc text
        # có shape JSON-like. Lazy parse ở `.json()` để tránh work khi caller
        # không gọi.
        self._json_cache: Any
        if json_body is not None:
            self._json_cache = json_body
        elif self._text.strip().startswith(("{", "[")):
            try:
                self._json_cache = json.loads(self._text)
            except (json.JSONDecodeError, ValueError):
                self._json_cache = None
        else:
            self._json_cache = None
        # Case-insensitive dict for headers.
        self.headers: dict[str, str] = {k: v for k, v in (headers or {}).items()}
        self.cookies: dict[str, str] = dict(cookies or {})
        self.url = url
        self.history: list[FakeResponse] = list(history or [])

    @property
    def text(self) -> str:
        return self._text

    @property
    def content(self) -> bytes:
        return self._content

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def json(self) -> Any:
        if self._json_cache is not None:
            return self._json_cache
        # Fallback: try parse text
        return json.loads(self._text)

    def raise_for_status(self) -> None:
        """Raise `curl_cffi.HTTPError` khi status >= 400 (match real Response).

        Trước đây raise `RuntimeError` — sai type khiến test `except
        http.HTTPError:` không catch được, gây silent-fail contract giữa
        fake và real curl_cffi.
        """
        if self.status_code >= 400:
            raise _http.HTTPError(
                f"FakeResponse HTTP {self.status_code}"
            )


class FakeStreamResponse:
    """Streaming response for `FakeAsyncSession.stream(...)` — mimics the subset
    of `curl_cffi.requests.Response` a streaming NDJSON consumer touches:
    `.status_code`, `.headers`, `.aiter_content(chunk_size=...)`, `.aclose()`,
    `.raise_for_status()`.

    The point of this fake is to let a test drive the exact byte-chunk boundaries
    the production line-buffer must survive, INCLUDING a JSON object split across
    two chunks (`chunks=[b'{"stage":"star', b'ting","percent":5}\\n']`). It does
    NOT reassemble lines — it yields `chunks` verbatim so the consumer's own
    buffering is what is under test.

    Args:
        chunks: Byte chunks to yield in order. A caller splits a JSON line
            mid-object by putting the two halves in adjacent chunks.
        status_code: HTTP status of the streaming response (checked before the
            body is read).
        headers: Response headers (e.g. `content-type: application/x-ndjson`).
        delays: Optional per-chunk sleep (seconds) applied BEFORE yielding chunk
            `i` — used to simulate the vendor's silent `fetching` gap so a
            cancellation test can prove the read is interruptible mid-gap.
        raise_at: Optional `(index, exception)` — raise `exception` instead of
            yielding the chunk at `index`, simulating a mid-stream transport
            failure / truncation.
    """

    def __init__(
        self,
        chunks: list[bytes],
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        delays: list[float] | None = None,
        raise_at: tuple[int, BaseException] | None = None,
    ) -> None:
        self._chunks: list[bytes] = list(chunks)
        self.status_code = status_code
        self.headers: dict[str, str] = {
            "content-type": "application/x-ndjson",
            **(headers or {}),
        }
        self._delays: list[float] = list(delays or [])
        self._raise_at = raise_at
        self.closed = False

    async def aiter_content(
        self, chunk_size: int | None = None, decode_unicode: bool = False
    ) -> AsyncIterator[bytes]:
        """Async-yield the caller-supplied byte chunks in order.

        `chunk_size` is accepted for signature parity with curl_cffi but ignored
        — the fake yields exactly the chunks it was given so tests control the
        boundaries. A non-zero `delays[i]` sleeps before chunk `i` (silent gap);
        `raise_at` raises mid-stream instead of yielding.
        """
        for i, chunk in enumerate(self._chunks):
            if i < len(self._delays) and self._delays[i]:
                await asyncio.sleep(self._delays[i])
            if self._raise_at is not None and self._raise_at[0] == i:
                raise self._raise_at[1]
            yield chunk

    async def aclose(self) -> None:
        self.closed = True

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise _http.HTTPError(f"FakeStreamResponse HTTP {self.status_code}")


#: Streaming response spec — a `FakeStreamResponse`, an exception raised on
#: `stream()` entry, or a lazy factory `(Call) -> FakeStreamResponse | Exception`.
StreamSpec = (
    FakeStreamResponse
    | BaseException
    | Callable[[Call], "FakeStreamResponse | BaseException"]
)


#: Response spec — allow lazy factory (callable) hoặc raise exception.
ResponseSpec = FakeResponse | BaseException | Callable[[Call], "FakeResponse | BaseException"]


@dataclass
class Route:
    """Route entry — pattern match request + list response queue."""

    method: str
    url_pattern: str
    is_regex: bool
    responses: list[ResponseSpec] = field(default_factory=list)
    call_count: int = 0

    def matches(self, method: str, url: str) -> bool:
        if method.upper() != self.method.upper():
            return False
        if self.is_regex:
            return re.match(self.url_pattern, url) is not None
        # Exact match, hoặc match tới phần trước query string (`?...`).
        # KHÔNG dùng `startswith` — dễ tạo xung đột giữa route bare-path
        # và route có suffix `/foo` (bare pattern sẽ vô tình match request
        # có suffix). Nếu test cần khớp lỏng, dùng `regex=True`.
        if url == self.url_pattern:
            return True
        # Match with query string: pattern "https://x/y" matches "https://x/y?a=b"
        if "?" in url and url.split("?", 1)[0] == self.url_pattern:
            return True
        return False


class FakeAsyncSession:
    """Fake `curl_cffi.requests.AsyncSession` cho test.

    Cách 1 — inject trực tiếp vào constructor client under test:
        session = FakeAsyncSession()
        session.route("POST", "https://api.stripe.com/v1/payment_pages",
                      [FakeResponse(200, json_body={...})])
        client = StripeClient(http_client=session, ...)
        await client.init(...)
        assert len(session.calls) == 1

    Cách 2 — monkey-patch factory cho integration test:
        session = FakeAsyncSession()
        monkeypatch.setattr(
            "app.core.http_client.create_async_client",
            lambda **kw: session,
        )
    """

    def __init__(
        self,
        *,
        default_handler: Callable[[Call], "FakeResponse | BaseException"] | None = None,
    ) -> None:
        self.calls: list[Call] = []
        self.routes: list[Route] = []
        #: Streaming routes — kept separate from `routes` so `stream()` and the
        #: unary `request()` paths never resolve each other's specs (additive,
        #: leaves every existing behavior untouched).
        self.stream_routes: list[Route] = []
        #: Cookies dict — production code dùng `session.cookies.set(k, v)` và
        #: iterate `session.cookies.jar`. Ở fake này chỉ mock đủ subset.
        self.cookies = _FakeCookies()
        self.closed = False
        #: Fallback khi không có route nào match. Dùng khi test chỉ quan tâm
        #: capture request, không quan tâm URL cụ thể (VD property test kiểm
        #: `params["sig"]` được truyền nguyên vẹn).
        self.default_handler = default_handler

    # ------------------------------------------------------------------
    # Route registration
    # ------------------------------------------------------------------

    def route(
        self,
        method: str,
        url: str,
        responses: list[ResponseSpec] | ResponseSpec,
        *,
        regex: bool = False,
    ) -> Route:
        """Đăng ký 1 route. `responses` có thể là list (consume theo thứ tự)
        hoặc 1 response (dùng cho mọi call match)."""
        if not isinstance(responses, list):
            responses = [responses]
        r = Route(
            method=method.upper(),
            url_pattern=url,
            is_regex=regex,
            responses=list(responses),
        )
        self.routes.append(r)
        return r

    def stream_route(
        self,
        method: str,
        url: str,
        responses: list[StreamSpec] | StreamSpec,
        *,
        regex: bool = False,
    ) -> Route:
        """Register a streaming route for `stream(...)`. `responses` is a
        `FakeStreamResponse` (or exception / factory), or a list consumed in
        order across successive `stream()` calls (single entry = sticky)."""
        if not isinstance(responses, list):
            responses = [responses]
        r = Route(
            method=method.upper(),
            url_pattern=url,
            is_regex=regex,
            responses=list(responses),
        )
        self.stream_routes.append(r)
        return r

    # ------------------------------------------------------------------
    # Streaming — mimic `curl_cffi.requests.AsyncSession.stream` (async CM)
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def stream(
        self, method: str, url: str, **kwargs: Any
    ) -> AsyncIterator[FakeStreamResponse]:
        """Async-context-manager mirror of `AsyncSession.stream(method, url,
        **kwargs)`: yields a `FakeStreamResponse` whose `aiter_content()` streams
        the route's chunks, then `aclose()`s it on exit — exactly like curl_cffi's
        `@asynccontextmanager` `stream` (which yields then `await rsp.aclose()`).

        The request is recorded in `self.calls` (same shape as `request()`), so a
        test can assert on the captured `json`/`headers` body without the
        production code ever logging it.
        """
        call = Call(
            method=method.upper(),
            url=url,
            params=kwargs.get("params"),
            data=kwargs.get("data"),
            json_body=kwargs.get("json"),
            headers=kwargs.get("headers"),
            cookies=kwargs.get("cookies"),
            files=kwargs.get("files"),
            timeout=kwargs.get("timeout"),
            allow_redirects=kwargs.get("allow_redirects"),
        )
        self.calls.append(call)

        route = self._find_stream_route(method, url)
        if route is None:
            raise AssertionError(
                f"FakeAsyncSession: no stream route matches {method.upper()} {url}. "
                f"Registered stream routes: "
                + ", ".join(f"{r.method} {r.url_pattern}" for r in self.stream_routes)
            )
        spec = self._pop_response(route)
        if callable(spec) and not isinstance(spec, (FakeStreamResponse, BaseException)):
            spec = spec(call)  # type: ignore[misc]
        if isinstance(spec, BaseException):
            raise spec
        if not isinstance(spec, FakeStreamResponse):
            raise TypeError(
                f"Stream route response must be FakeStreamResponse or Exception, "
                f"got {type(spec)}"
            )
        try:
            yield spec
        finally:
            await spec.aclose()

    # ------------------------------------------------------------------
    # Async context manager (dùng `async with fake_session` trong prod code)
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "FakeAsyncSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def close(self) -> None:
        self.closed = True

    # ------------------------------------------------------------------
    # HTTP methods — production code chỉ gọi .get / .post / .request
    # ------------------------------------------------------------------

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> FakeResponse:
        return await self.request("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> FakeResponse:
        return await self.request("DELETE", url, **kwargs)

    async def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        call = Call(
            method=method.upper(),
            url=url,
            params=kwargs.get("params"),
            data=kwargs.get("data"),
            json_body=kwargs.get("json"),
            headers=kwargs.get("headers"),
            cookies=kwargs.get("cookies"),
            files=kwargs.get("files"),
            timeout=kwargs.get("timeout"),
            allow_redirects=kwargs.get("allow_redirects"),
        )
        self.calls.append(call)

        route = self._find_route(method, url)
        if route is None:
            if self.default_handler is not None:
                spec_out = self.default_handler(call)
                if isinstance(spec_out, BaseException):
                    raise spec_out
                if not isinstance(spec_out, FakeResponse):
                    raise TypeError(
                        f"default_handler must return FakeResponse or Exception, "
                        f"got {type(spec_out)}"
                    )
                if not spec_out.url:
                    spec_out.url = url
                return spec_out
            raise AssertionError(
                f"FakeAsyncSession: no route matches {method.upper()} {url}. "
                f"Registered routes: "
                + ", ".join(f"{r.method} {r.url_pattern}" for r in self.routes)
            )

        spec = self._pop_response(route)
        if callable(spec) and not isinstance(spec, (FakeResponse, BaseException)):
            spec = spec(call)  # type: ignore[misc]
        if isinstance(spec, BaseException):
            raise spec
        if not isinstance(spec, FakeResponse):
            raise TypeError(f"Route response must be FakeResponse or Exception, got {type(spec)}")
        # Fill in default url if response didn't set it.
        if not spec.url:
            spec.url = url
        # Mirror curl_cffi: persist any Set-Cookie from the response into the
        # session cookie jar so subsequent requests carry it (e.g. a page GET
        # that hands back `pix_chatgpt_session`). Additive — a response with no
        # `cookies` is a no-op, so existing consumers are unaffected.
        if spec.cookies:
            for _name, _value in spec.cookies.items():
                self.cookies.set(_name, _value)
        return spec

    # ------------------------------------------------------------------
    # Assertion helpers
    # ------------------------------------------------------------------

    def urls_called(self) -> list[str]:
        return [c.url for c in self.calls]

    def call_for(self, method: str, url_substring: str) -> Call:
        """Tìm call đầu tiên khớp method + substring trong URL."""
        for c in self.calls:
            if c.method == method.upper() and url_substring in c.url:
                return c
        raise AssertionError(
            f"FakeAsyncSession: no call {method} matching {url_substring!r}. "
            f"Actual: {[(c.method, c.url) for c in self.calls]}"
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _find_route(self, method: str, url: str) -> Route | None:
        for r in self.routes:
            if r.matches(method, url):
                return r
        return None

    def _find_stream_route(self, method: str, url: str) -> Route | None:
        for r in self.stream_routes:
            if r.matches(method, url):
                return r
        return None

    def _pop_response(self, route: Route) -> ResponseSpec:
        route.call_count += 1
        if not route.responses:
            raise AssertionError(
                f"Route {route.method} {route.url_pattern} exhausted: no more responses."
            )
        if len(route.responses) == 1:
            # Sticky: single response is reused for all subsequent calls.
            return route.responses[0]
        return route.responses.pop(0)


class _FakeCookies:
    """Mock cho `session.cookies` — match contract `curl_cffi.requests.Cookies`.

    Production code dùng 3 API:
        - `.set(name, value, *, domain=..., path=...)`
        - `.get(name, *, domain=...)` — RAISE `CookieConflict` khi trùng
        - iterate `.jar` — trả `http.cookiejar.Cookie` objects (không phải fake)

    Trước đây fake này silent-return giá trị đầu khi trùng name, KHÔNG match
    contract curl_cffi (real raise `CookieConflict`). Nay dùng
    `http.cookiejar.CookieJar` thật để test bắt được đúng edge case
    (`chatgpt_login._first_cookie_value` iterate jar là design đúng chính vì
    lý do này).
    """

    def __init__(self) -> None:
        self._jar = _cookiejar.CookieJar()

    def set(
        self, name: str, value: str, *, domain: str = "", path: str = "/"
    ) -> None:
        """Thêm cookie vào jar. `domain=""` → dùng `""` để iterate với
        production code (production check `cookie.domain.lstrip('.')`).
        """
        cookie = _cookiejar.Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=bool(domain),
            domain_initial_dot=domain.startswith("."),
            path=path,
            path_specified=True,
            secure=False,
            expires=int(time.time()) + 3600,
            discard=False,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
        self._jar.set_cookie(cookie)

    def get(
        self, name: str, *, domain: str = "", path: str = "/"
    ) -> str | None:
        """Trả value hoặc raise `CookieConflict` khi trùng name khác domain.

        Match contract `curl_cffi.requests.Cookies.get()` — nếu caller
        không chỉ định `domain=` và có nhiều cookie cùng `name` khác domain
        → RAISE. Caller phải chỉ định `domain` hoặc iterate `.jar`.
        """
        matches = [c for c in self._jar if c.name == name]
        if not matches:
            return None
        if domain:
            for c in matches:
                if c.domain == domain and c.path == path:
                    return c.value
            return None
        if len(matches) > 1:
            # Cùng semantic với curl_cffi thật.
            raise _CookieConflict(
                f"Multiple cookies exist with name={name!r} — specify domain="
            )
        return matches[0].value

    @property
    def jar(self) -> _cookiejar.CookieJar:
        """Trả `CookieJar` thật — production code iterate qua `.name/.value/
        .domain/.path` và có thể dùng thêm method jar khác nếu cần."""
        return self._jar

    def __iter__(self):
        # Trả unique names — mimic curl_cffi `Cookies.__iter__`.
        seen: set[str] = set()
        for c in self._jar:
            if c.name not in seen:
                seen.add(c.name)
                yield c.name

    def __len__(self) -> int:
        return sum(1 for _ in self._jar)

    def get_dict(self) -> dict[str, str]:
        """Snapshot flat dict — cookie cuối cùng thắng khi trùng name."""
        return {c.name: c.value or "" for c in self._jar}
