"""Pull_Job_Notifier — gửi QR Pull_Mode + hook thông báo lỗi kỹ thuật
(design.md §3.6, Requirement 1.6, 1.7, 8, 13.3, 13.6).

Tách riêng khỏi `pull_mode.py` (vốn đã ~780 dòng chỉ cho
`PullJobCoordinator`) để giữ mỗi file dưới ngưỡng dễ đọc (~400-450 dòng).

Trách nhiệm của `PullJobNotifier`:
    - `notify_pull_qr_ready(record)`: đăng ký làm `JobTerminalHook` — CHỈ
      áp dụng cho job CÓ Job_Pull_Assignment (R1.6, R1.7 — early-return
      dựa trên `record.pull_assignment_state is not None`, KHÔNG dựa vào
      `telegram.mode` live). Gửi `sendPhoto` kèm 2 nút Hoàn_Thành/Thất_Bại
      tới `pull_origin_chat_id`, sau đó khởi tạo Pull_Verify_State qua
      `job_manager.initialize_pull_verify_state` (R8.1-R8.4).
    - `notify_pull_error(record, error_code)`: đăng ký làm
      `PullErrorHook` — gửi message báo lỗi kỹ thuật ngắn gọn (chứa
      `error_code`, KHÔNG chứa secrets) kèm nút "Nhận job tiếp" tới
      `pull_origin_chat_id` (R13.3, R13.6).

Boundary dependency direction (Requirement 20): module này CHỈ gọi
`job_manager.initialize_pull_verify_state` (method public MỚI, KHÔNG
mutate trực tiếp field nào của `_JobRecord`, R20.2).

Đăng ký hook (`register_terminal_hook`/`register_pull_error_hook`) là
trách nhiệm của bootstrap wiring (task 32) — class này KHÔNG tự đăng ký
trong `__init__`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from app.notifiers.telegram.client import TelegramApiError, TelegramBotClient
from app.notifiers.telegram.formatter import build_caption, build_pull_caption
from app.notifiers.telegram.status_card import WorkerStatusCardTracker

if TYPE_CHECKING:
    from app.core.job_manager import JobManager, _JobRecord

logger = logging.getLogger(__name__)

# R8.3 — reply_markup 1 hàng, ĐÚNG 2 nút theo thứ tự: Hoàn_Thành_Button
# (trái) rồi Thất_Bại_Button (phải), callback_data đúng format
# `"pull_job:done:{job_id}"` / `"pull_job:fail:{job_id}"`.
_HOAN_THANH_LABEL = "✅ Hoàn thành"
_THAT_BAI_LABEL = "❌ Thất bại"

def _build_qr_ready_keyboard(job_id: str) -> dict:
    """Build reply_markup cho message QR Pull_Mode (R8.3)."""
    return {
        "inline_keyboard": [
            [
                {"text": _HOAN_THANH_LABEL, "callback_data": f"pull_job:done:{job_id}"},
                {"text": _THAT_BAI_LABEL, "callback_data": f"pull_job:fail:{job_id}"},
            ]
        ]
    }


class PullJobNotifier:
    """Đăng ký làm terminal hook + pull-error hook CHỈ cho job CÓ
    Job_Pull_Assignment (R1.6, R1.7, R13.3, R13.6)."""

    def __init__(
        self,
        job_manager: "JobManager",
        client: TelegramBotClient,
        bot_token_getter: Callable[[], Awaitable[str]],
        status_card: WorkerStatusCardTracker,
    ) -> None:
        self._job_manager = job_manager
        self._client = client
        self._bot_token_getter = bot_token_getter
        # Task 5 (UX refactor): share tracker với PullJobCoordinator để
        # trước khi gửi ảnh QR, edit status card sang ✅ "Đã lấy job"
        # (thay vì bỏ card lửng lơ ở pha "⚙️ Đang xử lý").
        self._status_card = status_card

    # ------------------------------------------------------------------
    # Terminal hook — Requirement 8
    # ------------------------------------------------------------------
    async def notify_pull_qr_ready(self, record: "_JobRecord") -> None:
        """Terminal hook — gửi QR Pull_Mode + 2 nút Hoàn_Thành/Thất_Bại
        tới `pull_origin_chat_id` (R8.1-R8.3), rồi khởi tạo
        Pull_Verify_State qua `job_manager.initialize_pull_verify_state`
        nếu `sendPhoto` thành công (R8.4). Log lỗi kèm context nếu
        `sendPhoto` thất bại, KHÔNG khởi tạo state (R8.5).

        R1.6/R1.7: early-return nếu `record.pull_assignment_state is
        None` — hook này CHỈ áp dụng cho job CÓ Job_Pull_Assignment,
        job Push_Mode do `TelegramNotifier.notify_qr_ready` xử lý riêng.
        """
        if record.pull_assignment_state is None:
            return

        artifact = record.artifact_path
        if not artifact:
            logger.warning(
                "pull notify skipped: artifact_path=None cho QR_READY job "
                "(job_id=%s)",
                record.job.job_id,
            )
            return

        artifact_path = Path(artifact)
        if not artifact_path.is_file():
            logger.warning(
                "pull notify skipped: file QR không tồn tại (job_id=%s path=%s)",
                record.job.job_id,
                artifact_path,
            )
            return

        try:
            photo_bytes = artifact_path.read_bytes()
        except OSError as exc:
            logger.warning(
                "pull notify skipped: đọc file QR fail (job_id=%s err=%s)",
                record.job.job_id,
                exc,
            )
            return

        job_id = record.job.job_id
        payment_method = getattr(record.job, "payment_method", None)
        if payment_method and payment_method.lower().startswith("upi"):
            caption = build_caption(
                account_line=record.job.account_line,
                finished_at=record.finished_at,
                payment_link=record.payment_link,
                payment_method=payment_method,
                order=record.order,
                qr_expires_at=record.qr_expires_at,
                chat_id=record.pull_origin_chat_id,
                sent_at=time.time(),
                username=record.pull_assigned_username,
                first_name=record.pull_assigned_first_name,
            )
        else:
            caption = build_pull_caption(
                account_line=record.job.account_line,
                finished_at=record.finished_at,
                payment_link=record.payment_link,
                username=record.pull_assigned_username,
                first_name=record.pull_assigned_first_name,
            )
        keyboard = _build_qr_ready_keyboard(job_id)

        bot_token = await self._bot_token_getter()
        origin_chat_id = record.pull_origin_chat_id

        try:
            result = await self._client.send_photo(
                bot_token,
                origin_chat_id,
                photo_bytes,
                caption,
                reply_markup=keyboard,
            )
        except TelegramApiError as exc:
            logger.error(
                "pull notify sendPhoto failed: job_id=%s telegram_user_id=%s "
                "exc_type=%s status_code=%s error_code=%s description=%s",
                job_id,
                record.pull_assigned_telegram_user_id,
                type(exc).__name__,
                exc.status_code,
                exc.error_code,
                exc.description,
            )
            return
        except Exception as exc:
            # FIX 12 (token-leak hardening): KHÔNG dùng exc_info=True cho
            # nhánh lỗi transport/generic — traceback có thể chứa request
            # URL dạng `/bot<token>/...`. Chỉ log type + message (đã được
            # `str(exc)` rút gọn, không kèm URL đầy đủ).
            logger.error(
                "pull notify sendPhoto failed: job_id=%s telegram_user_id=%s "
                "exc_type=%s exc=%s",
                job_id,
                record.pull_assigned_telegram_user_id,
                type(exc).__name__,
                str(exc),
            )
            return

        message_id = result.get("message_id") if isinstance(result, dict) else None
        if not isinstance(message_id, int):
            logger.error(
                "pull notify sendPhoto response thiếu message_id hợp lệ: "
                "job_id=%s result=%r",
                job_id,
                result,
            )
            return

        await self._job_manager.initialize_pull_verify_state(
            job_id, origin_chat_id, message_id
        )

        # Model batch (task UX refactor): ghi nhận 1 job trong batch đã
        # sinh QR — tracker tự cập nhật tiến độ card VÀ tự finalize khi cả
        # batch đã settle (qr + error == total). Đặt SAU sendPhoto +
        # initialize_pull_verify_state để card tiến độ chỉ tăng khi QR đã
        # thực sự gửi thành công tới worker (tránh đếm "đã xử lý" cho job
        # mà worker chưa từng thấy QR do sendPhoto lỗi).
        telegram_user_id = record.pull_assigned_telegram_user_id
        if telegram_user_id:
            await self._status_card.record_qr_ready(telegram_user_id, bot_token)

    # ------------------------------------------------------------------
    # (Removed) `notify_pull_error` — chuyển sang
    # `PullJobCoordinator.handle_pull_error` (task 4 UX refactor). Hook
    # cũ luôn gửi 1 message text lỗi kỹ thuật riêng + nút "Nhận job tiếp"
    # khiến chat spam khi worker gặp chuỗi lỗi hệ thống. Bootstrap giờ
    # đăng ký coordinator method (đọc status card tracker + auto-skip
    # tối đa 5 job trước khi finalize) qua `register_pull_error_hook`.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Mode-switch-notify hook — Requirement 3.2 (d)(e), 3.6
    # ------------------------------------------------------------------
    async def notify_mode_switch_cancelled(self, record: "_JobRecord") -> None:
        """Mode-switch-notify hook — gọi cho MỖI job vừa bị
        `set_operating_mode` force-fail vì đang assigned/pending lúc đổi
        Operating_Mode (R3.2 bước d, e).

        Khớp `ModeSwitchNotifyHook = Callable[["_JobRecord"],
        Awaitable[None]]` (job_manager.py) — `_invoke_mode_switch_notify_hooks`
        gọi `await hook(record)`.

        Hai bước best-effort (R3.6 — lỗi CHỈ log warning kèm `job_id` +
        `type(exc).__name__`, KHÔNG raise; boundary
        `_invoke_mode_switch_notify_hooks` không được kẹt vì lỗi Telegram
        API):
            (d) `editMessageReplyMarkup(reply_markup=None)` gỡ 2 nút
                Hoàn_Thành/Thất_Bại trên message QR (nếu message QR đã
                từng gửi → `telegram_message_chat_id`/`telegram_message_id`
                đã set).
            (e) `sendMessage` báo worker job đã bị hủy do quản trị đổi
                chế độ vận hành (nếu `pull_origin_chat_id` đã set) —
                message ngắn, KHÔNG chứa secrets.

        Token đọc live qua `self._bot_token_getter()` (có thể đổi live qua
        Settings API).
        """
        job_id = record.job.job_id
        bot_token = await self._bot_token_getter()

        # (d) Gỡ reply_markup của message QR nếu message đã từng gửi.
        message_chat_id = record.telegram_message_chat_id
        message_id = record.telegram_message_id
        if message_chat_id and message_id is not None:
            try:
                await self._client.edit_message_reply_markup(
                    bot_token,
                    message_chat_id,
                    message_id,
                    reply_markup=None,
                )
            except Exception as exc:
                logger.warning(
                    "notify_mode_switch_cancelled: editMessageReplyMarkup "
                    "failed job_id=%s exc_type=%s",
                    job_id,
                    type(exc).__name__,
                )

        # (e) Báo worker job đã bị hủy do đổi mode vận hành.
        origin_chat_id = record.pull_origin_chat_id
        if origin_chat_id:
            try:
                await self._client.send_message(
                    bot_token,
                    origin_chat_id,
                    "⚠️ Job đã bị hủy do quản trị đổi chế độ vận hành.",
                )
            except Exception as exc:
                logger.warning(
                    "notify_mode_switch_cancelled: send_message failed "
                    "job_id=%s exc_type=%s",
                    job_id,
                    type(exc).__name__,
                )
