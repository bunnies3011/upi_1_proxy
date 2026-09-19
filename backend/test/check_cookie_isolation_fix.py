"""Verify fix HTTP 431 sau migrate httpx→curl_cffi.

Repro bug:
    1. Session cache đầy 29 cookies (session-token chunked .0/.1/.2 + CF/DD).
    2. Revalidate fail → jar bị pollute → CSRF login fallback bị 431.

Fix triệt để (2026-07):
    - `_snapshot_cookies` filter xuống whitelist essential (3-5 cookies).
    - `revalidate` KHÔNG chạm jar khi fail (dùng `Cookie` header thủ công).
    - `_restore_cookies_scoped` set `domain=".chatgpt.com"` explicit.
    - `reset_openai_cookies` dọn cả cookies domain rỗng (defense-in-depth).

Tests (progress log realtime):
    [TC-01] `_snapshot_cookies` filter đúng whitelist essential.
    [TC-02] `_build_cookie_header` sinh chuỗi hợp lệ.
    [TC-03] `_restore_cookies_scoped` set domain=".chatgpt.com" cho cookies whitelist.
    [TC-04] `_restore_cookies_scoped` SKIP cookies không thuộc whitelist.
    [TC-05] `revalidate` FAIL → jar chính vẫn zero-state (không pollution).
    [TC-06] `revalidate` OK → jar được populate cookies scoped.
    [TC-07] `reset_openai_cookies` dọn cả cookies domain rỗng.

Chạy: `python3 test/check_cookie_isolation_fix.py`
"""

from __future__ import annotations

import asyncio
import logging
import sys
from http.cookiejar import Cookie, CookieJar
from pathlib import Path

# Đảm bảo import app.* — chạy từ backend/.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_ROOT))

from app.core import http_client as http  # noqa: E402
from app.payments.ideal.chatgpt_client import (  # noqa: E402
    ChatgptClient,
    _build_cookie_header,
    _restore_cookies_scoped,
)
from app.payments.ideal.chatgpt_login import (  # noqa: E402
    _ESSENTIAL_COOKIE_PREFIXES,
    _is_essential_cookie,
    _snapshot_cookies,
)
from app.payments.ideal.models import SessionBundle  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
_LOG = logging.getLogger("test")


