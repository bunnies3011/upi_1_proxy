"""Verify `ChatgptClient.reset_openai_cookies()` xoá đúng cookies chatgpt/openai.

Repro bug 431:
    1. Set cookies mô phỏng snapshot cache (session-token chunked + cookies phụ).
    2. Gọi reset_openai_cookies() → assert cookies chatgpt/openai bị xoá,
       cookies domain khác (vd DataDog third-party) giữ nguyên.

Không thực hiện HTTP request — chỉ verify contract cookie jar operations.
"""
from __future__ import annotations

import logging
import sys

from app.core.session_cache import AccountSessionCache
from app.core.settings_store import SettingsRepository
from app.payments.ideal.chatgpt_client import ChatgptClient
from curl_cffi.requests import AsyncSession
from pathlib import Path

log = logging.getLogger("test.reset_openai_cookies")
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
)


def make_client() -> AsyncSession:
    """AsyncSession có sẵn cookies mô phỏng snapshot cache."""
    client = AsyncSession(impersonate="chrome136")
    # Session-token JWT chunked (mô phỏng revalidate() đã restore).
    client.cookies.set(
        "__Secure-next-auth.session-token.0", "A" * 4000, domain=".chatgpt.com"
    )
    client.cookies.set(
        "__Secure-next-auth.session-token.1", "B" * 4000, domain=".chatgpt.com"
    )
    client.cookies.set("__cf_bm", "cfval", domain=".chatgpt.com")
    client.cookies.set("oai-did", "device-uuid", domain=".openai.com")
    client.cookies.set("_dd_s", "dd-session", domain="auth.openai.com")
    # Cookie ngoài scope — phải được giữ lại.
    client.cookies.set("thirdparty", "keepme", domain="example.com")
    return client


def make_chatgpt_client(client: AsyncSession) -> ChatgptClient:
    # SettingsRepository yêu cầu DB — dùng None-ish là được vì reset_openai_cookies
    # không đụng session_cache/settings.
    settings = SettingsRepository.__new__(SettingsRepository)
    cache = AccountSessionCache(settings=settings, cache_dir=Path("/tmp/_check_reset"))
    return ChatgptClient(http_client=client, session_cache=cache, logger=log)


def snapshot(client: AsyncSession) -> list[tuple[str, str]]:
    return sorted(
        (ck.name, (ck.domain or "").lstrip("."))
        for ck in client.cookies.jar
    )


def main() -> int:
    print("[1/3] Setup jar với cookies mô phỏng cache stale", flush=True)
    client = make_client()
    before = snapshot(client)
    for name, dom in before:
        print(f"  before: name={name} domain={dom}", flush=True)
    assert len(before) == 6, f"[FAIL] expected 6 cookies pre-reset, got {len(before)}"
    print("[PASS] setup — 6 cookies in jar", flush=True)

    print("[2/3] Gọi reset_openai_cookies()", flush=True)
    cc = make_chatgpt_client(client)
    removed = cc.reset_openai_cookies()
    print(f"  removed={removed}", flush=True)
    assert removed == 5, f"[FAIL] expected removed=5, got {removed}"
    print("[PASS] removed count = 5", flush=True)

    print("[3/3] Verify chỉ giữ third-party cookie", flush=True)
    after = snapshot(client)
    for name, dom in after:
        print(f"  after: name={name} domain={dom}", flush=True)
    assert after == [("thirdparty", "example.com")], f"[FAIL] unexpected remaining: {after}"
    print("[PASS] third-party cookie preserved", flush=True)

    print("[4/4] Idempotent: gọi lại → removed=0", flush=True)
    removed2 = cc.reset_openai_cookies()
    assert removed2 == 0, f"[FAIL] expected idempotent removed=0, got {removed2}"
    print("[PASS] idempotent", flush=True)

    print("\n[OK] all assertions passed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
