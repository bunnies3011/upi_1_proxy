"""Worker_Status_Card — 1 message duy nhất cập nhật realtime cho mỗi phiên
"Nhận job" (1 batch) của worker, thay vì spam nhiều message rời qua các
pha claim / xử lý / kết quả.

Model batch (thay model claim-từng-cái + auto-skip cũ):
    - Worker bấm "Nhận job" → coordinator claim tối đa N account cùng lúc
      (N = `telegram.pull_mode.max_concurrent_jobs_per_user`) rồi gọi
      `start_batch(chat_id, user_id, total, bot_token)` — gửi 1 card duy
      nhất "⚙️ Đang xử lý N job...".
    - Mỗi job trong batch settle (theo pha XỬ LÝ, chưa tính bước worker
      bấm ✅/❌ trên QR):
          * QR sinh ra   → `record_qr_ready(user_id, bot_token)` (qr += 1)
          * Lỗi hẳn      → `record_job_failed(user_id, bot_token)` (err += 1)
            (chỉ gọi khi job đã dùng HẾT lượt auto-retry ở tầng
            `JobManager._maybe_auto_retry` — mỗi lần requeue KHÔNG chạm
            card, nên "1 lần chỉ lên 1 lần").
    - Khi `qr + err == total` (cả batch đã xử lý xong pha lấy QR) →
      finalize card:
          * qr == 0  → "⚠️ Cả N job đều lỗi" + nút "Thử lại".
          * qr > 0   → "✅ Đã lấy X/N QR..." (không nút — worker xử lý QR
            bằng 2 nút ✅/❌ trên từng ảnh QR; nút "Nhận job tiếp" chỉ
            hiện trên message kết quả của QR CUỐI CÙNG được resolve).

Boundary (Requirement 20): tracker này CHỈ gọi `TelegramBotClient`
(sendMessage / editMessageText) — KHÔNG import `JobManager` hay bất kỳ
core symbol nào. Coordinator/Notifier tự inject tracker qua constructor.

In-memory only — nhất quán với `_pull_job_locks` / `RoundRobinDistributor`:
mỗi lần restart worker bấm "Nhận job" lại là có session mới. Bounded qua
`MAX_TRACKED_SESSIONS` (LRU) để không leak dict theo tổng số worker.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.notifiers.telegram.client import TelegramBotClient

logger = logging.getLogger(__name__)

#: Trần LRU dict `_sessions` — mỗi worker (`telegram_user_id`) giữ tối đa
#: 1 entry. Chọn 1000 mirror pattern `CallbackDedupeCache` (polling.py) —
#: dư sức cho quy mô nhóm worker thực tế (<100), evict entry cũ nhất khi
#: vượt để `_sessions` không phình vô hạn.
MAX_TRACKED_SESSIONS = 1000


_TRY_AGAIN_BUTTON = {
    "inline_keyboard": [
        [{"text": "🎯 Thử lại", "callback_data": "pull_job:claim"}]
    ]
}


@dataclass
class _BatchSession:
    """State per-worker cho 1 batch "Nhận job" đang chạy.

    Attributes:
        chat_id: Chat chứa status card — cần cho mọi lần editMessageText.
        message_id: ID card đã gửi (từ response `sendMessage`). `None` nếu
            `sendMessage` lần đầu fail — các bước sau fallback `sendMessage`
            mới (best-effort, không raise).
        total: Số job đã claim trong batch (1..N).
        qr_count: Số job đã sinh QR thành công.
        error_count: Số job đã lỗi hẳn (hết lượt auto-retry).
    """

    chat_id: str
    message_id: int | None
    total: int
    qr_count: int = 0
    error_count: int = 0

    @property
    def settled(self) -> int:
        return self.qr_count + self.error_count


class WorkerStatusCardTracker:
    """Quản lý status card batch per-worker, share giữa `PullJobCoordinator`
    và `PullJobNotifier`.

    Vòng đời 1 batch:
        1. `await start_batch(chat_id, user_id, total, bot_token)` — sau khi
           coordinator claim được ≥1 account. Gửi card "⚙️ Đang xử lý N job".
        2. `await record_qr_ready(user_id, bot_token)` / `record_job_failed(...)`
           — mỗi job trong batch settle pha lấy QR. Edit card cập nhật tiến
           độ, và finalize khi `settled == total`.

    Concurrency: `_sessions` được truy cập từ nhiều task async trên cùng
    event loop (polling loop khi worker bấm nút + scheduler/hook khi job
    settle). Dùng `asyncio.Lock` guard read+mutate của session dict.
    """

    def __init__(self, client: "TelegramBotClient") -> None:
        self._client = client
        self._sessions: "OrderedDict[str, _BatchSession]" = OrderedDict()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Read helper
    # ------------------------------------------------------------------
    async def has_active(self, user_id: str) -> bool:
        """True nếu worker đang có batch đang chạy (chưa finalize)."""
        async with self._lock:
            return user_id in self._sessions

    # ------------------------------------------------------------------
    # start_batch — sendMessage card đầu batch
    # ------------------------------------------------------------------
    async def start_batch(
        self, chat_id: str, user_id: str, total: int, bot_token: str
    ) -> None:
        """Bắt đầu batch mới với `total` job đã claim. Ghi đè session cũ
        nếu còn sót (worker bấm lại sau khi batch trước đã finalize).

        Best-effort: `sendMessage` lỗi CHỈ log warning, session vẫn tạo
        với `message_id=None` — bước sau fallback `sendMessage` mới.
        """
        text = self._progress_text(total, 0, 0)
        message_id: int | None = None
        try:
            result = await self._client.send_message(bot_token, chat_id, text)
            raw_id = result.get("message_id") if isinstance(result, dict) else None
            if isinstance(raw_id, int):
                message_id = raw_id
        except Exception as exc:
            # FIX 12 hardening: bot token nằm trong URL nên KHÔNG exc_info=True.
            logger.warning(
                "status_card start_batch: send_message failed "
                "user_id=%s exc_type=%s exc=%s",
                user_id,
                type(exc).__name__,
                str(exc),
            )

        async with self._lock:
            self._sessions[user_id] = _BatchSession(
                chat_id=chat_id, message_id=message_id, total=total
            )
            self._sessions.move_to_end(user_id)
            if len(self._sessions) > MAX_TRACKED_SESSIONS:
                self._sessions.popitem(last=False)

    # ------------------------------------------------------------------
    # record_qr_ready / record_job_failed — 1 job trong batch settle
    # ------------------------------------------------------------------
    async def record_qr_ready(self, user_id: str, bot_token: str) -> None:
        """1 job trong batch đã sinh QR — tăng `qr_count`, edit tiến độ,
        finalize nếu cả batch đã settle. No-op nếu không có session."""
        await self._record(user_id, bot_token, qr=True)

    async def record_job_failed(self, user_id: str, bot_token: str) -> None:
        """1 job trong batch đã lỗi hẳn (hết lượt auto-retry) — tăng
        `error_count`, edit tiến độ, finalize nếu cả batch đã settle.
        No-op nếu không có session."""
        await self._record(user_id, bot_token, qr=False)

    async def _record(self, user_id: str, bot_token: str, *, qr: bool) -> None:
        async with self._lock:
            session = self._sessions.get(user_id)
            if session is None:
                return
            if qr:
                session.qr_count += 1
            else:
                session.error_count += 1
            self._sessions.move_to_end(user_id)

            finished = session.settled >= session.total
            snapshot = _BatchSession(
                chat_id=session.chat_id,
                message_id=session.message_id,
                total=session.total,
                qr_count=session.qr_count,
                error_count=session.error_count,
            )
            if finished:
                self._sessions.pop(user_id, None)

        if not finished:
            text = self._progress_text(
                snapshot.total, snapshot.qr_count, snapshot.error_count
            )
            await self._edit_or_send(snapshot, user_id, text, bot_token)
            return

        # Batch đã xử lý xong pha lấy QR — finalize.
        if snapshot.qr_count == 0:
            text = (
                f"⚠️ <b>Cả {snapshot.total} job đều gặp lỗi hệ thống.</b>\n"
                "Vui lòng thử lại sau ít phút."
            )
            await self._edit_or_send(
                snapshot, user_id, text, bot_token, reply_markup=_TRY_AGAIN_BUTTON
            )
        else:
            text = (
                f"✅ <b>Đã lấy {snapshot.qr_count}/{snapshot.total} QR.</b>\n"
                "Bấm ✅/❌ trên từng ảnh QR để hoàn tất, xong sẽ có nút "
                "nhận job tiếp."
            )
            await self._edit_or_send(snapshot, user_id, text, bot_token)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _progress_text(total: int, qr_count: int, error_count: int) -> str:
        remaining = total - qr_count - error_count
        return (
            f"⚙️ <b>Đang xử lý {total} job...</b>\n"
            f"✅ QR: {qr_count}   ❌ Lỗi: {error_count}   ⏳ Còn: {remaining}"
        )

    async def _edit_or_send(
        self,
        session: _BatchSession,
        user_id: str,
        text: str,
        bot_token: str,
        *,
        reply_markup: dict | None = None,
    ) -> None:
        """editMessageText best-effort. Nếu `message_id=None` (send lần đầu
        fail) HOẶC editMessageText raise (message quá cũ / bị xoá), fallback
        sendMessage mới. Cả 2 nhánh swallow lỗi + log warning — trạng thái
        tracker KHÔNG rollback dù gửi Telegram thất bại.
        """
        if session.message_id is not None:
            try:
                await self._client.edit_message_text(
                    bot_token,
                    session.chat_id,
                    session.message_id,
                    text,
                    reply_markup=reply_markup,
                )
                return
            except Exception as exc:
                logger.warning(
                    "status_card edit_or_send: edit_message_text failed "
                    "user_id=%s chat_id=%s message_id=%s exc_type=%s exc=%s",
                    user_id,
                    session.chat_id,
                    session.message_id,
                    type(exc).__name__,
                    str(exc),
                )

        try:
            await self._client.send_message(
                bot_token, session.chat_id, text, reply_markup=reply_markup
            )
        except Exception as exc:
            logger.warning(
                "status_card edit_or_send: fallback send_message failed "
                "user_id=%s chat_id=%s exc_type=%s exc=%s",
                user_id,
                session.chat_id,
                type(exc).__name__,
                str(exc),
            )
