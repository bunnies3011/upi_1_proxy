"""HTTP client facade — wrap `curl_cffi.requests.AsyncSession`.

Điểm tập trung DUY NHẤT cho việc tạo async HTTP client trong backend. Lý do:

1. Chrome TLS/JA3/H2 impersonation qua `impersonate` (mặc định
   `DEFAULT_IMPERSONATE`, hiện `chrome136`) để bypass Cloudflare 403
   (chatgpt.com). `curl_cffi` dùng BoringSSL bundled — không
   đi qua Python `ssl` module, nên `truststore.inject_into_ssl()` (đã gỡ ở
   `app/__init__.py`) không còn tác dụng và cũng không còn cần thiết.

2. Alias exception cho code cũ từng bắt `httpx.TimeoutException` /
   `httpx.NetworkError` / `httpx.TransportError` / `httpx.HTTPError` /
   `httpx.DecodingError` / `httpx.ResponseNotRead`. Mapping (xem doc bên
   dưới) giữ SEMANTIC — `except (TimeoutException, NetworkError,
   TransportError):` tiếp tục bao trọn mọi lỗi transport level.

3. Điểm mock cho tests — `tests/support/fake_http.FakeAsyncSession` được
   inject qua monkeypatch `create_async_client`.

Note về `Response`:
   `curl_cffi.requests.Response` có `.status_code`, `.text`, `.content`,
   `.headers`, `.cookies` (Cookies obj có `.jar` = `http.cookiejar.CookieJar`,
   `.get(name, domain=..., path=...)`, raise `CookieConflict` khi trùng),
   `.url` (str), `.history` (list), `.json()` (sync). API tương đương httpx
   ở mức code chúng ta dùng.
"""
from __future__ import annotations

from typing import Any, Final

from curl_cffi.requests import AsyncSession, Response  # re-export
from curl_cffi.requests import exceptions as _cffi_exc
from curl_cffi.requests.errors import CookieConflict  # re-export

# ---------------------------------------------------------------------------
# Public types (re-export)
# ---------------------------------------------------------------------------

__all__ = [
    "AsyncSession",
    "Response",
    "CookieConflict",
    "TimeoutException",
    "NetworkError",
    "TransportError",
    "HTTPError",
    "DecodingError",
    "ResponseNotRead",
    "create_async_client",
    "classify_error",
    "is_timeout",
    "is_network_error",
    "DEFAULT_IMPERSONATE",
]

# ---------------------------------------------------------------------------
# Exception aliases — semantic equivalents của httpx
# ---------------------------------------------------------------------------

#: Timeout (kết nối / đọc). curl_cffi `Timeout` là parent của `ConnectTimeout`
#: và `ReadTimeout` → catch một chỗ.
TimeoutException = _cffi_exc.Timeout

#: Lỗi kết nối (DNS, TCP, TLS handshake, proxy). curl_cffi `ConnectionError`
#: là parent của `DNSError`, `SSLError`, `ConnectTimeout`. `ProxyError` KHÔNG
#: nằm dưới `ConnectionError` — thêm riêng qua tuple ở `is_network_error`.
NetworkError = _cffi_exc.ConnectionError

#: Base transport-level. Trong httpx, `TransportError` bao trọn timeout +
#: network + protocol. Ở curl_cffi, `RequestException` là base rộng nhất
#: bao gồm cả HTTP status errors — không hoàn toàn 1-1 nhưng đủ cho các
#: `except (TimeoutException, NetworkError, TransportError):` hiện tại
#: (redundant catch — hai đầu là subclass của `TransportError` này).
TransportError = _cffi_exc.RequestException

#: Base HTTP error — dùng cho `except httpx.HTTPError` (catch-all).
HTTPError = _cffi_exc.RequestException

#: Body decoding error (gzip/brotli/utf-8 fail). curl_cffi có
#: `ContentDecodingError`. Code cũ thường kèm `ValueError`/`UnicodeDecodeError`.
DecodingError = _cffi_exc.ContentDecodingError


class ResponseNotRead(RuntimeError):
    """Placeholder cho `httpx.ResponseNotRead`.

    curl_cffi không có khái niệm stream-not-yet-read (response luôn được
    fully materialized). Giữ class này để `except (ResponseNotRead,
    RuntimeError):` ở code cũ vẫn compile mà không cần rewrite logic.
    """


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

#: Profile impersonate mặc định — Chrome 136 desktop.
#:
#: LỊCH SỬ:
#:   - 2026-06: dùng `chrome131` (khi đó là Chrome stable mới nhất được
#:     curl_cffi hỗ trợ).
#:   - 2026-07-07: bump lên `chrome136` — Cloudflare đưa fingerprint
#:     TLS/JA3/H2 của Chrome 131 vào diện `cf-mitigated: challenge`, gây
#:     403 lặp lại trên `chatgpt.com` (verify bằng probe: chrome131 → 403
#:     với cf-mitigated=challenge; chrome136/142/145/146 → 200 OK).
#:
#: Khi bump profile, PHẢI đồng bộ `_SENTINEL_UA` ở
#: `app/payments/ideal/sentinel.py` — OpenAI Sentinel verify UA embedded
#: trong PoW payload khớp `User-Agent` wire, mismatch → reject request.
#:
#: Có thể override khi cần thử fallback (chrome142/chrome145/chrome146)
#: nếu Cloudflare đổi thêm.
DEFAULT_IMPERSONATE: Final[str] = "chrome136"


