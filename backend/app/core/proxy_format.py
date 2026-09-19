"""Proxy line parsing + `{SID}` placeholder materialization + credential mask.

Ported từ `gpt_signup_hybrid/web/proxy_format.py` (2026-07 canonical) —
project đó đã chạy production ổn định với UPI/reg/session flow, hỗ trợ 5
format proxy phổ biến người dùng thường copy-paste và có redaction chuẩn
cho log/SSE/UI.

Pool lưu **RAW LINE / TEMPLATE** (không pre-normalize khi apply_settings) —
consumer (httpx/curl_cffi/browser) PHẢI gọi `materialize_proxy(line)` trước
mỗi request. Module pure-sync, no I/O, no network.

Format hỗ trợ:
  - `host:port`                    → `http://host:port` (no-auth)
  - `host:port:user`               → `http://user@host:port` (pass rỗng)
  - `host:port:user:pass`          → `http://user:pass@host:port`
  - `scheme://user:pass@host:port` → giữ nguyên (URL form, backward-compat)
  - `user:pass@host:port`          → `http://user:pass@host:port`
                                     (credential-at form, phổ biến ở
                                     BrightData/CliProxy/Oxylabs)

Placeholder `{SID}` (hoặc `{sid}` — case-insensitive) ở user và/hoặc pass
được thay bằng **CÙNG 1 SID** ngẫu nhiên mỗi lần `materialize_proxy` (1
base line = vô hạn sticky session → IP khác nhau mỗi lease).

Đây cũng là nơi đặt `mask_proxy` canonical (log/UI) và `sanitize_proxy_text`
(strip credential khỏi exception message trước khi phát ra SSE) để DRY.
"""

from __future__ import annotations

import hashlib
import random
import re
import string
from urllib.parse import quote

# Match cả `{SID}` lẫn `{sid}` — user import proxy thường viết chữ thường
# (VD `user-{sid}:pass`), tool phải nhận cả hai.
_SID_RE = re.compile(r"\{sid\}", re.IGNORECASE)

# Strip credential khỏi proxy URL nhúng trong text bất kỳ (exception message,
# detail...) trước khi đưa ra log/SSE/UI. Phủ cả URL materialized (random SID)
# mà `redact_message` theo-value không biết trước để replace.
_PROXY_CRED_RE = re.compile(r"//[^/@\s]+@")

_SID_ALPHABET = string.ascii_lowercase + string.digits
_DEFAULT_SID_LEN = 8


# ---------------------------------------------------------------------------
# Public helpers — SID + template detection
# ---------------------------------------------------------------------------


def gen_sid(length: int = _DEFAULT_SID_LEN) -> str:
    """Random sticky-session id `[a-z0-9]{length}`.

    Dùng cho proxy sticky-session (BrightData/CliProxy pattern `user-{SID}:pass`):
    1 SID = 1 IP; đổi SID → đổi IP nhưng cùng credential. `[a-z0-9]` không
    chứa `:` để tránh phá split `host:port:user:pass`.
    """
    return "".join(random.choice(_SID_ALPHABET) for _ in range(max(1, length)))


def has_template(line: str) -> bool:
    """True nếu `line` chứa placeholder `{SID}`/`{sid}` (case-insensitive)."""
    return bool(line) and _SID_RE.search(line) is not None


# ---------------------------------------------------------------------------
# Materialize — raw line/template → concrete URL httpx accept được
# ---------------------------------------------------------------------------


def materialize_proxy(line: str, *, sid_len: int = _DEFAULT_SID_LEN) -> str:
    """Line/template → concrete URL `scheme://[user[:pass]@]host:port`.

    Semantics:
        1. Thay MỌI `{SID}`/`{sid}` bằng CÙNG 1 SID (gen 1 lần/call, lazy).
        2. Credential được URL-encode (`quote(safe="")`) → password chứa
           `@`/`:`/`/` không phá `urlparse` khi httpx/browser tiêu thụ.
        3. URL form (có `://`) → passthrough (chỉ thay SID, KHÔNG re-parse/
           re-encode) để không double-quote credential đã đúng.
        4. Credential-at form `user:pass@host:port` (có `@`, không `://`) →
           prepend `http://` passthrough (format phổ biến provider).
        5. Colon form `host:port[:user[:pass]]` → parse + URL-encode +
           `http://` prefix.

    Args:
        line: Raw line từ pool (đã hoặc chưa strip). Có thể chứa `{SID}`.
        sid_len: Độ dài SID sinh nếu line có template. Default 8 ký tự
            (2^{~48 bits} entropy — đủ cho sticky session).

    Returns:
        URL đầy đủ cho httpx/curl_cffi/browser. Không còn placeholder.

    Raises:
        ValueError: line rỗng sau strip, hoặc format rác không parse được
            (VD chỉ có 1 phần, hoặc port không phải digit).
    """
    line = (line or "").strip()
    if not line:
        raise ValueError("empty proxy line")

    # SID thay TRƯỚC khi parse/quote → 1 SID phủ cả user + pass; SID là
    # `[a-z0-9]` không chứa `:` nên split colon-form bên dưới an toàn.
    if has_template(line):
        sid = gen_sid(sid_len)
        line = _SID_RE.sub(sid, line)

    # (3) URL form — caller đã tự chuẩn URL, không re-encode để tránh double-quote.
    if "://" in line:
        return line

    # (4) Credential-at form `user:pass@host:port`:
    # Nhiều provider (BrightData / CliProxy / Oxylabs) cho format
    # `user:pass@host:port` KHÔNG có scheme. Nếu có `@` → tách bằng `@`
    # cuối cùng; phần SAU phải match `host:port` (port toàn digit) → prepend
    # `http://`. Nếu phần SAU `@` KHÔNG match host:port → fallback colon-split
    # (backward-compat cho edge case `user@domain` trong colon-form).
    if "@" in line:
        at_pos = line.rfind("@")
        after_at = line[at_pos + 1 :]
        colon_pos = after_at.rfind(":")
        if colon_pos > 0:
            host = after_at[:colon_pos]
            port = after_at[colon_pos + 1 :]
            if host and port and port.isdigit():
                return f"http://{line}"

    # (5) Colon form `host:port[:user[:pass]]`. Split maxsplit=3 để `pass`
    # giữ được dấu `:` nội tại (rất hiếm nhưng phòng ngừa).
    parts = line.split(":", 3)
    n = len(parts)
    if n == 2:
        host, port = parts
        return f"http://{host}:{port}"
    if n == 3:
        host, port, user = parts
        return f"http://{quote(user, safe='')}@{host}:{port}"
    if n == 4:
        host, port, user, pwd = parts
        return (
            f"http://{quote(user, safe='')}:{quote(pwd, safe='')}"
            f"@{host}:{port}"
        )
    raise ValueError(
        f"invalid proxy format (need host:port[:user[:pass]]): {line!r}"
    )


