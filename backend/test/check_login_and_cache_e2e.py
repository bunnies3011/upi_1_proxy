"""E2E test: login thật + cache reuse (revalidate) với account thật.

Mục tiêu (progress realtime, mỗi bước 1 dòng log):
    [TC-01] Login zero-state → SessionBundle hợp lệ + cache saved.
    [TC-02] Load cache + revalidate → PASS (không login lại).
    [TC-03] Kiểm tra jar populate scoped=".chatgpt.com" sau revalidate.
    [TC-04] Kiểm tra kích thước cache (essential-only, ~5 cookies).

Redaction: KHÔNG log password/totp_secret. Log email + step + status thôi.

Chạy: `python3 test/check_login_and_cache_e2e.py`
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

# Setup import path
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_ROOT))

from app.core import http_client as http  # noqa: E402
from app.core.payment_flow import AccountLineError  # noqa: E402
from app.core.session_cache import AccountSessionCache  # noqa: E402
from app.payments.ideal.chatgpt_client import ChatgptClient  # noqa: E402
from app.payments.ideal.errors import LoginError  # noqa: E402
from app.payments.ideal.models import (  # noqa: E402
    SessionBundle,
    parse_account_line,
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Danh sách account thử — chỉ dùng account ĐẦU TIÊN pass sẽ dừng (tránh
# spam Cloudflare + rate-limit login). Format ``email|password|totp_secret``.
_ACCOUNT_LINES: list[str] = [
    "roves_vestige2i+uf7a7n@icloud.com|Anczz123456789@|7IVU46EENTYEQ4RS6UEH7PODSQHTLJJZ",
    "zealous-flint49+e4l26v@icloud.com|Anczz123456789@|JGRCEBRM7CFUKKL2NRHP2EE2DZGWFUV2",
    "spikier_gaze_54+b3ejl01@icloud.com|Anczz123456789@|CPN4HIYKEKCTFACTI7OYUWYNMXQEKFSG",
    "roves_vestige2i+h5l0kw0@icloud.com|Anczz123456789@|7U77ARGBEGZDYUXCQ3OSWVQZOLV6CVTB",
    "zealous-flint49+cxrmk1@icloud.com|Anczz123456789@|B5O37BHBLTN74QHZGVR5O5SSGTRTRWUW",
    "spikier_gaze_54+ud9sc5b@icloud.com|Anczz123456789@|LQTDPUNCOWCPKUK6Z2EBS6GQPMY5RWWA",
    "roves_vestige2i+16jzd@icloud.com|Anczz123456789@|UK2AXZP5APOGMPKR4CNEPCWUIXRK7MKD",
    "zealous-flint49+1ilo5@icloud.com|Anczz123456789@|5AFS4ZE4DDGKKY5QC2QXHC56LTAL7OBA",
    "spikier_gaze_54+ttuwfas@icloud.com|Anczz123456789@|F7QKCGB4FTOGJMO3DEOBHLMLR7AQ5T7H",
    "roves_vestige2i+pr1ja@icloud.com|Anczz123456789@|4B5NVKUS7UEJFF7W52MPR5E6MAH2M5SX",
    "zealous-flint49+1kcd2y3@icloud.com|Anczz123456789@|N6MMRAAN3L6UAFXJZ6F57GJ7LEVP24OO",
    "spikier_gaze_54+at4zw@icloud.com|Anczz123456789@|ARI4REDWQMVP24SCATZZTWWWROMMBYJX",
]

_ACCOUNT_KEY_HEX_LENGTH = 32
_HTTP_TIMEOUT = 60.0


# ---------------------------------------------------------------------------
# Redaction-safe logging
# ---------------------------------------------------------------------------
class _RedactFormatter(logging.Formatter):
    """Formatter redact password/totp_secret trong log messages."""

    _secrets: list[str] = []

    @classmethod
    def add_secret(cls, s: str) -> None:
        if s and s not in cls._secrets:
            cls._secrets.append(s)

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        for s in self._secrets:
            if s and s in msg:
                msg = msg.replace(s, "***REDACTED***")
        return msg


_handler = logging.StreamHandler()
_handler.setFormatter(_RedactFormatter("%(asctime)s [%(name)s] %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
_LOG = logging.getLogger("e2e")


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------
def _account_key(email: str) -> str:
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:_ACCOUNT_KEY_HEX_LENGTH]


class _FakeSettingsRepo:
    """Minimal settings repo cho AccountSessionCache init (không dùng DB)."""

    pass


def _log_pass(tc: str, msg: str) -> None:
    print(f"[PASS] {tc} — {msg}", flush=True)


def _log_fail(tc: str, msg: str) -> None:
    print(f"[FAIL] {tc} — {msg}", flush=True)


def _log_step(tc: str, msg: str) -> None:
    print(f"[{tc}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Core test flow — try 1 account, dừng ngay khi login pass
# ---------------------------------------------------------------------------
async def try_account(
    line: str,
    idx: int,
    cache_dir: Path,
) -> tuple[bool, str | None]:
    """Thử login + cache reuse với 1 account.

    Returns:
        (success: bool, email: str | None) — success=True khi mọi TC pass.
    """
    parsed = parse_account_line(line)
    if isinstance(parsed, AccountLineError):
        _log_fail(f"account#{idx}", f"parse fail: {parsed}")
        return False, None

    email = parsed.email
    account_key = _account_key(email)

    # Đăng ký secrets để redact log (password + totp).
    _RedactFormatter.add_secret(parsed.password)
    if parsed.totp_secret:
        _RedactFormatter.add_secret(parsed.totp_secret)

    # Setup cache
    cache = AccountSessionCache(_FakeSettingsRepo(), cache_dir)  # type: ignore
    cache.apply_settings(
        {"session_cache.enabled": True, "session_cache.ttl_hours": 24}
    )
    await cache.clear(account_key)  # zero-state
    _log_step(
        f"account#{idx}",
        f"start email={email} (cache_dir={cache_dir.name}, account_key={account_key[:12]}…)",
    )

    # =====================================================================
    # TC-01: Login zero-state
    # =====================================================================
    print(f"[TC-01/{idx}] LOGIN — {email}", flush=True)
    async with http.create_async_client(timeout=_HTTP_TIMEOUT) as client:
        chatgpt_client = ChatgptClient(
            http_client=client,
            session_cache=cache,
            logger=_LOG,
        )
        try:
            session = await chatgpt_client.login(
                email=parsed.email,
                password=parsed.password,
                totp_secret=parsed.totp_secret,
            )
        except LoginError as exc:
            _log_fail(
                f"TC-01/{idx}",
                f"LoginError reason={exc.reason} email={email}",
            )
            return False, email
        except Exception as exc:  # noqa: BLE001
            _log_fail(
                f"TC-01/{idx}",
                f"Exception: {type(exc).__name__}={exc}",
            )
            return False, email

        # Verify shape
        if not session.access_token or len(session.access_token) < 20:
            _log_fail(
                f"TC-01/{idx}",
                f"access_token quá ngắn ({len(session.access_token)} chars)",
            )
            return False, email

        _RedactFormatter.add_secret(session.access_token)
        _log_pass(
            f"TC-01/{idx}",
            f"login OK — access_token_len={len(session.access_token)} "
            f"cookies={len(session.cookies)} (essential-only filtered)",
        )

        # Save cache
        payload = asdict(session)
        await cache.save(account_key, payload)
        cache_file = cache._cache_file_path(account_key)  # noqa: SLF001
        if not cache_file.exists():
            _log_fail(f"TC-01/{idx}", "cache file không được tạo")
            return False, email
        _log_pass(
            f"TC-01/{idx}",
            f"cache saved: {cache_file.name} ({cache_file.stat().st_size} bytes)",
        )

    # =====================================================================
    # TC-02: Cache hit + revalidate (LẦN 2 — không login lại)
    # =====================================================================
    print(f"[TC-02/{idx}] CACHE HIT + REVALIDATE — {email}", flush=True)
    async with http.create_async_client(timeout=_HTTP_TIMEOUT) as client2:
        chatgpt_client2 = ChatgptClient(
            http_client=client2,
            session_cache=cache,
            logger=_LOG,
        )

        cached = await cache.get(account_key)
        if cached is None:
            _log_fail(f"TC-02/{idx}", "cache miss dù vừa save")
            return False, email

        # Rebuild SessionBundle từ payload
        try:
            cached_session = SessionBundle(
                email=cached.payload["email"],
                access_token=cached.payload["access_token"],
                cookies=cached.payload["cookies"],
            )
        except (KeyError, TypeError) as exc:
            _log_fail(f"TC-02/{idx}", f"cache payload shape corrupt: {exc}")
            return False, email

        result = await chatgpt_client2.revalidate(cached_session)
        if result is not True:
            _log_fail(
                f"TC-02/{idx}",
                "revalidate trả False — cache không dùng được, "
                "sẽ phải login lại (fail mục tiêu 'hạn chế login lại')",
            )
            return False, email
        _log_pass(
            f"TC-02/{idx}",
            "revalidate OK — cache tái sử dụng, KHÔNG login lại",
        )

        # =====================================================================
        # TC-03: Jar populate scoped đúng — không có cookies "wildcard"
        # nguy hiểm (những cookies KHÔNG phải __Host-/__Secure- prefix nhưng
        # có domain rỗng — nguồn của 431). Cookies __Host- prefix theo RFC
        # 6265 §4.1.3.1 BẮT BUỘC không có Domain attribute → libcurl
        # normalize domain=""; đây là behavior đúng, không phải pollution.
        # =====================================================================
        print(f"[TC-03/{idx}] JAR SCOPED CHECK", flush=True)
        jar_cookies = list(client2.cookies.jar)
        chatgpt_scoped: list[str] = []
        host_prefix_no_domain: list[str] = []  # OK theo RFC 6265
        wrong_domain: list[str] = []           # NGUY HIỂM (bug 431)
        for c in jar_cookies:
            if c.domain == ".chatgpt.com":
                chatgpt_scoped.append(c.name)
                continue
            if not c.domain:
                # __Host- prefix theo RFC BẮT BUỘC không có domain → OK.
                if c.name.startswith("__Host-"):
                    host_prefix_no_domain.append(c.name)
                else:
                    wrong_domain.append(f"{c.name}(empty)")
        if wrong_domain:
            _log_fail(
                f"TC-03/{idx}",
                f"jar có cookies domain rỗng nguy hiểm (bug 431): {wrong_domain}",
            )
            return False, email
        if not chatgpt_scoped:
            _log_fail(f"TC-03/{idx}", "jar không có cookies chatgpt.com scoped")
            return False, email
        _log_pass(
            f"TC-03/{idx}",
            f"jar scoped .chatgpt.com có {len(chatgpt_scoped)} cookies + "
            f"{len(host_prefix_no_domain)} __Host- (RFC-compliant): "
            f"{chatgpt_scoped + host_prefix_no_domain}",
        )

    # =====================================================================
    # TC-04: Kích thước cache reasonable (essential-only)
    # =====================================================================
    print(f"[TC-04/{idx}] CACHE SIZE CHECK", flush=True)
    cache_size = cache_file.stat().st_size
    if cache_size > 20_000:
        _log_fail(
            f"TC-04/{idx}",
            f"cache quá lớn ({cache_size} bytes > 20KB) — filter whitelist "
            "không hoạt động",
        )
        return False, email
    if len(session.cookies) > 10:
        _log_fail(
            f"TC-04/{idx}",
            f"cookies quá nhiều ({len(session.cookies)} > 10) — filter fail",
        )
        return False, email
    _log_pass(
        f"TC-04/{idx}",
        f"cache size {cache_size} bytes, {len(session.cookies)} cookies "
        f"(essential-only, <20KB target)",
    )

    return True, email


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
async def main() -> int:
    print("=" * 70, flush=True)
    print("E2E: login thật + cache reuse (revalidate) — check fix HTTP 431", flush=True)
    print("=" * 70, flush=True)

    with TemporaryDirectory(prefix="ideal_qr_e2e_") as tmpdir:
        cache_dir = Path(tmpdir)
        print(f"cache_dir tạm: {cache_dir}", flush=True)

        for idx, line in enumerate(_ACCOUNT_LINES, start=1):
            print("-" * 70, flush=True)
            print(f"Thử account #{idx}/{len(_ACCOUNT_LINES)}", flush=True)
            success, email = await try_account(line, idx, cache_dir)
            if success:
                print("=" * 70, flush=True)
                print(
                    f"[ALL PASS] account#{idx} ({email}) — "
                    "login + cache + revalidate hoạt động triệt để",
                    flush=True,
                )
                return 0
            print(f"account#{idx} không pass, thử account tiếp theo...", flush=True)

        print("=" * 70, flush=True)
        print(
            f"[FAIL] Đã thử hết {len(_ACCOUNT_LINES)} accounts, không có "
            "account nào pass — có thể do network/proxy/Cloudflare block",
            flush=True,
        )
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
