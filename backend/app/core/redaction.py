"""Sensitive_Data_Redaction helper — Requirements 1.9, 3.6, 4.9, 14.8.

Module thuộc `core/`, KHÔNG import bất kỳ thứ gì từ `app.payments.*`
(Payment_Module_Boundary).

Cung cấp 2 hàm dùng chung cho toàn hệ thống khi ghi log realtime / phát qua
kênh SSE:

- `redact_dict(payload)`: redact theo TÊN FIELD (key) trong dict, đệ quy vào
  nested dict/list.
- `redact_message(message, known_secrets)`: redact theo GIÁ TRỊ đã biết
  (ví dụ giá trị `sig`/cookie/access_token cụ thể của 1 job) xuất hiện ở vị
  trí bất kỳ trong 1 chuỗi log tự do.

Cả 2 hàm dùng chung mask cố định `_REDACTED_MASK` để không lộ giá trị thô
theo Sensitive_Data_Redaction (Glossary).
"""

from __future__ import annotations

_REDACTED_MASK = "***REDACTED***"

# Tên field nhạy cảm (case-insensitive), bao quát các trường liệt kê ở
# Glossary "Sensitive_Data_Redaction" + Requirement 1.9 (password,
# totp_secret, cookie, access_token), Requirement 3.6 (token JS-runtime của
# Stripe: js_checksum, rv_timestamp, passive_captcha_token), Requirement 4.9
# (sig), Requirement 14.8 (thêm proxy credential, web.auth_token).
#
# Lưu tên field THUẦN (không kèm namespace/dot) — `redact_dict` tự xử lý
# match theo full key hoặc theo phần cuối sau dấu "." (ví dụ key
# "web.auth_token" khớp field "auth_token").
_SENSITIVE_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "password",
        "totp_secret",
        "cookie",
        "cookies",
        "access_token",
        "auth_token",
        "sig",
        "js_checksum",
        "rv_timestamp",
        "passive_captcha_token",
        "proxy_credential",
        "proxy_username",
        "proxy_password",
        # Telegram bot token — format `<int>:<AA...>` với AA là 35 ký tự
        # base64url; ai có token này có toàn quyền điều khiển bot. Redact
        # cả key `bot_token` (namespace-scoped `telegram.bot_token` được
        # match qua tail sau dấu `.`) và alias `telegram_bot_token`.
        "bot_token",
        "telegram_bot_token",
        # YesCaptcha / generic API keys (OaiPay solver) — field-name backstop.
        "api_key",
        "yescaptcha_api_key",
    }
)


def _is_sensitive_key(key: str) -> bool:
    """So khớp `key` với `_SENSITIVE_FIELD_NAMES`, case-insensitive.

    Hỗ trợ key dạng `namespace.field` (ví dụ `web.auth_token`) bằng cách so
    khớp cả full key và phần cuối sau dấu `.` cuối cùng.
    """
    lowered = key.lower()
    if lowered in _SENSITIVE_FIELD_NAMES:
        return True
    tail = lowered.rsplit(".", 1)[-1]
    return tail in _SENSITIVE_FIELD_NAMES


def redact_dict(payload: dict) -> dict:
    """Trả về dict MỚI với mọi giá trị của field nhạy cảm đã bị mask.

    - KHÔNG mutate `payload` gốc.
    - Đệ quy vào nested dict/list (list chứa dict cũng được xử lý).
    - Match key case-insensitive, hỗ trợ key dạng `namespace.field`.
    """
    return _redact_value(payload)  # type: ignore[return-value]


def _redact_value(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if isinstance(key, str) and _is_sensitive_key(key):
                result[key] = _REDACTED_MASK
            else:
                result[key] = _redact_value(item)
        return result
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    return value


def redact_message(message: str, known_secrets: list[str]) -> str:
    """Thay thế mọi occurrence của mỗi giá trị trong `known_secrets` bằng mask.

    - Bỏ qua (skip) chuỗi rỗng trong `known_secrets` — không được match toàn
      bộ `message` (chuỗi rỗng là substring của mọi chuỗi).
    - Thay thế toàn bộ occurrence (không chỉ occurrence đầu tiên), tại vị trí
      bất kỳ trong `message`.
    """
    redacted = message
    for secret in known_secrets:
        if not secret:
            continue
        redacted = redacted.replace(secret, _REDACTED_MASK)
    return redacted
