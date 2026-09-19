"""Formatter thuần function cho Telegram notification.

Tách bạch khỏi `notifier.py` để test độc lập (không cần HTTP client / settings).
Toàn bộ hàm ở đây là pure — không I/O, không state.

Ba trách nhiệm chính:
    1. `mask_email(email)`: che phần local của email theo rule "2 ký tự
       đầu + domain" (VD `abc@gmail.com` → `ab***@gmail.com`).
    2. `format_vn_time(epoch)`: convert epoch UTC → chuỗi `HH:MM:SS DD/MM/YYYY`
       theo timezone `Asia/Ho_Chi_Minh`.
    3. `build_caption(...)`: build HTML caption cho `sendPhoto` — Telegram
       Bot API hỗ trợ subset HTML (<b>, <code>, <a href>).

Ghi chú Fail_Fast: cả 3 hàm KHÔNG raise cho input hợp lệ về type; edge
case (email không có `@`, epoch âm) được xử lý an toàn (trả string mask
`***` hoặc timestamp fallback), giữ Sensitive_Data_Redaction thành công
tuyệt đối.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from html import escape

# UTC+7 fixed — Việt Nam không có DST. Dùng `timedelta` thay vì `ZoneInfo`
# để tránh phụ thuộc tzdata trên container Alpine/scratch (tzdata có thể
# thiếu). Semantic đúng: UTC+7 áp dụng quanh năm cho VN.
_VN_TZ = timezone(timedelta(hours=7))
_IN_TZ = timezone(timedelta(hours=5, minutes=30))

# Ký tự mask cố định — giữ đồng bộ với `redaction._REDACTED_MASK` về mặt
# visual (dùng `***` ngắn cho readability trong caption Telegram).
_MASK = "***"


def extract_email(account_line: str) -> str:
    """Cắt phần email đầu (trước dấu `|`) từ dòng account thô.

    Định dạng account_line (theo `payments/ideal/models.parse_account_line`):
    `email|password|totp_secret` hoặc `email|access_token`. Hàm KHÔNG
    validate email format — trả nguyên phần trước `|`. Nếu không có `|`,
    trả nguyên chuỗi đã strip.

    Trả `""` khi input rỗng/whitespace-only — caller (`mask_email`) sẽ
    xử lý fallback.
    """
    if not account_line:
        return ""
    stripped = account_line.strip()
    if not stripped:
        return ""
    return stripped.split("|", 1)[0].strip()


def mask_email(account_line: str) -> str:
    """Che email theo rule "2 ký tự đầu + domain" đã chốt với user.

    Ví dụ:
        `abc@gmail.com`         → `ab***@gmail.com`
        `a@gmail.com`           → `a***@gmail.com` (chỉ 1 ký tự → giữ)
        `abcdef@corp.io|pw|totp`→ `ab***@corp.io`
        `noatsign`              → `***`
        `""` / whitespace       → `***`

    Không raise, không lộ ký tự thứ 3+ của local part.
    """
    email = extract_email(account_line)
    if not email or "@" not in email:
        return _MASK
    local, _, domain = email.partition("@")
    if not local:
        return f"{_MASK}@{domain}" if domain else _MASK
    if not domain:
        # Trường hợp `abc@` (trailing @) — hiếm, mask toàn bộ để an toàn.
        return _MASK
    # Rule "2 ký tự đầu + domain". Nếu local <2 ký tự, giữ nguyên số ký
    # tự thực có (không pad) — vẫn theo pattern `<prefix>***@<domain>`.
    prefix = local[:2]
    return f"{prefix}{_MASK}@{domain}"


def format_vn_time(epoch_seconds: float | None) -> str:
    """Format epoch UTC → `HH:MM:SS DD/MM/YYYY` (Asia/Ho_Chi_Minh).

    `None` hoặc giá trị âm/không hợp lệ → trả string `"—"` để caption
    không hiển thị số 0 gây confuse. Không raise.
    """
    if epoch_seconds is None:
        return "—"
    try:
        # Dùng utcfromtimestamp KHÔNG được (deprecated Python 3.12); dùng
        # fromtimestamp với tz=UTC rồi convert sang VN.
        dt_utc = datetime.fromtimestamp(float(epoch_seconds), tz=timezone.utc)
    except (OSError, ValueError, OverflowError):
        return "—"
    dt_vn = dt_utc.astimezone(_VN_TZ)
    return dt_vn.strftime("%H:%M:%S %d/%m/%Y")


def _format_time(epoch_seconds: float | None, tz: timezone) -> str:
    if epoch_seconds is None:
        return "—"
    try:
        dt_utc = datetime.fromtimestamp(float(epoch_seconds), tz=timezone.utc)
    except (OSError, ValueError, OverflowError):
        return "—"
    return dt_utc.astimezone(tz).strftime("%H:%M:%S %d/%m/%Y")


def format_india_time(epoch_seconds: float | None) -> str:
    """Format epoch UTC → `HH:MM:SS DD/MM/YYYY` (India Standard Time)."""
    return _format_time(epoch_seconds, _IN_TZ)


def _is_upi_payment_method(payment_method: str | None) -> bool:
    if not payment_method:
        return False
    return payment_method.lower().startswith("upi")


def _resolve_chat_target(chat_label: str | None, chat_id: str | None) -> str:
    label = (chat_label or "").strip()
    if label:
        return label
    return (chat_id or "").strip()


def build_caption(
    account_line: str,
    finished_at: float | None,
    payment_link: str | None = None,
    payment_method: str | None = None,
    order: int | None = None,
    qr_expires_at: float | None = None,
    chat_label: str | None = None,
    chat_id: str | None = None,
    sent_at: float | None = None,
    username: str | None = None,
    first_name: str | None = None,
) -> str:
    """Build HTML caption cho `sendPhoto` Telegram.

    Ưu tiên hiển thị tên Scanner (người bấm nhận job: @username hoặc first_name).
    Nếu không có scanner bấm nhận, fallback về chat_label.
    """
    full_email = extract_email(account_line) or account_line.strip()
    vn_time = format_vn_time(finished_at)

    scanner_identity = ""
    stripped_user = (username or "").strip()
    stripped_fn = (first_name or "").strip()
    if stripped_user:
        scanner_identity = f"@{stripped_user}"
    elif stripped_fn:
        scanner_identity = stripped_fn
    else:
        target = _resolve_chat_target(chat_label, chat_id)
        if target and target.lower() not in ("chatgpt", "new upi bot"):
            scanner_identity = target

    if _is_upi_payment_method(payment_method):
        qr_no = f"#{order}" if order is not None and order > 0 else "#?"
        lines = [
            f"<b>QR {escape(qr_no)} - UPI ChatGPT Plus (IN)</b>",
            f"Email: {escape(full_email)}",
        ]
        if qr_expires_at is not None:
            lines.append(f"Expires: {escape(format_vn_time(qr_expires_at))} VN")
            lines.append(f"Expired: {escape(format_india_time(qr_expires_at))} IN")
        if payment_link:
            lines.append(escape(payment_link))
        if scanner_identity:
            lines.append(f"👤 Người nhận QR: {escape(scanner_identity)}")
        sent_time = sent_at if sent_at is not None else finished_at
        if sent_time is not None:
            lines.append(f"🕒 Nhận lúc: {escape(format_vn_time(sent_time))} VN")
        return "\n".join(lines)

    lines = [
        "✅ <b>iDEAL QR ready</b>",
        f"👤 Email: <code>{escape(full_email)}</code>",
    ]
    if scanner_identity:
        lines.append(f"🙋 Worker: {escape(scanner_identity)}")
    lines.append(f"🕒 Time (VN): {escape(vn_time)}")
    if payment_link:
        lines.append(
            f'🔗 <a href="{escape(payment_link, quote=True)}">Open payment link</a>'
        )
    return "\n".join(lines)


def append_plus_status(caption: str, *, plus_n: int | None = None) -> str:
    """Ghép dòng trạng thái Plus vào caption QR (editMessageCaption).

    ``plus_n`` — số thứ tự Plus trong batch của group (1-based). None → không #.
    """
    return append_plan_outcome_status(caption, "plus", plus_n=plus_n)


def append_plan_outcome_status(
    caption: str,
    outcome: str,
    *,
    plus_n: int | None = None,
) -> str:
    """Ghép dòng status Plus/expired vào caption QR.

    ``outcome``: ``"plus"`` | ``"expired"``.
    """
    base = (caption or "").rstrip()
    if outcome == "plus":
        if plus_n is not None and plus_n > 0:
            status = f"✅ Đã lên Plus (#{plus_n})"
        else:
            status = "✅ Đã lên Plus"
    elif outcome == "expired":
        status = "⌛ Hết hạn poll / chưa lên Plus"
    else:
        status = f"⚠️ Check: {outcome}"
    if status in base:
        return base
    return f"{base}\n———\n{escape(status)}" if base else escape(status)


def plus_tag_text(*, plus_n: int | None = None) -> str:
    """Text reply tag khi account verify Plus — song ngữ ngắn cho worker group."""
    if plus_n is not None and plus_n > 0:
        return f"✅ Plus active (#{plus_n})\n✅ Đã lên Plus (#{plus_n})"
    return "✅ Plus active\n✅ Đã lên Plus"


def expired_tag_text() -> str:
    """Reply khi auto-check poll hết 5 phút mà plan chưa plus."""
    return (
        "⌛ Expired / not on Plus\n"
        "⌛ Hết hạn poll / chưa lên Plus"
    )


def build_batch_tally_text(plus: int, expired: int = 0, error: int = 0) -> str:
    """1 dòng tally batch per worker chat — edit-in-place, không spam."""
    return f"📊 Batch Plus: ✅ {int(plus)}  ⌛ {int(expired)}  ❌ {int(error)}"


def build_period_close_text(
    plus: int, *, expired: int = 0, reset_at: str
) -> str:
    """Permanent receipt when an operator closes a batch period.

    Pure formatter — caller supplies `reset_at` (Vietnam-time stamp);
    this function never reads the clock.
    """
    return (
        f"📋 Chốt kỳ — Batch Plus ✅ {int(plus)} ⌛ {int(expired)} "
        f"(reset {reset_at})"
    )


def build_pull_caption(
    account_line: str,
    finished_at: float | None,
    payment_link: str | None,
    username: str | None,
    first_name: str | None,
) -> str:
    """Build HTML caption cho `sendPhoto` ở Pull_Mode (Requirement 8.2).

    Giống hoàn toàn `build_caption` (email đã mask, giờ VN, link thanh
    toán optional) nhưng chèn thêm 1 dòng định danh Telegram_Worker đang
    giữ job — ngay sau dòng email, trước dòng giờ, để người đọc thấy
    "đây là job của ai" sớm nhất khi lướt caption.

    `username`/`first_name` là SNAPSHOT lấy từ `callback_query.from` TẠI
    THỜI ĐIỂM Nhận_Job_Button được bấm (R5.6/R8.2) — hàm này KHÔNG gọi
    lại Telegram API, chỉ format dữ liệu đã có sẵn.

    Ưu tiên hiển thị `@{username}` nếu `username` non-empty (sau strip);
    fallback `first_name` nếu `username` rỗng/thiếu. Cả 2 đều rỗng/thiếu
    (trường hợp hiếm — Telegram luôn có `first_name`, nhưng vẫn xử lý an
    toàn theo Fail_Fast_Policy "không hiển thị dữ liệu gây hiểu lầm"):
    OMIT hẳn dòng định danh worker thay vì hiển thị placeholder giả tạo.

    `username`/`first_name` là dữ liệu do user Telegram tự đặt (untrusted
    input) — PHẢI escape bằng `html.escape` trước khi chèn vào caption
    HTML, tương tự cách `build_caption` escape `masked`/`vn_time`.
    """
    full_email = extract_email(account_line) or account_line.strip()
    vn_time = format_vn_time(finished_at)

    lines = [
        "✅ <b>iDEAL QR ready</b>",
        f"👤 Email: <code>{escape(full_email)}</code>",
    ]

    stripped_username = (username or "").strip()
    stripped_first_name = (first_name or "").strip()
    if stripped_username:
        identity = f"@{stripped_username}"
    elif stripped_first_name:
        identity = stripped_first_name
    else:
        identity = ""
    if identity:
        lines.append(f"🙋 Worker: {escape(identity)}")

    lines.append(f"🕒 Time (VN): {escape(vn_time)}")
    if payment_link:
        # `payment_link` là URL công khai iDEAL — escape để phòng edge case
        # URL chứa `<`/`>`/`&` (rare nhưng có thể xảy ra với query param).
        lines.append(
            f'🔗 <a href="{escape(payment_link, quote=True)}">Open payment link</a>'
        )

    return "\n".join(lines)
