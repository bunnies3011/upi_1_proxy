"""Smoke check migration curl_cffi sau khi fix bugs.

Chạy: `python3 test/check_curl_cffi_migration.py` từ backend/.

Verify:
    1. Toàn bộ module production import OK (không còn `httpx`/`respx`).
    2. Facade `http_client.create_async_client` construct được AsyncSession
       với các kwarg trong signature.
    3. `FakeAsyncSession` + `FakeResponse` + `_FakeCookies` mới hoạt động
       đúng contract:
        - `FakeResponse.raise_for_status()` raise `http.HTTPError`.
        - `_FakeCookies.set()` + `.get(name)` với 2 domain khác nhau raise
          `CookieConflict`.
        - `_FakeCookies.jar` là `http.cookiejar.CookieJar` thật, iterate
          trả `Cookie` objects có `.name/.value/.domain/.path`.
        - Route matching với query string.
    4. `_SENTINEL_UA` đồng bộ với `DEFAULT_IMPERSONATE`.
    5. `StripeClient.__init__` nhận `proxy_url` kwarg.

Fail-fast: bất kỳ assertion fail → exit non-zero với message.
"""
from __future__ import annotations

import inspect
import sys
import traceback


def _step(idx: int, name: str) -> None:
    print(f"[{idx:02d}] {name}", flush=True)


def _pass(idx: int, name: str, detail: str = "") -> None:
    print(f"[PASS] {idx:02d} {name} :: {detail}", flush=True)


def _fail(idx: int, name: str, detail: str) -> None:
    print(f"[FAIL] {idx:02d} {name} :: {detail}", flush=True)
    sys.exit(1)


