"""Pull_Job_Coordinator — xử lý callback_data tiền tố `"pull_job:"` (design.md
§3.6, Requirement 5, 9, 10, 11, 12, 14, 17, 22.11).

Trách nhiệm của `PullJobCoordinator` trong module này (task 27+28 — KHÔNG
bao gồm `PullJobNotifier` [task 29]):
    - `claim` (`pull_job:claim`): dequeue account từ Pull_Account_Pool cho
      worker bấm nút "🎯 Nhận job"/"🎯 Nhận job tiếp" (Requirement 5, 6, 14).
    - `done:{job_id}` (`pull_job:done:{job_id}`): xử lý nút "✅ Hoàn thành" —
      verify Plus qua `check_plan_status`, tái dùng state machine
      `plus_check_state`/`plus_check_attempts` (Requirement 9, 10, 11).
    - `fail:{job_id}` (`pull_job:fail:{job_id}`): xử lý nút "❌ Thất bại" —
      kết luận thất bại ngay không cần verify (Requirement 9, 12).
    - `reset_confirm:{requester_id}`/`reset_cancel:{requester_id}`
      (`pull_job:reset_confirm:{id}`/`pull_job:reset_cancel:{id}`): xử lý
      2 nút xác nhận/hủy của prompt `/reset` (Requirement 17).
    - `chotky_confirm:{requester_id}`/`chotky_cancel:{requester_id}`:
      2 nút xác nhận/hủy của prompt `/chotky` — confirm awaits
      `reset_batch_tally_hook` (Push-mode period close) injected by
      bootstrap; cancel only removes buttons.

Boundary dependency direction (Requirement 20): module này CHỈ gọi các
method public đã liệt kê ở R20.1 của `JobManager` (`claim_pull_account`,
`resolve_pull_outcome`, `get_worker_active_pull_count`,
`record_plus_check_transition`, `check_plan_status`, `get_job`) — KHÔNG
BAO GIỜ gán trực tiếp field nào của `_JobRecord` (R20.2). Field đọc được
qua `get_job()` (R20.4) CHỈ dùng để hiển thị/định tuyến thông báo
(`pull_origin_chat_id`, `telegram_message_chat_id`, `telegram_message_id`,
`pull_assigned_telegram_user_id`, `pull_outcome`, `plus_check_state`,
`plus_check_attempts`) — KHÔNG mutate. Chốt kỳ Plus không đi qua
JobManager public API — hook optional từ notifier.

Mọi `answerCallbackQuery` VÀ mọi lệnh Telegram API best-effort sau khi
state đã chốt (`editMessageReplyMarkup`, `sendMessage`) đều được bọc
try/except log-and-continue — lỗi gọi Telegram API KHÔNG BAO GIỜ được
raise ra ngoài `handle_callback` (R9.4, R10.8, R11.5, R12.7) và KHÔNG
rollback state đã ghi.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import TYPE_CHECKING, Awaitable, Callable

from app.core.errors import JobAlreadyResolvedError, JobNotFoundError
from app.core.job_manager import PlusCheckState, PullOutcome
from app.notifiers.telegram.client import TelegramBotClient
from app.notifiers.telegram.commands import format_chotky_summary
from app.notifiers.telegram.status_card import WorkerStatusCardTracker

if TYPE_CHECKING:
    from app.core.job_manager import JobManager, _JobRecord
    from app.core.settings_store import SettingsRepository

logger = logging.getLogger(__name__)

# R2.4/2.5 — namespace mới sau khi tái cấu trúc `telegram.*` (cùng key dùng
# ở `commands.py` cho Start_Command).
_KEY_ALLOWED_CHAT_IDS = "telegram.pull_mode.allowed_chat_ids"
_KEY_MODE = "telegram.mode"
_KEY_MAX_CONCURRENT = "telegram.pull_mode.max_concurrent_jobs_per_user"

# R14.1 — nút "Nhận job tiếp" đính kèm MỌI message xác nhận kết quả (thành
# công, thất bại hết lượt, thất bại tự bấm). `callback_data` giống hệt nút
# Nhận_Job_Button gốc trên message `/start` (R14.2 — xử lý hoàn toàn giống
# nhau, không phân biệt nguồn gốc bấm).
_CLAIM_AGAIN_BUTTON = {
    "inline_keyboard": [
        [{"text": "🎯 Nhận job tiếp", "callback_data": "pull_job:claim"}]
    ]
}


class PullJobCoordinator:
    """Xử lý mọi callback_data tiền tố `"pull_job:"` (R5, R9-R14, R17.2-17.6)."""

    def __init__(
        self,
        job_manager: "JobManager",
        client: TelegramBotClient,
        settings: "SettingsRepository",
        bot_token_getter: Callable[[], Awaitable[str]],
        status_card: WorkerStatusCardTracker,
        reset_batch_tally_hook: Callable[[], Awaitable[dict]] | None = None,
    ) -> None:
        self._job_manager = job_manager
        self._client = client
        self._settings = settings
        self._bot_token_getter = bot_token_getter
        # Task 3-4 (UX refactor): status card share với PullJobNotifier —
        # coordinator ghi các pha đầu (begin_claim/processing/limit/no_jobs),
        # `handle_pull_error` (đăng ký làm pull_error hook thay cho
        # PullJobNotifier.notify_pull_error) tự đọc/ghi pha retry, notifier
        # ghi pha finalize_success. Không share JobManager state — chỉ
        # chia sẻ 1 dict message-id per worker để tránh spam sendMessage.
        self._status_card = status_card
        # Optional Push-mode period-close hook (from TelegramNotifier).
        # None → /chotky confirm replies "unavailable".
        self._reset_batch_tally_hook = reset_batch_tally_hook
        # R17.6 — dedup key = f"{chat_id}:{message_id}" của message prompt
        # `/reset` (và `/chotky`) đã được xác nhận HOẶC hủy (chỉ 1 trong 2
        # xảy ra 1 lần duy nhất cho mỗi prompt). Track riêng thay vì tái
        # dùng Callback_Dedupe_Cache (cache đó dedupe theo
        # `callback_query.id` — mỗi lần bấm nút sinh callback_query.id
        # MỚI dù cùng message, nên không phát hiện được "bấm lại nút
        # reset_confirm SAU KHI reset_cancel đã xử lý" hay ngược lại).
        #
        # Fix 10 — bounded LRU (OrderedDict) thay cho `set` vô hạn: mỗi
        # prompt `/reset` sinh 1 key mới, `set` sẽ phình vĩnh viễn theo
        # thời gian chạy. Dùng OrderedDict + evict phần tử cũ nhất khi vượt
        # `_RESET_DEDUPE_MAXSIZE`, mirror pattern `CallbackDedupeCache`
        # (polling.py). Giữ nguyên dedupe semantic: 1 prompt đã resolve
        # chặn re-process. Chỉ truy cập tuần tự từ polling loop (không
        # concurrent) nên không cần lock.
        self._resolved_reset_requests: "OrderedDict[str, None]" = OrderedDict()

    # R17.6 — trần LRU cho dedupe prompt `/reset` (mirror CallbackDedupeCache).
    _RESET_DEDUPE_MAXSIZE = 500

    def _reset_request_seen(self, dedupe_key: str) -> bool:
        """True nếu prompt `/reset` (theo `dedupe_key`) đã được xác nhận
        HOẶC hủy trước đó. Pure lookup — KHÔNG mutate thứ tự LRU."""
        return dedupe_key in self._resolved_reset_requests

    def _mark_reset_request_resolved(self, dedupe_key: str) -> None:
        """Ghi nhận prompt `/reset` đã resolve (confirm/cancel) — đẩy key
        về vị trí mới nhất, evict key cũ nhất khi vượt trần."""
        if dedupe_key in self._resolved_reset_requests:
            self._resolved_reset_requests.move_to_end(dedupe_key)
            return
        self._resolved_reset_requests[dedupe_key] = None
        if len(self._resolved_reset_requests) > self._RESET_DEDUPE_MAXSIZE:
            self._resolved_reset_requests.popitem(last=False)

    # ------------------------------------------------------------------
    # Dispatch (design.md §3.4 — Callback_Router gọi method này với NGUYÊN
    # dict `callback_query`, KHÔNG strip tiền tố trước — module này tự
    # extract `callback_query["data"]` và strip `"pull_job:"`).
    # ------------------------------------------------------------------
    async def handle_callback(self, callback_query: dict) -> None:
        """Parse `callback_query["data"]` (đã có tiền tố `"pull_job:"` do
        `Callback_Router` chỉ gọi tới đây khi khớp tiền tố này — method
        này tự strip lại tiền tố để tự đứng độc lập, không phụ thuộc
        router đã strip hay chưa) và dispatch tới sub-handler tương ứng.

        Format `data` sau khi strip tiền tố: `"claim"` | `"done:{job_id}"`
        | `"fail:{job_id}"` | `"reset_confirm:{requester_id}"` |
        `"reset_cancel:{requester_id}"` | `"chotky_confirm:{requester_id}"`
        | `"chotky_cancel:{requester_id}"`.

        MỌI nhánh đều gọi `answerCallbackQuery` best-effort trong ~3 giây
        (R5.2-5.7, R9.1-9.4, R10-R12) — từng sub-handler tự đảm bảo điều
        này, `handle_callback` chỉ đảm bảo defensive fallback cho
        `data` không nhận diện được.
        """
        data = callback_query.get("data", "")
        if not data.startswith("pull_job:"):
            # Callback_Router (task 30) CHỈ gọi tới đây khi đã khớp tiền
            # tố — nhánh này là defensive guard cho trường hợp gọi sai
            # (VD test/misuse), KHÔNG raise để không làm hỏng polling loop.
            logger.warning(
                "PullJobCoordinator.handle_callback nhận data sai tiền tố: %r",
                data,
            )
            return

        payload = data[len("pull_job:"):]
        bot_token = await self._bot_token_getter()

        if payload == "claim":
            await self._handle_claim(callback_query, bot_token)
        elif payload.startswith("done:"):
            job_id = payload[len("done:"):]
            await self._handle_done(callback_query, job_id, bot_token)
        elif payload.startswith("fail:"):
            job_id = payload[len("fail:"):]
            await self._handle_fail(callback_query, job_id, bot_token)
        elif payload.startswith("reset_confirm:"):
            requester_id = payload[len("reset_confirm:"):]
            await self._handle_reset_confirm(callback_query, requester_id, bot_token)
        elif payload.startswith("reset_cancel:"):
            requester_id = payload[len("reset_cancel:"):]
            await self._handle_reset_cancel(callback_query, requester_id, bot_token)
        elif payload.startswith("chotky_confirm:"):
            requester_id = payload[len("chotky_confirm:"):]
            await self._handle_chotky_confirm(
                callback_query, requester_id, bot_token
            )
        elif payload.startswith("chotky_cancel:"):
            requester_id = payload[len("chotky_cancel:"):]
            await self._handle_chotky_cancel(
                callback_query, requester_id, bot_token
            )
        else:
            logger.warning(
                "PullJobCoordinator.handle_callback: data không nhận diện "
                "được: %r",
                data,
            )
            await self._answer(bot_token, callback_query.get("id", ""))

    # ------------------------------------------------------------------
    # Helpers best-effort Telegram API (R9.4, R10.8, R11.5, R12.7)
    # ------------------------------------------------------------------
    async def _answer(
        self,
        bot_token: str,
        callback_query_id: str,
        *,
        text: str = "",
        show_alert: bool = False,
    ) -> None:
        """`answerCallbackQuery` best-effort — lỗi CHỈ log warning, KHÔNG
        raise (R9.4 generalize cho MỌI answerCallbackQuery trong module)."""
        try:
            await self._client.answer_callback_query(
                bot_token, callback_query_id, text=text, show_alert=show_alert
            )
        except Exception as exc:
            # Fix C — KHÔNG dùng exc_info=True (traceback có thể chứa URL
            # kèm bot token). Chỉ log loại + message (nhất quán FIX 12).
            logger.warning(
                "answer_callback_query failed: callback_query_id=%s exc_type=%s exc=%s",
                callback_query_id,
                type(exc).__name__,
                str(exc),
            )

    async def _remove_buttons(
        self, bot_token: str, chat_id: str, message_id: int
    ) -> None:
        """`editMessageReplyMarkup(reply_markup=None)` best-effort — lỗi CHỈ
        log warning, KHÔNG raise, KHÔNG rollback state đã chốt trước đó
        (R10.8, R11.5, R12.7)."""
        try:
            await self._client.edit_message_reply_markup(
                bot_token, chat_id, message_id, reply_markup=None
            )
        except Exception as exc:
            # Fix C — bỏ exc_info=True (tránh token trong URL lộ qua traceback).
            logger.warning(
                "edit_message_reply_markup failed: chat_id=%s message_id=%s exc_type=%s exc=%s",
                chat_id,
                message_id,
                type(exc).__name__,
                str(exc),
            )

    async def _send_result_message(
        self,
        bot_token: str,
        chat_id: str,
        text: str,
        *,
        telegram_user_id: str | None = None,
    ) -> None:
        """`sendMessage` báo kết quả 1 job trong batch, kèm nút "Nhận job
        tiếp" (R14.1) CHỈ KHI worker đã xử lý hết batch đang giữ.

        Model batch: mỗi QR trong batch có nút Hoàn_Thành/Thất_Bại riêng
        — nếu MỌI message kết quả đều đính nút "Nhận job tiếp", worker xử
        lý xong QR đầu tiên trong batch N job đã thấy nút này ngay dù còn
        N-1 QR khác chưa xử lý (bấm vào sẽ bị `claim_pull_batch` chặn với
        status "holding", không hỏng dữ liệu nhưng gây rối UX — đúng loại
        spam message mà batch model muốn tránh). Chỉ đính nút khi
        `get_worker_active_pull_count(telegram_user_id) == 0` NGAY SAU
        khi outcome job này đã chốt — nghĩa là đây là job CUỐI CÙNG còn
        PENDING trong batch vừa được resolve.

        `telegram_user_id=None` (fallback an toàn, không nên xảy ra vì
        job Pull_Mode luôn có `pull_assigned_telegram_user_id` sau khi
        `resolve_pull_outcome` — giữ optional để caller không bắt buộc
        phải có, KHÔNG đính nút trong trường hợp này để tránh sai lệch).
        """
        include_claim_button = (
            telegram_user_id is not None
            and self._job_manager.get_worker_active_pull_count(telegram_user_id) == 0
        )
        reply_markup = _CLAIM_AGAIN_BUTTON if include_claim_button else None
        try:
            await self._client.send_message(
                bot_token, chat_id, text, reply_markup=reply_markup
            )
        except Exception as exc:
            # Fix C — bỏ exc_info=True (tránh token trong URL lộ qua traceback).
            logger.warning(
                "send_message failed: chat_id=%s exc_type=%s exc=%s",
                chat_id,
                type(exc).__name__,
                str(exc),
            )

    # ------------------------------------------------------------------
    # "claim" — Requirement 5, 6, 14
    # ------------------------------------------------------------------
    async def _handle_claim(self, callback_query: dict, bot_token: str) -> None:
        """Xử lý `pull_job:claim` — dùng chung cho Nhận_Job_Button gốc
        (message `/start`) VÀ nút "Nhận job tiếp" trên message kết quả
        (R14.2 — hoàn toàn giống nhau).

        Thứ tự check (R5.2-5.6): chat được phép -> mode đúng "pull" ->
        `claim_pull_account` (chính method này atomic hoá check limit +
        pool rỗng dưới lock, R5.8) -> phân biệt lý do `None` -> answer.
        """
        callback_id = callback_query.get("id", "")
        # Chat của message chứa nút — KHÁC `callback_query["from"]` (là
        # USER đã bấm). `callback_query["message"]["chat"]["id"]` là chat
        # nơi nút đang hiển thị (R5.2 kiểm tra live tại thời điểm bấm).
        #
        # Fix 4 — Telegram OMIT `callback_query["message"]` với message
        # cũ hơn ~48h (hoặc inline message). Truy cập trực tiếp
        # ["message"]["chat"]["id"] sẽ raise KeyError, propagate tới
        # polling loop (bị swallow) khiến KHÔNG có answerCallbackQuery nào
        # được gửi và nút quay vô hạn. Extract an toàn qua `.get()`, nếu
        # thiếu thì báo nút hết hạn và return (KHÔNG raise).
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        raw_chat_id = chat.get("id")
        if raw_chat_id is None:
            await self._answer(
                bot_token,
                callback_id,
                text="Nút đã hết hạn, gõ /start để lấy nút mới.",
                show_alert=True,
            )
            return
        chat_id = str(raw_chat_id)
        from_user = callback_query.get("from", {})
        telegram_user_id = str(from_user.get("id"))
        username = from_user.get("username")
        first_name = from_user.get("first_name")

        allowed_chat_ids = await self._settings.get(_KEY_ALLOWED_CHAT_IDS)
        if not isinstance(allowed_chat_ids, list):
            allowed_chat_ids = []
        if allowed_chat_ids and chat_id not in allowed_chat_ids:
            # R5.2: im lặng hoàn toàn — KHÔNG tiết lộ lý do, KHÔNG gọi
            # claim_pull_account.
            await self._answer(bot_token, callback_id, text="", show_alert=False)
            return

        mode = await self._settings.get(_KEY_MODE)
        if mode != "pull":
            # R5.3.
            await self._answer(
                bot_token,
                callback_id,
                text="Tool hiện không ở chế độ nhận job",
                show_alert=True,
            )
            return

        # Model batch (thay claim-từng-cái cũ): claim tối đa N account
        # (N = `_KEY_MAX_CONCURRENT`) cùng lúc trong 1 lần bấm. Coordinator
        # KHÔNG cần tự tính limit trước — `claim_pull_batch` tự đọc live +
        # tự gate "còn batch cũ chưa xử lý hết" atomic dưới lock.
        status, job_ids = await self._job_manager.claim_pull_batch(
            telegram_user_id, chat_id, username, first_name
        )

        if status == "holding":
            # Worker còn job batch trước chưa xử lý hết (còn job
            # ASSIGNED + PENDING) — từ chối claim mới, KHÔNG đụng session
            # card cũ (vẫn đang hiển thị tiến độ batch cũ).
            await self._answer(
                bot_token,
                callback_id,
                text=(
                    "Bạn còn job chưa xử lý hết, hãy hoàn tất trước khi "
                    "nhận batch mới."
                ),
                show_alert=True,
            )
            return

        if status == "empty":
            # Pool rỗng hoặc chưa cấu hình max_concurrent hợp lệ.
            await self._answer(
                bot_token,
                callback_id,
                text="Hiện không có job nào khả dụng, thử lại sau.",
                show_alert=True,
            )
            return

        # status == "claimed" — đã claim ≥1 account (có thể < N nếu pool
        # không đủ). Gửi status card batch NGAY (R5.7 tương đương cho
        # model batch).
        await self._status_card.start_batch(
            chat_id, telegram_user_id, len(job_ids), bot_token
        )
        await self._answer(
            bot_token,
            callback_id,
            text=f"Đã nhận {len(job_ids)} job, đang xử lý...",
            show_alert=False,
        )

    # ------------------------------------------------------------------
    # Pull-error hook (task 4 UX refactor) — thay
    # `PullJobNotifier.notify_pull_error` cũ. Đăng ký qua
    # `job_manager.register_pull_error_hook` trong bootstrap.
    # ------------------------------------------------------------------
    async def handle_pull_error(
        self, record: "_JobRecord", error_code: str | None
    ) -> None:
        """Được `JobManager._invoke_pull_error_hooks` gọi khi 1 job trong
        batch Pull_Mode vừa chuyển ERROR + `resolve_pull_error` đã chốt
        `pull_outcome = FAIL` (nghĩa là `JobManager._maybe_auto_retry` đã
        thử HẾT lượt retry cho account đó — retry trong-batch là account
        lỗi thì requeue chạy lại NGAY tại tầng `JobManager`, hook này CHỈ
        được gọi khi account đã fail persistent, KHÔNG còn lượt retry).

        Model batch: mỗi job trong batch KHÔNG tự claim account khác khi
        lỗi — chỉ ghi nhận 1 job đã "lỗi hẳn" vào status card
        (`record_job_failed`), tracker tự tính batch đã settled đủ
        (`qr_count + error_count == total`) hay chưa và tự finalize.

        Log `error_code` với `job_id` + `telegram_user_id` để admin trace
        account nào bị lỗi persistent (VD `approve_blocked` sau khi đã
        hết lượt retry — tín hiệu account/proxy có vấn đề thật).
        """
        job_id = record.job.job_id
        telegram_user_id = record.pull_assigned_telegram_user_id

        logger.info(
            "handle_pull_error: job_id=%s telegram_user_id=%s "
            "error_code=%s",
            job_id,
            telegram_user_id,
            error_code,
        )

        if not telegram_user_id:
            # Job KHÔNG gắn với worker Pull_Mode (VD Push_Mode job chạy
            # xong với ERROR bị mislabel, hoặc record hỏng) — bỏ qua.
            logger.warning(
                "handle_pull_error skipped: thiếu telegram_user_id (job_id=%s)",
                job_id,
            )
            return

        bot_token = await self._bot_token_getter()
        await self._status_card.record_job_failed(telegram_user_id, bot_token)

    # ------------------------------------------------------------------
    # "done:{job_id}" — Requirement 9, 10, 11
    # ------------------------------------------------------------------
    async def _handle_done(
        self, callback_query: dict, job_id: str, bot_token: str
    ) -> None:
        callback_id = callback_query["id"]

        # R9.1: check job_id tồn tại TRƯỚC ownership.
        record = self._job_manager.get_job(job_id)
        if record is None:
            await self._answer(
                bot_token, callback_id, text="Job không còn tồn tại", show_alert=True
            )
            return

        # R9.2: ownership.
        from_user_id = str(callback_query.get("from", {}).get("id"))
        if (
            record.pull_assigned_telegram_user_id is None
            or from_user_id != record.pull_assigned_telegram_user_id
        ):
            await self._answer(
                bot_token, callback_id, text="Đây không phải job của bạn", show_alert=True
            )
            return

        # R10.6: job đã terminal (verified/exhausted_2/failed_marked đều
        # kéo theo pull_outcome khác PENDING).
        if record.pull_outcome != PullOutcome.PENDING:
            await self._answer(
                bot_token, callback_id, text="Job này đã có kết quả", show_alert=True
            )
            return

        # R10.5: đang checking từ 1 lượt bấm Hoàn_Thành trước đó — KHÔNG
        # gọi check_plan_status lần 2.
        if record.plus_check_state == PlusCheckState.CHECKING:
            await self._answer(
                bot_token,
                callback_id,
                text="Đang kiểm tra, đợi kết quả...",
                show_alert=False,
            )
            return

        # R10.1: armed -> checking, tăng attempts, NGAY TRƯỚC KHI gọi
        # check_plan_status. Nếu record.plus_check_state khác "armed" (lẽ
        # ra không thể xảy ra sau 2 check trên, nhưng để job_manager tự
        # validate CAS-style dưới lock per-job — R22.9), transition tự
        # trả False.
        transitioned = await self._job_manager.record_plus_check_transition(
            job_id,
            PlusCheckState.ARMED.value,
            PlusCheckState.CHECKING.value,
            record.plus_check_attempts,
        )
        if not transitioned:
            # Race — 1 callback "done" khác đã giành lock trước và đổi
            # state (R22.9, Property 9). Dừng ở đây, KHÔNG gọi
            # check_plan_status.
            await self._answer(
                bot_token, callback_id, text="Đang xử lý, thử lại sau.", show_alert=False
            )
            return

        # Re-fetch để lấy plus_check_attempts VỪA được tăng bởi transition
        # (0->1 lần đầu, 1->2 lần thứ 2) — dùng giá trị thật dưới lock,
        # KHÔNG tự tính +1 ở tầng coordinator (tránh lệch với job_manager).
        updated_record = self._job_manager.get_job(job_id)
        if updated_record is None:
            await self._answer(
                bot_token, callback_id, text="Job không còn tồn tại", show_alert=True
            )
            return
        current_attempts = updated_record.plus_check_attempts

        # Fix 5 — early ack: `check_plan_status` là call mạng/provider có
        # thể mất vài giây; nếu chỉ answerCallbackQuery SAU khi nó xong,
        # nút Telegram treo và callback có thể hết hạn (~các giây ngắn),
        # khiến answer cuối cùng bị Telegram từ chối. Gửi ack nhẹ NGAY khi
        # đã chốt transition armed->checking (state đã an toàn dưới lock)
        # và TRƯỚC khi gọi check_plan_status. Các answerCallbackQuery về
        # sau (_apply_done_*/_recover_*) trở thành answer THỨ HAI cho cùng
        # callback — Telegram bỏ qua và `_answer` đã swallow lỗi, nên chúng
        # là best-effort no-op. Để KHÔNG mất phản hồi retry/exhausted/
        # verified cho user, mỗi nhánh kết luận đều gửi kèm sendMessage vào
        # pull_origin_chat_id (verified/exhausted đã có sẵn; _apply_done_retry
        # được bổ sung ở Fix 5). KHÔNG đặt ack này TRƯỚC các guard ownership/
        # terminal/checking ở trên — các guard đó cần alert riêng của mình.
        await self._answer(
            bot_token, callback_id, text="Đang kiểm tra...", show_alert=False
        )

        try:
            result = await self._job_manager.check_plan_status(job_id)
        except Exception as exc:
            # R10.7: swallow, log, tự phục hồi state (KHÔNG kẹt "checking").
            logger.warning(
                "check_plan_status raised: job_id=%s exc_type=%s",
                job_id,
                type(exc).__name__,
                exc_info=True,
            )
            await self._recover_done_from_exception(
                job_id, current_attempts, bot_token, callback_id
            )
            return

        plan = result.get("plan") if isinstance(result, dict) else None

        if plan == "plus":
            await self._apply_done_success(job_id, current_attempts, bot_token, callback_id, updated_record)
        elif current_attempts == 1:
            await self._apply_done_retry(job_id, bot_token, callback_id, updated_record)
        elif current_attempts == 2:
            await self._apply_done_exhausted(job_id, bot_token, callback_id, updated_record)
        else:
            # Không nên xảy ra — whitelist chỉ cho phép armed->checking từ
            # attempts hiện tại 0 hoặc 1, tức current_attempts sau tăng
            # luôn thuộc {1, 2}. Defensive fallback: log + ack chung,
            # KHÔNG mutate gì thêm để tránh side-effect sai.
            logger.warning(
                "handle_done: current_attempts bất thường job_id=%s attempts=%s",
                job_id,
                current_attempts,
            )
            await self._answer(
                bot_token, callback_id, text="Có lỗi xử lý, thử lại sau.", show_alert=True
            )

    async def _recover_done_from_exception(
        self,
        job_id: str,
        current_attempts: int,
        bot_token: str,
        callback_id: str,
    ) -> None:
        """R10.7 — swallow exception `check_plan_status`, chuyển state về
        `"armed"` nếu `attempts < 2` (tức đúng bằng 1, vì whitelist chỉ
        cho phép attempts thuộc {1, 2} tại điểm này), hoặc thực hiện đúng
        side-effect kết luận THẤT BẠI của R11.2 nếu `attempts == 2` — job
        KHÔNG bị kẹt vĩnh viễn ở `"checking"`.
        """
        record = self._job_manager.get_job(job_id)
        if record is None:
            await self._answer(
                bot_token, callback_id, text="Job không còn tồn tại", show_alert=True
            )
            return

        if current_attempts == 1:
            await self._job_manager.record_plus_check_transition(
                job_id,
                PlusCheckState.CHECKING.value,
                PlusCheckState.ARMED.value,
                1,
            )
            # Fix A — early ack "Đang kiểm tra..." (Fix 5) đã answer callback
            # TRƯỚC khi gọi check_plan_status, nên answerCallbackQuery alert
            # dưới đây là no-op. Gửi thêm 1 sendMessage best-effort vào origin
            # chat để worker nhận tín hiệu thử lại (mirror _apply_done_retry).
            # KHÔNG kèm nút "Nhận job tiếp" — job CHƯA terminal, worker retry
            # bằng chính 2 nút trên message QR (state đã về armed).
            origin_chat_id = record.pull_origin_chat_id
            if origin_chat_id:
                await self._send_plain_message(
                    bot_token,
                    origin_chat_id,
                    "⚠️ Có lỗi khi kiểm tra, hãy thử lại.",
                )
            await self._answer(
                bot_token,
                callback_id,
                text="Có lỗi khi kiểm tra, vui lòng thử lại.",
                show_alert=True,
            )
            return

        # current_attempts == 2 (hoặc giá trị bất thường khác) — áp dụng
        # đúng side-effect R11.2 để tránh kẹt vĩnh viễn.
        await self._apply_done_exhausted(job_id, bot_token, callback_id, record)

    async def _apply_done_success(
        self,
        job_id: str,
        current_attempts: int,
        bot_token: str,
        callback_id: str,
        record: "_JobRecord",
    ) -> None:
        """R10.2 — `plan == "plus"`: checking->verified, CHỈ áp dụng
        side-effect (resolve SUCCESS + gỡ nút + gửi message) NẾU transition
        trả `True`."""
        transitioned = await self._job_manager.record_plus_check_transition(
            job_id,
            PlusCheckState.CHECKING.value,
            PlusCheckState.VERIFIED.value,
            current_attempts,
        )
        if not transitioned:
            # Race — VD Thất_Bại_Button đã override giữa lúc check_plan_status
            # đang chạy (R12.6 — quyết định của failed_marked có hiệu lực
            # cuối cùng, kết quả check muộn bị bỏ qua hoàn toàn).
            await self._answer(
                bot_token, callback_id, text="Job này đã có kết quả", show_alert=True
            )
            return

        try:
            await self._job_manager.resolve_pull_outcome(job_id, PullOutcome.SUCCESS)
        except (JobAlreadyResolvedError, JobNotFoundError) as exc:
            # Fix C — bỏ exc_info=True cho nhất quán (không thêm giá trị; đây
            # là exception nội bộ, không có URL/token).
            logger.warning(
                "resolve_pull_outcome race sau khi verified: job_id=%s exc_type=%s",
                job_id,
                type(exc).__name__,
            )
            await self._answer(
                bot_token, callback_id, text="Job này đã có kết quả", show_alert=True
            )
            return

        chat_id = record.telegram_message_chat_id
        message_id = record.telegram_message_id
        if chat_id and message_id:
            await self._remove_buttons(bot_token, chat_id, message_id)

        origin_chat_id = record.pull_origin_chat_id
        if origin_chat_id:
            await self._send_result_message(
                bot_token,
                origin_chat_id,
                "✅ Đã xác nhận lên Plus thành công! Job hoàn tất.",
                telegram_user_id=record.pull_assigned_telegram_user_id,
            )

        await self._answer(
            bot_token, callback_id, text="Đã xác nhận thành công!", show_alert=False
        )

    async def _apply_done_retry(
        self,
        job_id: str,
        bot_token: str,
        callback_id: str,
        record: "_JobRecord",
    ) -> None:
        """R10.3 — `plan != "plus"` VÀ `attempts == 1`: checking->armed,
        báo thử lại, giữ nguyên 2 nút trên message QR."""
        transitioned = await self._job_manager.record_plus_check_transition(
            job_id,
            PlusCheckState.CHECKING.value,
            PlusCheckState.ARMED.value,
            1,
        )
        if not transitioned:
            await self._answer(
                bot_token, callback_id, text="Job này đã có kết quả", show_alert=True
            )
            return

        # Fix 5 — do đã gửi early ack "Đang kiểm tra..." trước check_plan_status,
        # answerCallbackQuery alert dưới đây có thể KHÔNG hiển thị (callback đã
        # được answer). Gửi thêm 1 sendMessage best-effort vào origin chat để
        # phản hồi "thử lại" không bị mất. KHÔNG kèm nút "Nhận job tiếp" (job
        # CHƯA terminal — worker retry bằng chính 2 nút trên message QR).
        origin_chat_id = record.pull_origin_chat_id
        if origin_chat_id:
            await self._send_plain_message(
                bot_token,
                origin_chat_id,
                "⚠️ Chưa lên Plus, hãy thử lại.",
            )

        await self._answer(
            bot_token, callback_id, text="Chưa lên Plus, thử lại", show_alert=True
        )

    async def _apply_done_exhausted(
        self,
        job_id: str,
        bot_token: str,
        callback_id: str,
        record: "_JobRecord",
    ) -> None:
        """R11.1-R11.3 — `plan != "plus"` VÀ `attempts == 2`: checking->
        exhausted_2, CHỈ áp dụng side-effect (fail_count+1 qua
        `resolve_pull_outcome`, gỡ nút, gửi message) NẾU transition trả
        `True`. Cũng được gọi lại từ `_recover_done_from_exception` (R10.7)
        khi `check_plan_status` raise ở lượt thử thứ 2."""
        transitioned = await self._job_manager.record_plus_check_transition(
            job_id,
            PlusCheckState.CHECKING.value,
            PlusCheckState.EXHAUSTED_2.value,
            2,
        )
        if not transitioned:
            await self._answer(
                bot_token, callback_id, text="Job này đã có kết quả", show_alert=True
            )
            return

        try:
            await self._job_manager.resolve_pull_outcome(job_id, PullOutcome.FAIL)
        except (JobAlreadyResolvedError, JobNotFoundError) as exc:
            # Fix C — bỏ exc_info=True cho nhất quán (exception nội bộ, không URL).
            logger.warning(
                "resolve_pull_outcome race sau khi exhausted_2: job_id=%s exc_type=%s",
                job_id,
                type(exc).__name__,
            )
            await self._answer(
                bot_token, callback_id, text="Job này đã có kết quả", show_alert=True
            )
            return

        chat_id = record.telegram_message_chat_id
        message_id = record.telegram_message_id
        if chat_id and message_id:
            await self._remove_buttons(bot_token, chat_id, message_id)

        origin_chat_id = record.pull_origin_chat_id
        if origin_chat_id:
            await self._send_result_message(
                bot_token,
                origin_chat_id,
                "❌ Đã thử 2 lần, tính là thất bại",
                telegram_user_id=record.pull_assigned_telegram_user_id,
            )

        await self._answer(
            bot_token, callback_id, text="Đã ghi nhận thất bại.", show_alert=False
        )

    # ------------------------------------------------------------------
    # "fail:{job_id}" — Requirement 9, 12
    # ------------------------------------------------------------------
    async def _handle_fail(
        self, callback_query: dict, job_id: str, bot_token: str
    ) -> None:
        """R12.1-R12.7 — override kết quả bất kể đang `armed` hay
        `checking` (R12.4), KHÔNG bao giờ gọi `check_plan_status` (R12.2).
        """
        callback_id = callback_query["id"]

        # R9.1: check job_id tồn tại TRƯỚC ownership.
        record = self._job_manager.get_job(job_id)
        if record is None:
            await self._answer(
                bot_token, callback_id, text="Job không còn tồn tại", show_alert=True
            )
            return

        # R9.2: ownership.
        from_user_id = str(callback_query.get("from", {}).get("id"))
        if (
            record.pull_assigned_telegram_user_id is None
            or from_user_id != record.pull_assigned_telegram_user_id
        ):
            await self._answer(
                bot_token, callback_id, text="Đây không phải job của bạn", show_alert=True
            )
            return

        # R12.5: đã terminal từ trước (phát hiện TRƯỚC khi gọi transition)
        # -> báo ngay, KHÔNG thực hiện lại bất kỳ bước nào.
        if record.pull_outcome != PullOutcome.PENDING:
            await self._answer(
                bot_token, callback_id, text="Job này đã có kết quả", show_alert=True
            )
            return

        # R12.1/R12.4: chuyển từ state HIỆN TẠI ("armed" hoặc "checking") ->
        # "failed_marked" — Thất_Bại_Button có quyền override kết quả đang
        # chờ check.
        current_state = record.plus_check_state.value
        transitioned = await self._job_manager.record_plus_check_transition(
            job_id,
            current_state,
            PlusCheckState.FAILED_MARKED.value,
            record.plus_check_attempts,
        )
        if not transitioned:
            # Race — job đã bị resolve bởi đường khác (VD check_plan_status
            # của 1 lượt Hoàn_Thành trước đã kịp verified/exhausted_2) giữa
            # lúc đọc state ở trên và lúc giành lock ở đây — báo "đã có kết
            # quả" thay vì áp dụng lại (đúng theo task text).
            await self._answer(
                bot_token, callback_id, text="Job này đã có kết quả", show_alert=True
            )
            return

        try:
            await self._job_manager.resolve_pull_outcome(job_id, PullOutcome.FAIL)
        except (JobAlreadyResolvedError, JobNotFoundError) as exc:
            # Fix C — bỏ exc_info=True cho nhất quán (exception nội bộ, không URL).
            logger.warning(
                "resolve_pull_outcome race sau khi failed_marked: job_id=%s exc_type=%s",
                job_id,
                type(exc).__name__,
            )
            await self._answer(
                bot_token, callback_id, text="Job này đã có kết quả", show_alert=True
            )
            return

        chat_id = record.telegram_message_chat_id
        message_id = record.telegram_message_id
        if chat_id and message_id:
            await self._remove_buttons(bot_token, chat_id, message_id)

        origin_chat_id = record.pull_origin_chat_id
        if origin_chat_id:
            await self._send_result_message(
                bot_token,
                origin_chat_id,
                "❌ Đã đánh dấu thất bại",
                telegram_user_id=record.pull_assigned_telegram_user_id,
            )

        await self._answer(
            bot_token, callback_id, text="Đã ghi nhận thất bại.", show_alert=False
        )

    # ------------------------------------------------------------------
    # "reset_confirm:{requester_id}" / "reset_cancel:{requester_id}" —
    # Requirement 17
    # ------------------------------------------------------------------
    async def _send_plain_message(
        self, bot_token: str, chat_id: str, text: str
    ) -> None:
        """`sendMessage` KHÔNG kèm nút nào (khác `_send_result_message` —
        message reply reset / thông báo retry không có nút "Nhận job tiếp")
        best-effort — lỗi CHỈ log warning, KHÔNG raise, KHÔNG rollback state
        đã chốt trước đó (nhất quán với `_remove_buttons`/
        `_send_result_message`). Dùng cho reply xác nhận/hủy `/reset` (R17)
        VÀ thông báo "thử lại" của `_apply_done_retry` (Fix 5)."""
        try:
            await self._client.send_message(bot_token, chat_id, text)
        except Exception as exc:
            # Fix C — bỏ exc_info=True (tránh token trong URL lộ qua traceback).
            logger.warning(
                "send_message (plain) failed: chat_id=%s exc_type=%s exc=%s",
                chat_id,
                type(exc).__name__,
                str(exc),
            )

    async def _handle_reset_confirm(
        self, callback_query: dict, requester_id: str, bot_token: str
    ) -> None:
        """R17.2, R17.3, R17.6 — bấm nút Reset_Confirm_Button trên message
        prompt do `_handle_reset` (`commands.py`) gửi khi worker gõ `/reset`.

        Thứ tự check: đã xử lý trước đó (R17.6) -> đúng người gõ `/reset`
        (R17.2/R17.3) -> `reset_all_worker_stats()` -> gỡ 2 nút -> reply
        xác nhận.

        `reset_all_worker_stats()` KHÔNG được bọc try/except ở đây — theo
        đúng thiết kế fail-fast của chính method đó (job_manager.py): đây
        là hành động do user chủ động chờ kết quả, lỗi DB write cần lộ ra
        ngay (qua boundary swallow chung ở tầng polling loop) thay vì âm
        thầm báo "đã reset" trong khi thực tế chưa ghi được.
        """
        callback_id = callback_query.get("id", "")
        # Message chứa 2 nút Xác_Nhận/Hủy — CHÍNH message này cần được gỡ
        # nút sau khi xử lý, lấy từ callback_query["message"] — không cần
        # tra cứu qua JobManager vì prompt `/reset` không gắn với
        # `_JobRecord` nào.
        #
        # Fix 4 — `message` có thể bị Telegram omit (prompt cũ >48h). Truy
        # cập trực tiếp sẽ raise KeyError → swallow ở polling boundary →
        # nút quay vô hạn. Extract an toàn, thiếu thì báo hết hạn + return.
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        raw_chat_id = chat.get("id")
        message_id = message.get("message_id")
        if raw_chat_id is None or message_id is None:
            await self._answer(
                bot_token,
                callback_id,
                text="Nút đã hết hạn, gõ /start để lấy nút mới.",
                show_alert=True,
            )
            return
        chat_id = str(raw_chat_id)
        from_user_id = str(callback_query.get("from", {}).get("id"))

        # R17.6 — dedup theo message prompt (1 prompt CHỈ được xử lý đúng
        # 1 lần, bất kể xác nhận hay hủy).
        dedupe_key = f"{chat_id}:{message_id}"
        if self._reset_request_seen(dedupe_key):
            await self._answer(
                bot_token,
                callback_id,
                text="Yêu cầu reset này đã được xử lý",
                show_alert=True,
            )
            return

        # R17.3 — sai người bấm: KHÔNG mark resolved (để đúng người vẫn
        # bấm được sau đó), KHÔNG reset, KHÔNG gỡ nút.
        if from_user_id != requester_id:
            await self._answer(
                bot_token,
                callback_id,
                text="Chỉ người gõ /reset mới xác nhận được",
                show_alert=True,
            )
            return

        # R17.2 — đúng người: mark resolved TRƯỚC KHI gọi reset (chặn 2
        # lượt bấm đúng người gần như đồng thời cùng thực hiện reset 2
        # lần) rồi mới thực thi side-effect thật.
        self._mark_reset_request_resolved(dedupe_key)

        await self._job_manager.reset_all_worker_stats()

        await self._remove_buttons(bot_token, chat_id, message_id)
        await self._send_plain_message(
            bot_token, chat_id, "✅ Đã reset toàn bộ thống kê"
        )
        await self._answer(bot_token, callback_id, text="", show_alert=False)

    async def _handle_reset_cancel(
        self, callback_query: dict, requester_id: str, bot_token: str
    ) -> None:
        """R17.4, R17.5, R17.6 — bấm nút Reset_Cancel_Button trên cùng
        message prompt của `_handle_reset_confirm`. Giống hệt luồng check
        dedup/ownership của confirm, KHÁC ở chỗ KHÔNG gọi
        `reset_all_worker_stats()` (R17.4 — KHÔNG thay đổi
        `telegram_worker_stats`).
        """
        callback_id = callback_query.get("id", "")
        # Fix 4 — extract `message` an toàn (Telegram có thể omit với prompt
        # cũ >48h); thiếu chat/message_id thì báo hết hạn + return, KHÔNG raise.
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        raw_chat_id = chat.get("id")
        message_id = message.get("message_id")
        if raw_chat_id is None or message_id is None:
            await self._answer(
                bot_token,
                callback_id,
                text="Nút đã hết hạn, gõ /start để lấy nút mới.",
                show_alert=True,
            )
            return
        chat_id = str(raw_chat_id)
        from_user_id = str(callback_query.get("from", {}).get("id"))

        dedupe_key = f"{chat_id}:{message_id}"
        if self._reset_request_seen(dedupe_key):
            await self._answer(
                bot_token,
                callback_id,
                text="Yêu cầu reset này đã được xử lý",
                show_alert=True,
            )
            return

        # R17.5 — sai người bấm: KHÔNG mark resolved, KHÔNG gỡ nút.
        if from_user_id != requester_id:
            await self._answer(
                bot_token,
                callback_id,
                text="Chỉ người gõ /reset mới hủy được",
                show_alert=True,
            )
            return

        # R17.4 — đúng người: mark resolved, gỡ nút, reply hủy. KHÔNG gọi
        # reset_all_worker_stats.
        self._mark_reset_request_resolved(dedupe_key)

        await self._remove_buttons(bot_token, chat_id, message_id)
        await self._send_plain_message(bot_token, chat_id, "Đã hủy reset")
        await self._answer(bot_token, callback_id, text="", show_alert=False)

    # ------------------------------------------------------------------
    # "chotky_confirm:{requester_id}" / "chotky_cancel:{requester_id}"
    # ------------------------------------------------------------------
    async def _handle_chotky_confirm(
        self, callback_query: dict, requester_id: str, bot_token: str
    ) -> None:
        """Confirm `/chotky`: requester-bound, then await period-close hook."""
        callback_id = callback_query.get("id", "")
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        raw_chat_id = chat.get("id")
        message_id = message.get("message_id")
        if raw_chat_id is None or message_id is None:
            await self._answer(
                bot_token,
                callback_id,
                text="Nút đã hết hạn, gõ /chotky để lấy nút mới.",
                show_alert=True,
            )
            return
        chat_id = str(raw_chat_id)
        from_user_id = str(callback_query.get("from", {}).get("id"))

        dedupe_key = f"chotky:{chat_id}:{message_id}"
        if self._reset_request_seen(dedupe_key):
            await self._answer(
                bot_token,
                callback_id,
                text="Yêu cầu chốt kỳ này đã được xử lý",
                show_alert=True,
            )
            return

        if from_user_id != requester_id:
            await self._answer(
                bot_token,
                callback_id,
                text="Chỉ người gõ /chotky mới xác nhận được",
                show_alert=True,
            )
            return

        self._mark_reset_request_resolved(dedupe_key)

        if self._reset_batch_tally_hook is None:
            await self._remove_buttons(bot_token, chat_id, message_id)
            await self._send_plain_message(
                bot_token, chat_id, "Chốt kỳ hiện không khả dụng."
            )
            await self._answer(bot_token, callback_id, text="", show_alert=False)
            return

        summary = await self._reset_batch_tally_hook()
        reply = format_chotky_summary(summary if isinstance(summary, dict) else {})

        await self._remove_buttons(bot_token, chat_id, message_id)
        await self._send_plain_message(bot_token, chat_id, reply)
        await self._answer(bot_token, callback_id, text="", show_alert=False)

    async def _handle_chotky_cancel(
        self, callback_query: dict, requester_id: str, bot_token: str
    ) -> None:
        """Cancel `/chotky` prompt — no period close, requester-bound."""
        callback_id = callback_query.get("id", "")
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        raw_chat_id = chat.get("id")
        message_id = message.get("message_id")
        if raw_chat_id is None or message_id is None:
            await self._answer(
                bot_token,
                callback_id,
                text="Nút đã hết hạn, gõ /chotky để lấy nút mới.",
                show_alert=True,
            )
            return
        chat_id = str(raw_chat_id)
        from_user_id = str(callback_query.get("from", {}).get("id"))

        dedupe_key = f"chotky:{chat_id}:{message_id}"
        if self._reset_request_seen(dedupe_key):
            await self._answer(
                bot_token,
                callback_id,
                text="Yêu cầu chốt kỳ này đã được xử lý",
                show_alert=True,
            )
            return

        if from_user_id != requester_id:
            await self._answer(
                bot_token,
                callback_id,
                text="Chỉ người gõ /chotky mới hủy được",
                show_alert=True,
            )
            return

        self._mark_reset_request_resolved(dedupe_key)

        await self._remove_buttons(bot_token, chat_id, message_id)
        await self._send_plain_message(bot_token, chat_id, "Đã hủy chốt kỳ")
        await self._answer(bot_token, callback_id, text="", show_alert=False)