def create_async_client(
    *,
    proxy: str | None = None,
    timeout: float | tuple[float, float] = 30.0,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    allow_redirects: bool = True,
    verify: bool = True,
    impersonate: str | None = DEFAULT_IMPERSONATE,
    **extra: Any,
) -> AsyncSession:
    """Khởi tạo `AsyncSession` với default consistent cho backend.

    Args:
        proxy: URL proxy đã materialize (http://user:pass@host:port). None
            = direct connection.
        timeout: Wall-clock timeout mặc định. Chấp nhận:
            - `float` — tổng timeout (connect + read) áp cho mọi request.
            - `tuple[float, float]` — `(connect_timeout, read_timeout)`,
              tách bạch phase connect và phase read. Dùng cho probe/preflight
              nơi dead proxy đa số fail ở TCP connect → cắt sớm không chờ
              đủ tổng timeout.
        headers: Default headers merge với header do `impersonate` set.
            KHÔNG cần set User-Agent hay Sec-CH-UA-* — `impersonate` tự lo.
        cookies: Cookies khởi tạo cho session (dict name→value).
        allow_redirects: Follow redirects tự động ở session-level. Override
            được per-request.
        verify: TLS cert verification. Mặc định True.
        impersonate: Chrome profile để set TLS/JA3/H2/header fingerprint.
            `None` → KHÔNG impersonate (curl_cffi dùng TLS/HTTP plain, tiết
            kiệm handshake overhead cho endpoint không có Cloudflare / không
            fingerprint-gate — ví dụ probe endpoint `api64.ipify.org`).
        **extra: Kwarg khác pass thẳng cho `AsyncSession` (e.g. `retry`,
            `max_redirects`).

    Returns:
        `AsyncSession` — dùng như context manager (`async with`) hoặc
        đóng thủ công qua `.close()`.
    """
    kwargs: dict[str, Any] = {
        "timeout": timeout,
        "allow_redirects": allow_redirects,
        "verify": verify,
    }
    # Chỉ pass `impersonate` khi non-None — curl_cffi mặc định KHÔNG
    # impersonate; pass explicit `None` xuống AsyncSession không phá gì
    # nhưng giữ signature rõ ràng: caller nào cần fingerprint mới truyền.
    if impersonate is not None:
        kwargs["impersonate"] = impersonate
    if proxy is not None:
        kwargs["proxy"] = proxy
    if headers is not None:
        kwargs["headers"] = headers
    if cookies is not None:
        kwargs["cookies"] = cookies
    kwargs.update(extra)
    return AsyncSession(**kwargs)


# ---------------------------------------------------------------------------
# Error classification — dùng cho proxy_health & retry logic
# ---------------------------------------------------------------------------

def is_timeout(exc: BaseException) -> bool:
    """True nếu exc là bất kỳ dạng timeout (connect/read/wall)."""
    return isinstance(exc, _cffi_exc.Timeout)


def is_network_error(exc: BaseException) -> bool:
    """True nếu exc là lỗi transport (DNS/TCP/TLS/proxy/timeout).

    Broader than `is_timeout` — bao gồm cả `ProxyError` (không phải subclass
    của `ConnectionError` trong curl_cffi).
    """
    return isinstance(
        exc,
        (
            _cffi_exc.Timeout,
            _cffi_exc.ConnectionError,
            _cffi_exc.ProxyError,
        ),
    )


def classify_error(exc: BaseException) -> str:
    """Phân loại exception thành label cố định cho `proxy_health`.

    Returns:
        - "timeout": bất kỳ Timeout (connect/read/pool).
        - "proxy_auth": ProxyError với 407 (proxy authentication required).
        - "proxy_connect": ProxyError khác 407 (proxy chết / refused).
        - "dns": DNSError (resolve host fail).
        - "ssl": SSLError / CertificateVerifyError.
        - "connect": ConnectionError khác (TCP refused, unreachable).
        - "protocol": HTTP-level (invalid response, too many redirects).
        - "unknown": các RequestException khác (or non-cffi exception).
    """
    if isinstance(exc, _cffi_exc.Timeout):
        return "timeout"
    if isinstance(exc, _cffi_exc.ProxyError):
        msg = str(exc)
        if "407" in msg:
            return "proxy_auth"
        return "proxy_connect"
    if isinstance(exc, _cffi_exc.DNSError):
        return "dns"
    if isinstance(exc, _cffi_exc.SSLError):
        return "ssl"
    if isinstance(exc, _cffi_exc.ConnectionError):
        return "connect"
    if isinstance(
        exc,
        (
            _cffi_exc.HTTPError,
            _cffi_exc.TooManyRedirects,
            _cffi_exc.ChunkedEncodingError,
        ),
    ):
        return "protocol"
    return "unknown"
