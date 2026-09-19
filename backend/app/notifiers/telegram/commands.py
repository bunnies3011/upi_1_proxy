"""Command_Router — xử lý update loại `message` chứa lệnh `/start`,
`/stats`, `/reset`, `/chotky` cho Pull_Mode (design.md §3.5, Requirement 16, 17, 19).

Trách nhiệm DUY NHẤT: nhận diện lệnh qua regex + reply message tương ứng.
KHÔNG xử lý callback_query (nút bấm) — đó là việc của `pull_mode.py`
(Pull_Job_Coordinator, xem design.md §3.6) qua tiền tố `pull_job:`. Cụ thể:
    - `/start` → reply kèm Nhận_Job_Button (`callback_data="pull_job:claim"`).
    - `/stats` → đọc `JobManager.get_worker_stats_ranking()`, format bảng
      text tối đa 20 dòng (Requirement 16).
    - `/reset` → reply kèm Reset_Confirm_Button/Reset_Cancel_Button gắn
      `requester_id` = `telegram_user_id` người gửi lệnh (Requirement 17.1).
      Việc XỬ LÝ 2 nút này (reset thật/hủy) nằm ở `pull_mode.py` vì cùng
      tiền tố `pull_job:` (R18.2) — module này CHỦ ĐỘNG không biết
      `JobManager.reset_all_worker_stats` để giữ đúng ranh giới trách
      nhiệm (design.md §3.5).
    - `/chotky` → admin-only (settings `telegram.pull_mode.admin_user_ids`);
      empty list refuses everyone. Reply kèm confirm/cancel button gắn
      `requester_id` (giống `/reset`). Xử lý confirm gọi
      `reset_batch_tally_hook` nằm ở `pull_mode.py` (cùng tiền tố
      `pull_job:`). Hook optional — `None` → reply "không khả dụng".

Boundary dependency direction (Requirement 20): module này CHỈ gọi
`JobManager.get_worker_stats_ranking()` — không import `core.job_manager`
trực tiếp (chỉ dùng cho type hint qua `TYPE_CHECKING`, tương tự pattern ở
`notifier.py`).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from html import escape
from typing import TYPE_CHECKING, Any

from app.notifiers.telegram.client import TelegramBotClient

if TYPE_CHECKING:
    from app.core.job_manager import JobManager
    from app.core.settings_store import SettingsRepository

logger = logging.getLogger(__name__)

# R19.5: khớp CHÍNH XÁC "/start"/"/stats"/"/reset"/"/chotky", có thể theo
# sau bởi hậu tố "@<tên bot>" (Telegram tự thêm khi group có nhiều bot).
# Case sensitive, KHÔNG trim khoảng trắng, KHÔNG khớp biến thể có tham số
# khác (VD "/start abc" không khớp vì có ký tự sau không thuộc pattern).
_COMMAND_PATTERN = re.compile(r"^/(start|stats|reset|chotky)(@\w+)?$")

# R2.4/2.5: whitelist chat cho Start_Command — namespace mới sau khi
# tái cấu trúc `telegram.*` (Requirement 2).
_KEY_ALLOWED_CHAT_IDS = "telegram.pull_mode.allowed_chat_ids"

# Admin allowlist cho /chotky (payroll-affecting). Empty → refuse all.
_KEY_ADMIN_USER_IDS = "telegram.pull_mode.admin_user_ids"

# R16.2: giới hạn số dòng hiển thị trong bảng /stats.
_STATS_MAX_ROWS = 20

_CHOTKY_UNAVAILABLE_TEXT = "Chốt kỳ hiện không khả dụng."
_CHOTKY_REFUSE_TEXT = (
    "⛔ Chỉ admin mới chốt kỳ được — cấu hình `admin_user_ids` hoặc dùng nút web"
)
_CHOTKY_CONFIRM_PROMPT = "Chốt kỳ và reset bộ đếm Plus?"


async def handle_message(
    message: dict,
    *,
    client: TelegramBotClient,
    bot_token: str,
    job_manager: "JobManager",
    settings: "SettingsRepository",
    reset_batch_tally_hook: Callable[[], Awaitable[dict]] | None = None,
) -> None:
    """Entry point Command_Router — dispatch `/start`, `/stats`, `/reset`, `/chotky`.

    R19.6: message không có `from` (sender ẩn danh, VD channel post) bị
    bỏ qua hoàn toàn — không xác định được `telegram_user_id`.
    R19.5: `text` không khớp `_COMMAND_PATTERN` → bỏ qua, không reply,
    không log (traffic bình thường của group).
    R19.7: trích `chat_id`/`telegram_user_id`/`username`/`first_name` từ
    `message` để dùng xuyên suốt logic downstream.
    """
    if message.get("from") is None:
        return

    text = message.get("text", "")
    match = _COMMAND_PATTERN.match(text)
    if not match:
        return

    command = match.group(1)
    chat_id = str(message["chat"]["id"])
    from_user = message["from"]
    telegram_user_id = str(from_user["id"])
    # R19.7 chỉ yêu cầu trích `username`/`first_name` cho logic downstream
    # cần chúng (R15.4 — update snapshot khi tăng counter, xử lý bởi
    # `pull_mode.py` khi callback resolve outcome). Không lệnh nào trong
    # `commands.py` (/start, /stats, /reset, /chotky) cần 2 field này
    # trực tiếp — /stats đọc identity từ `telegram_worker_stats` (đã lưu
    # sẵn qua R15.4), /start, /reset và /chotky chỉ cần
    # `chat_id`/`telegram_user_id`.

    if command == "start":
        await _handle_start(
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            client=client,
            bot_token=bot_token,
            settings=settings,
        )
    elif command == "stats":
        await _handle_stats(
            chat_id=chat_id,
            client=client,
            bot_token=bot_token,
            job_manager=job_manager,
        )
    elif command == "chotky":
        # Explicit branch — never fall through into `_handle_reset`.
        await _handle_chotky(
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            client=client,
            bot_token=bot_token,
            settings=settings,
            reset_batch_tally_hook=reset_batch_tally_hook,
        )
    else:  # "reset"
        await _handle_reset(
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            client=client,
            bot_token=bot_token,
        )


async def _handle_start(
    *,
    chat_id: str,
    telegram_user_id: str,
    client: TelegramBotClient,
    bot_token: str,
    settings: "SettingsRepository",
) -> None:
    """R19.1 (Requirement 16.1 trong tasks.md dùng số cũ — thực chất là
    Requirement 5.1 của Start_Command) + R2.4/2.5: check `allowed_chat_ids`
    trước khi reply.

    Danh sách rỗng = cho phép MỌI chat (R2.4). Danh sách non-empty và
    `chat_id` không khớp → im lặng bỏ qua, KHÔNG reply, KHÔNG tiết lộ lý
    do (tránh lộ thông tin cấu hình cho chat lạ, cùng nguyên tắc với R5.2
    áp dụng cho Nhận_Job_Button).
    """
    allowed_chat_ids = await settings.get(_KEY_ALLOWED_CHAT_IDS)
    if not isinstance(allowed_chat_ids, list):
        allowed_chat_ids = []

    if allowed_chat_ids and chat_id not in allowed_chat_ids:
        return

    keyboard = {
        "inline_keyboard": [
            [{"text": "🎯 Nhận job", "callback_data": "pull_job:claim"}]
        ]
    }
    await client.send_message(
        bot_token,
        chat_id,
        "Chào! Bấm nút dưới để nhận 1 job.",
        reply_markup=keyboard,
    )


async def _handle_stats(
    *,
    chat_id: str,
    client: TelegramBotClient,
    bot_token: str,
    job_manager: "JobManager",
) -> None:
    """R16.1-R16.3: query ranking, format bảng text tối đa 20 dòng, hoặc
    reply "Chưa có dữ liệu thống kê" nếu rỗng.

    `ranking` đã được `JobManager.get_worker_stats_ranking()` sắp đúng thứ
    tự (success_count giảm dần, tie-break fail_count tăng dần, tie-break
    cuối telegram_user_id tăng dần — R16.1) — hàm này KHÔNG tự sort lại.
    """
    ranking = await job_manager.get_worker_stats_ranking()

    if not ranking:
        await client.send_message(bot_token, chat_id, "Chưa có dữ liệu thống kê")
        return

    lines = ["📊 <b>Bảng xếp hạng</b>"]
    for entry in ranking[:_STATS_MAX_ROWS]:
        identity = _resolve_stats_identity(entry)
        success_count = entry.get("success_count", 0)
        fail_count = entry.get("fail_count", 0)
        total = success_count + fail_count
        lines.append(
            f"{escape(identity)}: ✅{success_count} ❌{fail_count} "
            f"(tổng {total})"
        )

    await client.send_message(bot_token, chat_id, "\n".join(lines))


def _resolve_stats_identity(entry: dict[str, Any]) -> str:
    """R16.2: ưu tiên `@username` non-empty → `first_name` non-empty →
    `telegram_user_id`.

    `username`/`first_name` là dữ liệu Telegram profile do user tự đặt
    (untrusted input) — caller (`_handle_stats`) escape trước khi chèn
    vào message HTML, giống pattern `build_pull_caption` ở `formatter.py`.
    """
    username = entry.get("username")
    if isinstance(username, str) and username.strip():
        return f"@{username.strip()}"

    first_name = entry.get("first_name")
    if isinstance(first_name, str) and first_name.strip():
        return first_name.strip()

    return str(entry.get("telegram_user_id", ""))


def _confirm_cancel_keyboard(action: str, telegram_user_id: str) -> dict:
    """Inline keyboard with confirm/cancel bound to `requester_id`."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Xác nhận",
                    "callback_data": f"pull_job:{action}_confirm:{telegram_user_id}",
                },
                {
                    "text": "❌ Hủy",
                    "callback_data": f"pull_job:{action}_cancel:{telegram_user_id}",
                },
            ]
        ]
    }