def main() -> int:
    total = 8

    # -----------------------------------------------------------------
    _step(1, "import production modules (no httpx/respx)")
    try:
        import app  # noqa: F401
        from app.core import http_client, proxy_health, session_cache  # noqa: F401
        from app.payments.ideal import (  # noqa: F401
            chatgpt_client,
            chatgpt_login,
            flow,
            sentinel,
            stripe_client,
            stripe_token,
            transaction_client,
        )
        from app.notifiers.telegram import client as telegram_client  # noqa: F401
    except Exception as exc:
        _fail(1, "import production", f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
    _pass(1, "import production", "12 module OK")

    # -----------------------------------------------------------------
    _step(2, "http_client.create_async_client kwarg signature")
    try:
        sig = inspect.signature(http_client.create_async_client)
        expected = {"proxy", "timeout", "headers", "cookies", "allow_redirects", "verify", "impersonate"}
        actual = set(sig.parameters.keys()) - {"extra"}
        missing = expected - actual
        if missing:
            _fail(2, "facade signature", f"missing kwargs: {missing}")
        # Tạo session thật để verify curl_cffi nhận kwarg (không network).
        session = http_client.create_async_client(
            proxy=None,
            timeout=5.0,
            headers={"X-Test": "1"},
            allow_redirects=False,
            impersonate="chrome136",
        )
        assert session.impersonate == "chrome136"
        assert session.timeout == 5.0
        assert session.allow_redirects is False
    except Exception as exc:
        _fail(2, "facade signature", f"{type(exc).__name__}: {exc}")
    _pass(2, "facade signature", "AsyncSession construct OK")

    # -----------------------------------------------------------------
    _step(3, "FakeResponse.raise_for_status raise http.HTTPError")
    try:
        # Path import phải cho phép import từ backend/tests (thêm vào sys.path).
        import os
        backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if backend_dir not in sys.path:
            sys.path.insert(0, backend_dir)
        from tests.support.fake_http import FakeAsyncSession, FakeResponse

        r = FakeResponse(status_code=500)
        try:
            r.raise_for_status()
            _fail(3, "raise_for_status", "did not raise")
        except http_client.HTTPError:
            pass
        except Exception as exc:
            _fail(3, "raise_for_status", f"raised wrong type: {type(exc).__name__}")
    except Exception as exc:
        _fail(3, "raise_for_status", f"{type(exc).__name__}: {exc}")
    _pass(3, "raise_for_status", "raise http.HTTPError OK")

    # -----------------------------------------------------------------
    _step(4, "_FakeCookies.get raise CookieConflict on ambiguity")
    try:
        session = FakeAsyncSession()
        session.cookies.set("oai-did", "value-A", domain="chatgpt.com")
        session.cookies.set("oai-did", "value-B", domain="auth.openai.com")
        # Multi-domain, no domain filter → must raise.
        try:
            session.cookies.get("oai-did")
            _fail(4, "CookieConflict", "did not raise")
        except http_client.CookieConflict:
            pass
        except Exception as exc:
            _fail(4, "CookieConflict", f"wrong type: {type(exc).__name__}")
        # Specify domain → resolve.
        v = session.cookies.get("oai-did", domain="chatgpt.com")
        if v != "value-A":
            _fail(4, "CookieConflict domain filter", f"got {v!r}")
    except Exception as exc:
        _fail(4, "CookieConflict", f"{type(exc).__name__}: {exc}")
    _pass(4, "CookieConflict", "raise + domain filter OK")

    # -----------------------------------------------------------------
    _step(5, "_FakeCookies.jar iterate real Cookie objects")
    try:
        import http.cookiejar as _cj
        session = FakeAsyncSession()
        session.cookies.set("cf_bm", "abc", domain="chatgpt.com")
        session.cookies.set("session_token", "xyz", domain=".openai.com")
        jar = session.cookies.jar
        if not isinstance(jar, _cj.CookieJar):
            _fail(5, "jar type", f"got {type(jar).__name__}")
        cookies_list = list(jar)
        if len(cookies_list) != 2:
            _fail(5, "jar count", f"got {len(cookies_list)}")
        for c in cookies_list:
            if not hasattr(c, "name") or not hasattr(c, "value") or not hasattr(c, "domain"):
                _fail(5, "cookie attrs", f"missing attrs on {c!r}")
    except Exception as exc:
        _fail(5, "jar", f"{type(exc).__name__}: {exc}")
    _pass(5, "jar", "CookieJar real objects OK")

    # -----------------------------------------------------------------
    _step(6, "Route.matches with query string")
    try:
        import asyncio
        async def _test_route():
            s = FakeAsyncSession()
            s.route("GET", "https://x.example/y", FakeResponse(200, text="ok"))
            r = await s.get("https://x.example/y?foo=bar&baz=qux")
            return r.status_code

        code = asyncio.run(_test_route())
        if code != 200:
            _fail(6, "query string match", f"got status {code}")
    except Exception as exc:
        _fail(6, "query string match", f"{type(exc).__name__}: {exc}")
    _pass(6, "query string match", "route matched with ?a=b")

    # -----------------------------------------------------------------
    _step(7, "Sentinel UA sync với DEFAULT_IMPERSONATE")
    try:
        # DEFAULT_IMPERSONATE và _SENTINEL_UA phải khớp cùng 1 major version
        # Chrome. Extract version từ DEFAULT_IMPERSONATE (e.g. "chrome136" →
        # "Chrome/136.") rồi check nó có nằm trong _SENTINEL_UA không.
        default_impersonate = http_client.DEFAULT_IMPERSONATE
        if not default_impersonate.startswith("chrome"):
            _fail(7, "impersonate default", f"expected chromeXXX, got {default_impersonate!r}")
        chrome_version = default_impersonate[len("chrome"):].rstrip("a")  # strip trailing 'a' from e.g. chrome133a
        expected_ua_marker = f"Chrome/{chrome_version}."
        if expected_ua_marker not in sentinel._SENTINEL_UA:
            _fail(
                7,
                "sentinel UA",
                f"expected {expected_ua_marker!r} in _SENTINEL_UA, got: {sentinel._SENTINEL_UA[:120]}",
            )
    except Exception as exc:
        _fail(7, "sentinel UA", f"{type(exc).__name__}: {exc}")
    _pass(7, "sentinel UA", f"{http_client.DEFAULT_IMPERSONATE} đồng bộ")

    # -----------------------------------------------------------------
    _step(8, "StripeClient.__init__ nhận proxy_url kwarg")
    try:
        sig = inspect.signature(stripe_client.StripeClient.__init__)
        if "proxy_url" not in sig.parameters:
            _fail(8, "StripeClient proxy_url", "kwarg missing")
        param = sig.parameters["proxy_url"]
        if param.default is not None:
            _fail(8, "StripeClient proxy_url", f"default should be None, got {param.default!r}")
    except Exception as exc:
        _fail(8, "StripeClient proxy_url", f"{type(exc).__name__}: {exc}")
    _pass(8, "StripeClient proxy_url", "kwarg present with default=None")

    print(f"\n[SMOKE] {total}/{total} checks passed ✓", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
