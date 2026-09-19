"""Verify conservative cache eviction policy (2026-07 fix).

Policy: cache CHỈ bị clear khi login fail vì lý do session/credential
thực sự invalid. Với transient errors (Cloudflare 403, network timeout,
rate-limit) → GIỮ cache để retry sau còn dùng được → hạn chế login lại.

Test matrix (progress log realtime):
    [TC-01] revalidate OK → dùng cache (không thay đổi).
    [TC-02] revalidate fail + login fail(invalid_credential) → CLEAR cache.
    [TC-03] revalidate fail + login fail(account_locked)    → CLEAR cache.
    [TC-04] revalidate fail + login fail(network_error)     → KEEP cache.
    [TC-05] revalidate fail + login fail(mfa_required)      → KEEP cache.
    [TC-06] revalidate fail + login OK                       → cache mới ghi đè.
    [TC-07] payload corrupt shape (không phải revalidate)   → CLEAR cache ngay.

Chạy: `python3 test/check_conservative_cache_eviction.py`
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_ROOT))

from app.core.session_cache import AccountSessionCache  # noqa: E402
from app.payments.ideal.errors import LoginError  # noqa: E402
from app.payments.ideal.flow import IdealFlowHandler  # noqa: E402
from app.payments.ideal.models import (  # noqa: E402
    IdealParsedAccount,
    SessionBundle,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
_LOG = logging.getLogger("test.eviction")


class _FakeSettingsRepo:
    pass


def _log_pass(tc: str, msg: str) -> None:
    print(f"[PASS] {tc} — {msg}", flush=True)


def _log_fail(tc: str, msg: str) -> None:
    print(f"[FAIL] {tc} — {msg}", flush=True)
    sys.exit(1)


def _account_key(email: str) -> str:
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:32]


def _make_cache(cache_dir: Path) -> AccountSessionCache:
    cache = AccountSessionCache(_FakeSettingsRepo(), cache_dir)  # type: ignore
    cache.apply_settings(
        {"session_cache.enabled": True, "session_cache.ttl_hours": 24}
    )
    return cache


def _make_session(email: str) -> SessionBundle:
    return SessionBundle(
        email=email,
        access_token="tok_" + "x" * 30,
        cookies={"__Secure-next-auth.session-token": "v1"},
    )


def _make_parsed(email: str) -> IdealParsedAccount:
    return IdealParsedAccount(
        raw_line=f"{email}|secret123|JBSWY3DPEHPK3PXP",
        email=email,
        password="secret123",
        totp_secret="JBSWY3DPEHPK3PXP",
        access_token=None,
    )


async def _run_resolve(
    handler: IdealFlowHandler,
    chatgpt_client_mock: MagicMock,
    parsed: IdealParsedAccount,
    account_key: str,
) -> SessionBundle | None:
    """Wrapper để gọi _resolve_session (private nhưng test path)."""
    try:
        return await handler._resolve_session(  # noqa: SLF001
            chatgpt_client=chatgpt_client_mock,
            parsed=parsed,
            account_key=account_key,
            logger=_LOG,
        )
    except LoginError:
        return None


def _make_handler(cache: AccountSessionCache) -> IdealFlowHandler:
    """Chỉ set field cần thiết cho `_resolve_session` — không init full."""
    handler = IdealFlowHandler.__new__(IdealFlowHandler)
    handler._session_cache = cache  # type: ignore[attr-defined]
    return handler


# ---------------------------------------------------------------------------
# TC-01: revalidate OK → cache reuse
# ---------------------------------------------------------------------------
async def tc_01_revalidate_ok_reuses_cache(cache_dir: Path) -> None:
    print("[TC-01] revalidate OK → cache reuse", flush=True)
    cache = _make_cache(cache_dir)
    email = "tc01@example.com"
    key = _account_key(email)
    session = _make_session(email)
    await cache.save(key, asdict(session))

    chatgpt_mock = MagicMock()
    chatgpt_mock.revalidate = AsyncMock(return_value=True)
    chatgpt_mock.login = AsyncMock()
    chatgpt_mock.reset_openai_cookies = MagicMock(return_value=0)

    handler = _make_handler(cache)
    result = await _run_resolve(handler, chatgpt_mock, _make_parsed(email), key)

    if result is None:
        _log_fail("TC-01", "resolve trả None dù revalidate OK")
    chatgpt_mock.login.assert_not_called()

    # Cache phải còn nguyên
    cached = await cache.get(key)
    if cached is None:
        _log_fail("TC-01", "cache bị xoá dù revalidate OK")
    _log_pass("TC-01", "revalidate OK → dùng cache, không login lại")


# ---------------------------------------------------------------------------
# TC-02: revalidate fail + login fail(invalid_credential) → CLEAR cache
# ---------------------------------------------------------------------------
async def tc_02_evict_on_invalid_credential(cache_dir: Path) -> None:
    print("[TC-02] revalidate fail + login invalid_credential → EVICT", flush=True)
    cache = _make_cache(cache_dir)
    email = "tc02@example.com"
    key = _account_key(email)
    await cache.save(key, asdict(_make_session(email)))

    chatgpt_mock = MagicMock()
    chatgpt_mock.revalidate = AsyncMock(return_value=False)
    chatgpt_mock.login = AsyncMock(
        side_effect=LoginError(reason="invalid_credential")
    )
    chatgpt_mock.reset_openai_cookies = MagicMock(return_value=0)

    handler = _make_handler(cache)
    result = await _run_resolve(handler, chatgpt_mock, _make_parsed(email), key)
    if result is not None:
        _log_fail("TC-02", "login fail nhưng resolve trả session")

    # Cache PHẢI bị xoá
    cached = await cache.get(key)
    if cached is not None:
        _log_fail("TC-02", "cache còn dù invalid_credential — evict fail")
    _log_pass("TC-02", "cache evicted khi login fail = invalid_credential")


# ---------------------------------------------------------------------------
# TC-03: revalidate fail + login fail(account_locked) → CLEAR cache
# ---------------------------------------------------------------------------
async def tc_03_evict_on_account_locked(cache_dir: Path) -> None:
    print("[TC-03] revalidate fail + login account_locked → EVICT", flush=True)
    cache = _make_cache(cache_dir)
    email = "tc03@example.com"
    key = _account_key(email)
    await cache.save(key, asdict(_make_session(email)))

    chatgpt_mock = MagicMock()
    chatgpt_mock.revalidate = AsyncMock(return_value=False)
    chatgpt_mock.login = AsyncMock(
        side_effect=LoginError(reason="account_locked")
    )
    chatgpt_mock.reset_openai_cookies = MagicMock(return_value=0)

    handler = _make_handler(cache)
    await _run_resolve(handler, chatgpt_mock, _make_parsed(email), key)

    cached = await cache.get(key)
    if cached is not None:
        _log_fail("TC-03", "cache còn dù account_locked — evict fail")
    _log_pass("TC-03", "cache evicted khi login fail = account_locked")


# ---------------------------------------------------------------------------
# TC-04: revalidate fail + login fail(network_error) → KEEP cache
# ---------------------------------------------------------------------------
async def tc_04_keep_on_network_error(cache_dir: Path) -> None:
    print("[TC-04] revalidate fail + login network_error → KEEP", flush=True)
    cache = _make_cache(cache_dir)
    email = "tc04@example.com"
    key = _account_key(email)
    original_session = _make_session(email)
    await cache.save(key, asdict(original_session))

    chatgpt_mock = MagicMock()
    chatgpt_mock.revalidate = AsyncMock(return_value=False)
    chatgpt_mock.login = AsyncMock(
        side_effect=LoginError(reason="network_error")
    )
    chatgpt_mock.reset_openai_cookies = MagicMock(return_value=0)

    handler = _make_handler(cache)
    await _run_resolve(handler, chatgpt_mock, _make_parsed(email), key)

    # Cache PHẢI còn — network error là transient
    cached = await cache.get(key)
    if cached is None:
        _log_fail(
            "TC-04",
            "cache bị xoá dù network_error là transient — user sẽ mất khả năng "
            "retry mà không login lại",
        )
    if cached.payload["email"] != email:
        _log_fail("TC-04", "cache bị corrupt")
    _log_pass(
        "TC-04",
        "cache KEPT khi login fail = network_error (retry sau vẫn dùng được)",
    )


# ---------------------------------------------------------------------------
# TC-05: revalidate fail + login fail(mfa_required) → KEEP cache
# ---------------------------------------------------------------------------
async def tc_05_keep_on_mfa_required(cache_dir: Path) -> None:
    print("[TC-05] revalidate fail + login mfa_required → KEEP", flush=True)
    cache = _make_cache(cache_dir)
    email = "tc05@example.com"
    key = _account_key(email)
    await cache.save(key, asdict(_make_session(email)))

    chatgpt_mock = MagicMock()
    chatgpt_mock.revalidate = AsyncMock(return_value=False)
    chatgpt_mock.login = AsyncMock(
        side_effect=LoginError(reason="mfa_required")
    )
    chatgpt_mock.reset_openai_cookies = MagicMock(return_value=0)

    handler = _make_handler(cache)
    await _run_resolve(handler, chatgpt_mock, _make_parsed(email), key)

    cached = await cache.get(key)
    if cached is None:
        _log_fail("TC-05", "cache bị xoá dù mfa_required là recoverable")
    _log_pass("TC-05", "cache KEPT khi login fail = mfa_required")


# ---------------------------------------------------------------------------
# TC-06: revalidate fail + login OK → cache mới ghi đè
# ---------------------------------------------------------------------------
async def tc_06_overwrite_on_login_ok(cache_dir: Path) -> None:
    print("[TC-06] revalidate fail + login OK → cache mới ghi đè", flush=True)
    cache = _make_cache(cache_dir)
    email = "tc06@example.com"
    key = _account_key(email)

    old_session = SessionBundle(
        email=email,
        access_token="old_token",
        cookies={"__Secure-next-auth.session-token": "old_v"},
    )
    await cache.save(key, asdict(old_session))

    new_session = SessionBundle(
        email=email,
        access_token="new_token_after_login",
        cookies={"__Secure-next-auth.session-token": "new_v"},
    )
    chatgpt_mock = MagicMock()
    chatgpt_mock.revalidate = AsyncMock(return_value=False)
    chatgpt_mock.login = AsyncMock(return_value=new_session)
    chatgpt_mock.reset_openai_cookies = MagicMock(return_value=0)

    handler = _make_handler(cache)
    result = await _run_resolve(handler, chatgpt_mock, _make_parsed(email), key)
    if result is None or result.access_token != "new_token_after_login":
        _log_fail("TC-06", f"resolve không trả session mới: {result}")

    cached = await cache.get(key)
    if cached is None or cached.payload["access_token"] != "new_token_after_login":
        _log_fail("TC-06", "cache không được overwrite với session mới")
    _log_pass("TC-06", "cache overwritten với session mới sau login OK")


# ---------------------------------------------------------------------------
# TC-07: payload corrupt shape → CLEAR ngay (không đợi login result)
# ---------------------------------------------------------------------------
async def tc_07_evict_on_corrupt_payload(cache_dir: Path) -> None:
    print("[TC-07] payload corrupt shape → CLEAR ngay", flush=True)
    cache = _make_cache(cache_dir)
    email = "tc07@example.com"
    key = _account_key(email)
    # Save cache với shape KHÔNG khớp SessionBundle (thiếu access_token)
    await cache.save(key, {"email": email, "wrong_field": "junk"})

    new_session = _make_session(email)
    chatgpt_mock = MagicMock()
    chatgpt_mock.revalidate = AsyncMock(return_value=True)  # sẽ không được gọi
    chatgpt_mock.login = AsyncMock(return_value=new_session)
    chatgpt_mock.reset_openai_cookies = MagicMock(return_value=0)

    handler = _make_handler(cache)
    result = await _run_resolve(handler, chatgpt_mock, _make_parsed(email), key)
    # Login PHẢI được gọi (cache corrupt → skip revalidate → login fallback)
    chatgpt_mock.login.assert_called_once()
    chatgpt_mock.revalidate.assert_not_called()
    if result is None:
        _log_fail("TC-07", "login đáng lẽ pass nhưng resolve trả None")

    # Cache mới ghi đè cache corrupt
    cached = await cache.get(key)
    if cached is None:
        _log_fail("TC-07", "cache new không được save")
    if cached.payload.get("access_token") is None:
        _log_fail("TC-07", "cache mới thiếu access_token")
    _log_pass("TC-07", "corrupt cache → skip revalidate + login mới → cache refresh")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
async def main() -> None:
    print("=" * 70, flush=True)
    print("Test conservative cache eviction policy (hạn chế login lại)", flush=True)
    print("=" * 70, flush=True)

    with TemporaryDirectory(prefix="ideal_qr_evict_") as tmpdir:
        cache_dir = Path(tmpdir)
        # Mỗi TC dùng account_key riêng → không cần isolate dir per-tc.
        await tc_01_revalidate_ok_reuses_cache(cache_dir)
        await tc_02_evict_on_invalid_credential(cache_dir)
        await tc_03_evict_on_account_locked(cache_dir)
        await tc_04_keep_on_network_error(cache_dir)
        await tc_05_keep_on_mfa_required(cache_dir)
        await tc_06_overwrite_on_login_ok(cache_dir)
        await tc_07_evict_on_corrupt_payload(cache_dir)

    print("=" * 70, flush=True)
    print("[ALL PASS] Conservative eviction verified", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