async def _handle_reset(
    *,
    chat_id: str,
    telegram_user_id: str,
    client: TelegramBotClient,
    bot_token: str,
) -> None:
    """R17.1: reply cảnh báo kèm 2 nút xác nhận/hủy, gắn `requester_id` =
    `telegram_user_id` người gõ `/reset` vào `callback_data`.

    KHÔNG reset ngay tại đây — chỉ gửi prompt. Xử lý bấm nút (so khớp
    `requester_id`, thực thi reset thật qua `JobManager.reset_all_worker_stats`)
    nằm ở `pull_mode.py` (design.md §3.5, R18.2).
    """
    await client.send_message(
        bot_token,
        chat_id,
        "Bạn có chắc muốn reset toàn bộ điểm thống kê không?",
        reply_markup=_confirm_cancel_keyboard("reset", telegram_user_id),
    )


async def _handle_chotky(
    *,
    chat_id: str,
    telegram_user_id: str,
    client: TelegramBotClient,
    bot_token: str,
    settings: "SettingsRepository",
    reset_batch_tally_hook: Callable[[], Awaitable[dict]] | None,
) -> None:
    """Admin-only period-close for the Push-mode Plus counter.

    Authorization: `from_user_id` must be in `telegram.pull_mode.admin_user_ids`.
    Empty / unset list refuses everyone (no `allowed_chat_ids` fallback).

    Two-step safety mirrors `/reset`: this handler only posts a confirm
    keyboard bound to `requester_id`. The confirm callback (in pull_mode)
    awaits `reset_batch_tally_hook` and replies with the summary.
    """
    admin_user_ids = await settings.get(_KEY_ADMIN_USER_IDS)
    if not isinstance(admin_user_ids, list):
        admin_user_ids = []
    admin_ids = {str(uid) for uid in admin_user_ids}

    if not admin_ids or telegram_user_id not in admin_ids:
        await client.send_message(bot_token, chat_id, _CHOTKY_REFUSE_TEXT)
        return

    if reset_batch_tally_hook is None:
        await client.send_message(bot_token, chat_id, _CHOTKY_UNAVAILABLE_TEXT)
        return

    await client.send_message(
        bot_token,
        chat_id,
        _CHOTKY_CONFIRM_PROMPT,
        reply_markup=_confirm_cancel_keyboard("chotky", telegram_user_id),
    )


def format_chotky_summary(summary: dict[str, Any]) -> str:
    """Format `reset_batch_tally` return value for Telegram / API echo."""
    plus_total = int(summary.get("plus_total", 0) or 0)
    expired_total = int(summary.get("expired_total", 0) or 0)
    closed = summary.get("closed") if isinstance(summary.get("closed"), list) else []
    skipped = summary.get("skipped") if isinstance(summary.get("skipped"), list) else []

    lines = [
        "✅ Đã chốt kỳ",
        f"Plus: {plus_total} · Hết hạn: {expired_total}",
        f"Chốt: {len(closed)} chat",
    ]
    if skipped:
        lines.append(
            "Bỏ qua (gửi receipt lỗi): " + ", ".join(str(c) for c in skipped)
        )
    return "\n".join(lines)