def _make_fake_client_with_cookies(cookies: list[dict]) -> http.AsyncSession:
    """Tạo AsyncSession với cookies preloaded vào jar (mô phỏng post-login)."""
    client = http.create_async_client(timeout=5.0)
    for c in cookies:
        cookie = Cookie(
            version=0,
            name=c["name"],
            value=c["value"],
            port=None,
            port_specified=False,
            domain=c.get("domain", ""),
            domain_specified=bool(c.get("domain")),
            domain_initial_dot=c.get("domain", "").startswith("."),
            path=c.get("path", "/"),
            path_specified=True,
            secure=c.get("secure", False),
            expires=None,
            discard=False,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
        client.cookies.jar.set_cookie(cookie)
    return client


def _log_pass(tc: str, msg: str) -> None:
    print(f"[PASS] {tc} — {msg}", flush=True)


def _log_fail(tc: str, msg: str) -> None:
    print(f"[FAIL] {tc} — {msg}", flush=True)
    sys.exit(1)


# ---------------------------------------------------------------------------
# TC-01: _snapshot_cookies filter đúng whitelist essential
# ---------------------------------------------------------------------------
def tc_01_snapshot_filters_essential() -> None:
    print("[TC-01] snapshot_cookies filter whitelist", flush=True)

    # Bao gồm mix cookies: essential + junk (CF ephemeral, DD analytics)
    bloat_cookies = [
        # ESSENTIAL — phải giữ
        {"name": "__Secure-next-auth.session-token", "value": "v1", "domain": ".chatgpt.com"},
        {"name": "__Secure-next-auth.session-token.0", "value": "chunk0", "domain": ".chatgpt.com"},
        {"name": "__Secure-next-auth.session-token.1", "value": "chunk1", "domain": ".chatgpt.com"},
        {"name": "__Host-next-auth.csrf-token", "value": "csrf1", "domain": "chatgpt.com"},
        {"name": "oai-did", "value": "did1", "domain": ".chatgpt.com"},
        {"name": "cf_clearance", "value": "clr1", "domain": ".chatgpt.com"},
        # JUNK — phải loại
        {"name": "__cf_bm", "value": "junk1", "domain": ".chatgpt.com"},  # ephemeral 30m
        {"name": "_dd_s", "value": "junk2", "domain": ".chatgpt.com"},  # DataDog
        {"name": "_dd_id", "value": "junk3", "domain": ".chatgpt.com"},
        {"name": "intercom-id-lupk8zn7", "value": "junk4", "domain": ".chatgpt.com"},
        {"name": "ajs_anonymous_id", "value": "junk5", "domain": ".chatgpt.com"},
        {"name": "_ga", "value": "junk6", "domain": ".chatgpt.com"},
        # Third-party — phải loại (không thuộc chatgpt/openai)
        {"name": "session-token", "value": "junk7", "domain": ".stripe.com"},
        {"name": "cf_clearance", "value": "keep_openai", "domain": ".openai.com"},  # openai OK
    ]
    client = _make_fake_client_with_cookies(bloat_cookies)

    snapshot = _snapshot_cookies(client)

    expected_names = {
        "__Secure-next-auth.session-token",
        "__Secure-next-auth.session-token.0",
        "__Secure-next-auth.session-token.1",
        "__Host-next-auth.csrf-token",
        "oai-did",
        "cf_clearance",  # đã dedupe theo tên (openai one thắng vì set sau).
    }
    got_names = set(snapshot.keys())

    if got_names != expected_names:
        _log_fail("TC-01", f"expected {expected_names}, got {got_names}")

    if len(snapshot) > 8:
        _log_fail("TC-01", f"snapshot vẫn còn nhiều cookies ({len(snapshot)}) — filter fail")

    _log_pass("TC-01", f"snapshot giữ {len(snapshot)} essential, bỏ 8 junk cookies")


# ---------------------------------------------------------------------------
# TC-02: _build_cookie_header sinh chuỗi hợp lệ
# ---------------------------------------------------------------------------
def tc_02_build_cookie_header() -> None:
    print("[TC-02] build_cookie_header + migration guard", flush=True)
    d = {
        "__Secure-next-auth.session-token": "v1",
        "__Host-next-auth.csrf-token": "csrf1",
        "oai-did": "did1",
    }
    header = _build_cookie_header(d)
    if "__Secure-next-auth.session-token=v1" not in header:
        _log_fail("TC-02", f"thiếu session-token trong header: {header}")
    if "; " not in header:
        _log_fail("TC-02", f"sai delimiter, header: {header}")
    if header.count("=") != 3:
        _log_fail("TC-02", f"expected 3 pairs, got: {header}")

    # Empty dict → rỗng
    if _build_cookie_header({}) != "":
        _log_fail("TC-02", "empty dict phải trả chuỗi rỗng")

    # Empty value → skip
    if _build_cookie_header({"foo": ""}) != "":
        _log_fail("TC-02", "empty value phải bị skip")

    # Migration guard: cache CŨ format bloat 29 cookies → header CHỈ chứa
    # essential, KHÔNG chứa __cf_bm/_dd_s/analytics → không vượt 8KB.
    bloat_cache = {
        # Essential
        "__Secure-next-auth.session-token": "j" * 2000,  # JWT giả 2KB
        "__Secure-next-auth.session-token.0": "c0" * 1000,
        "oai-did": "d1",
        # Junk (cache cũ format)
        "__cf_bm": "x" * 500,
        "_dd_s": "x" * 500,
        "_dd_id": "x" * 500,
        "intercom-id-lupk8zn7": "x" * 500,
        "ajs_anonymous_id": "x" * 100,
        "_ga": "x" * 100,
        "_gid": "x" * 100,
    }
    header_bloat = _build_cookie_header(bloat_cache)
    if "__cf_bm" in header_bloat or "_dd_s" in header_bloat or "_ga" in header_bloat:
        _log_fail(
            "TC-02",
            f"migration guard fail — junk cookies leaked: {header_bloat[:200]}",
        )
    if "__Secure-next-auth.session-token=" not in header_bloat:
        _log_fail("TC-02", "essential cookies missing sau migration guard")

    _log_pass(
        "TC-02",
        f"header hợp lệ, len={len(header)}; migration guard chặn junk "
        f"(bloat cache header len={len(header_bloat)} vs raw ~5KB)",
    )


# ---------------------------------------------------------------------------
# TC-03: _restore_cookies_scoped set domain=".chatgpt.com"
# ---------------------------------------------------------------------------
def tc_03_restore_sets_correct_domain() -> None:
    print("[TC-03] restore_cookies_scoped set domain", flush=True)
    client = http.create_async_client(timeout=5.0)
    cookies = {
        "__Secure-next-auth.session-token": "v1",
        "oai-did": "did1",
    }
    _restore_cookies_scoped(client, cookies)

    # Duyệt jar, verify domain
    for cookie in client.cookies.jar:
        if cookie.domain != ".chatgpt.com":
            _log_fail(
                "TC-03",
                f"cookie {cookie.name!r} có domain={cookie.domain!r}, "
                f"expected '.chatgpt.com'",
            )

    _log_pass("TC-03", "mọi cookies được set với domain='.chatgpt.com'")


# ---------------------------------------------------------------------------
# TC-04: _restore_cookies_scoped SKIP cookies ngoài whitelist
# ---------------------------------------------------------------------------
def tc_04_restore_skips_non_whitelist() -> None:
    print("[TC-04] restore SKIP cookies ngoài whitelist", flush=True)
    client = http.create_async_client(timeout=5.0)
    cookies = {
        "__Secure-next-auth.session-token": "v1",  # WHITELIST — giữ
        "some-random-cookie": "junk",  # NON-WHITELIST — bỏ
        "__cf_bm": "cf-junk",  # NON-WHITELIST — bỏ
    }
    _restore_cookies_scoped(client, cookies)
    names = {c.name for c in client.cookies.jar}
    if "__Secure-next-auth.session-token" not in names:
        _log_fail("TC-04", "whitelist cookie không được restore")
    if "some-random-cookie" in names or "__cf_bm" in names:
        _log_fail("TC-04", f"non-whitelist cookie leak: {names}")

    _log_pass("TC-04", "chỉ cookies whitelist được restore")


# ---------------------------------------------------------------------------
# TC-05: revalidate FAIL → jar zero-state
# ---------------------------------------------------------------------------
def tc_05_revalidate_fail_keeps_jar_clean() -> None:
    print("[TC-05] revalidate FAIL → jar zero-state", flush=True)

    class FakeCache:
        async def get(self, k): return None
        async def save(self, k, v): pass
        async def clear(self, k): pass

    class FakeResponse:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body
            self.headers = {"content-type": "application/json"}
        @property
        def text(self): return self._body
        def json(self):
            import json
            return json.loads(self._body)

    class FakeClient:
        def __init__(self):
            self.cookies = _FakeCookies()
        async def get(self, url, headers=None, **kw):
            # Mô phỏng CF 401 — session hết hạn
            return FakeResponse(401, '{"error":"unauthorized"}')

    class _FakeCookies:
        def __init__(self):
            self._jar = CookieJar()
        @property
        def jar(self):
            return self._jar
        def set(self, name, value, domain=None, path="/"):
            cookie = Cookie(
                version=0, name=name, value=value, port=None,
                port_specified=False, domain=domain or "",
                domain_specified=bool(domain),
                domain_initial_dot=(domain or "").startswith("."),
                path=path, path_specified=True, secure=False,
                expires=None, discard=False, comment=None,
                comment_url=None, rest={}, rfc2109=False,
            )
            self._jar.set_cookie(cookie)

    fake_client = FakeClient()
    client = ChatgptClient(
        http_client=fake_client,  # type: ignore
        session_cache=FakeCache(),  # type: ignore
        logger=logging.getLogger("test.revalidate_fail"),
    )
    session = SessionBundle(
        email="test@example.com",
        access_token="tok",
        cookies={
            "__Secure-next-auth.session-token": "v1",
            "__Secure-next-auth.session-token.0": "chunk0",
        },
    )

    result = asyncio.run(client.revalidate(session))
    if result is not False:
        _log_fail("TC-05", f"expected False, got {result}")

    # Jar phải sạch (không cookies)
    cookies_in_jar = list(fake_client.cookies.jar)
    if len(cookies_in_jar) > 0:
        names = [c.name for c in cookies_in_jar]
        _log_fail(
            "TC-05",
            f"jar bị pollute sau revalidate fail: {names}",
        )

    _log_pass("TC-05", "revalidate fail → jar sạch, không pollution")


# ---------------------------------------------------------------------------
# TC-06: revalidate OK → jar được populate cookies scoped
# ---------------------------------------------------------------------------
def tc_06_revalidate_ok_populates_jar() -> None:
    print("[TC-06] revalidate OK → jar populated scoped", flush=True)

    class FakeCache:
        async def get(self, k): return None
        async def save(self, k, v): pass
        async def clear(self, k): pass

    class FakeResponse:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body
            self.headers = {"content-type": "application/json"}
        @property
        def text(self): return self._body
        def json(self):
            import json
            return json.loads(self._body)

    class FakeClient:
        def __init__(self):
            self.cookies = _FakeCookies()
        async def get(self, url, headers=None, **kw):
            # Session còn valid
            return FakeResponse(
                200,
                '{"user":{"id":"user_abc"},"accessToken":"newtoken"}',
            )

    class _FakeCookies:
        def __init__(self):
            self._jar = CookieJar()
        @property
        def jar(self):
            return self._jar
        def set(self, name, value, domain=None, path="/"):
            cookie = Cookie(
                version=0, name=name, value=value, port=None,
                port_specified=False, domain=domain or "",
                domain_specified=bool(domain),
                domain_initial_dot=(domain or "").startswith("."),
                path=path, path_specified=True, secure=False,
                expires=None, discard=False, comment=None,
                comment_url=None, rest={}, rfc2109=False,
            )
            self._jar.set_cookie(cookie)

    fake_client = FakeClient()
    client = ChatgptClient(
        http_client=fake_client,  # type: ignore
        session_cache=FakeCache(),  # type: ignore
        logger=logging.getLogger("test.revalidate_ok"),
    )
    session = SessionBundle(
        email="test@example.com",
        access_token="tok",
        cookies={
            "__Secure-next-auth.session-token": "v1",
            "oai-did": "did1",
        },
    )
    result = asyncio.run(client.revalidate(session))
    if result is not True:
        _log_fail("TC-06", f"expected True, got {result}")

    cookies_in_jar = list(fake_client.cookies.jar)
    names = {c.name for c in cookies_in_jar}
    if "__Secure-next-auth.session-token" not in names:
        _log_fail("TC-06", f"session-token missing sau revalidate OK: {names}")
    for c in cookies_in_jar:
        if c.domain != ".chatgpt.com":
            _log_fail("TC-06", f"{c.name} có domain={c.domain}, expected .chatgpt.com")

    _log_pass("TC-06", f"revalidate ok → {len(cookies_in_jar)} cookies scoped .chatgpt.com")


# ---------------------------------------------------------------------------
# TC-07: reset_openai_cookies dọn cả cookies domain rỗng
# ---------------------------------------------------------------------------
def tc_07_reset_openai_cookies_dọn_domain_rỗng() -> None:
    print("[TC-07] reset_openai_cookies dọn domain rỗng", flush=True)
    class FakeCache:
        async def get(self, k): return None
        async def save(self, k, v): pass
        async def clear(self, k): pass

    cookies_mix = [
        {"name": "session-token-chatgpt", "value": "v1", "domain": ".chatgpt.com"},
        {"name": "session-token-openai", "value": "v2", "domain": ".openai.com"},
        {"name": "empty-domain-cookie", "value": "v3", "domain": ""},  # nguy hiểm
        {"name": "keep-third-party", "value": "v4", "domain": ".example.com"},
    ]
    fake_client = _make_fake_client_with_cookies(cookies_mix)
    client = ChatgptClient(
        http_client=fake_client,
        session_cache=FakeCache(),  # type: ignore
        logger=logging.getLogger("test.reset"),
    )
    removed = client.reset_openai_cookies()

    if removed != 3:
        _log_fail("TC-07", f"expected 3 removed (chatgpt/openai/empty), got {removed}")

    remaining = {c.name for c in fake_client.cookies.jar}
    if remaining != {"keep-third-party"}:
        _log_fail("TC-07", f"third-party cookie bị xoá nhầm: {remaining}")

    _log_pass("TC-07", "chatgpt+openai+empty domain đều bị dọn, third-party giữ nguyên")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def main() -> None:
    print("=" * 60, flush=True)
    print("Test cookie isolation fix (HTTP 431 curl_cffi)", flush=True)
    print("=" * 60, flush=True)
    tc_01_snapshot_filters_essential()
    tc_02_build_cookie_header()
    tc_03_restore_sets_correct_domain()
    tc_04_restore_skips_non_whitelist()
    tc_05_revalidate_fail_keeps_jar_clean()
    tc_06_revalidate_ok_populates_jar()
    tc_07_reset_openai_cookies_dọn_domain_rỗng()
    print("=" * 60, flush=True)
    print("[ALL PASS] Cookies isolation fix verified", flush=True)


if __name__ == "__main__":
    main()