# ---------------------------------------------------------------------------
# Backward-compat alias — code cũ (ProxyPool._materialize) gọi
# `materialize_template(template, sid)`. Ánh xạ sang `materialize_proxy` để
# giữ 1 tên public nhưng KHÔNG break callsite hiện có.
# ---------------------------------------------------------------------------


def materialize_template(template: str, sid: str) -> str:
    """Legacy: thay literal `{SID}` bằng `sid` cụ thể do caller cung cấp.

    Khác `materialize_proxy` ở 2 điểm:
        - SID do caller truyền (KHÔNG gen ngẫu nhiên).
        - KHÔNG tự prepend `http://` cho colon-form (giữ nguyên chuỗi đã
          thay SID). Caller (`ProxyPool.acquire`) chịu trách nhiệm feed
          cho httpx.

    Được giữ để không break `ProxyPool._materialize` cũ. Kịch bản mới nên
    dùng `materialize_proxy(line)` trực tiếp — SID tự gen, URL đầy đủ.
    """
    return template.replace("{SID}", sid).replace("{sid}", sid)


# ---------------------------------------------------------------------------
# Redaction — mask credentials cho log/UI/SSE
# ---------------------------------------------------------------------------


def sanitize_proxy_text(text: str) -> str:
    """Thay `//user:pass@` → `//***@` trong text bất kỳ (chống leak creds).

    Dùng cho exception message / SSE payload / stderr log — bất cứ nơi nào
    proxy URL có thể xuất hiện nhưng caller không biết trước giá trị
    materialized để `redact_message` theo-value.
    """
    return _PROXY_CRED_RE.sub("//***@", text)


def mask_proxy(url: str | None) -> str:
    """Mask credential cho log/UI. `None`/rỗng → `'direct'`.

    Xử cả 2 shape (pool lưu raw line colon-form, log dùng URL materialized):
        - URL form `scheme://user:pass@host:port` → `scheme://***@host:port`
        - Colon raw `host:port:user:pass`        → `***@host:port`
        - Credential-at `user:pass@host:port`    → `***@host:port`
        - No-auth (`host:port` / `scheme://host:port`) → trả nguyên
          (không có credential để mask).
    """
    if not url:
        return "direct"
    scheme, sep, rest = url.partition("://")
    if sep:
        # URL form (có scheme).
        if "@" not in rest:
            return url
        host_part = rest.rsplit("@", 1)[-1]
        return f"{scheme}://***@{host_part}"
    if "@" in url:
        # `user:pass@host:port` (không scheme).
        return f"***@{url.rsplit('@', 1)[-1]}"
    # Colon-form raw line: `host:port[:user[:pass]]`.
    parts = url.split(":", 3)
    if len(parts) >= 3:
        return f"***@{parts[0]}:{parts[1]}"
    return url


def safe_proxy_id(proxy_id: str) -> str:
    """Non-reversible short stable identifier cho log/SSE `proxy_id` field.

    Pool `proxy_id` thường là raw line hoặc template chứa credential/SID —
    KHÔNG được ghi thẳng vào log. SHA-256 hex prefix ổn định trong 1 run
    (cùng input → cùng output) nhưng không reverse được về raw id.
    Chuỗi rỗng → sentinel cố định (tránh hash rỗng gây nhầm).
    """
    if not proxy_id:
        return "p_none"
    digest = hashlib.sha256(proxy_id.encode("utf-8")).hexdigest()
    return f"p_{digest[:12]}"


__all__ = [
    "gen_sid",
    "has_template",
    "materialize_proxy",
    "materialize_template",
    "mask_proxy",
    "safe_proxy_id",
    "sanitize_proxy_text",
]
