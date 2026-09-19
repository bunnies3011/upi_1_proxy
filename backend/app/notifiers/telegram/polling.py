"""Hạ tầng long-polling Telegram dùng CHUNG giữa spec `telegram-pull-job-mode`
và `telegram-callback-verify-plus` (design.md §2.2/§3.3).

Module này là **transport-only** — chỉ biết cách gọi `getUpdates`, phân loại
1 Update object thành "message" hoặc "callback_query", dedupe callback trùng,
rồi dispatch tới 2 callback được INJECT từ bên ngoài (`on_message`,
`on_callback_query`). Module này KHÔNG import `commands.py`/`callback_router.py`/
`pull_mode.py` — tránh phụ thuộc ngược (những module đó mới biết ý nghĩa
nghiệp vụ của `pull_job:*`/`plus_check:*`/`/start`...).

3 thành phần:
    - `CallbackDedupeCache`: LRU in-memory 500 `callback_query.id` gần nhất,
      chống xử lý trùng 1 callback (Telegram có thể gửi lại update nếu ACK
      offset chưa kịp persist trước khi crash).
    - `CallbackPollingTask`: 1 vòng đời long-poll `getUpdates(offset, timeout=25)`
      cho ĐẾN KHI bị cancel hoặc gặp HTTP 409 Conflict.
    - `PollingSupervisor`: vòng lặp 5s đọc `telegram.polling_enabled` +
      `telegram.bot_token`, spawn/cancel `CallbackPollingTask` tương ứng.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import TYPE_CHECKING, Awaitable, Callable

from app.notifiers.telegram.client import TelegramApiError, TelegramBotClient

if TYPE_CHECKING:
    from app.core.settings_store import SettingsRepository

_logger = logging.getLogger(__name__)

#: Số giây giữa 2 lần đọc lại `telegram.polling_enabled`/`telegram.bot_token`
#: trong `PollingSupervisor.run_forever` (design.md §3.3).
_SUPERVISOR_TICK_SECONDS = 5

#: Số giây Telegram giữ connection mở để long-poll trong `getUpdates`
#: (truyền `timeout=` cho `TelegramBotClient.get_updates`).
_LONG_POLL_TIMEOUT_SECONDS = 25

#: Backoff ngắn trước khi retry `getUpdates` sau lỗi transient (KHÔNG áp
#: dụng cho 409 — 409 exit hẳn, xem `CallbackPollingTask.run`). Tránh
#: hot-loop khi Telegram tạm thời unreachable (network blip, 5xx...).
_TRANSIENT_ERROR_BACKOFF_SECONDS = 1.0


# ---------------------------------------------------------------------------
# CallbackDedupeCache
# ---------------------------------------------------------------------------


class CallbackDedupeCache:
    """LRU set 500 `callback_query.id` gần nhất, in-memory.

    Chỉ được truy cập từ 1 `CallbackPollingTask` tại 1 thời điểm (vòng lặp
    polling tuần tự, không có concurrent access) — KHÔNG cần lock.

    Semantic LRU: `add()` đưa id vừa thêm/vừa dùng lại về vị trí "mới nhất"
    (cuối `OrderedDict`); khi vượt `maxsize`, phần tử CŨ NHẤT (đầu
    `OrderedDict`) bị evict trước.
    """

    def __init__(self, maxsize: int = 500) -> None:
        self._maxsize = maxsize
        self._store: OrderedDict[str, None] = OrderedDict()

    def seen(self, callback_id: str) -> bool:
        """True nếu `callback_id` đã được `add()` trước đó và còn trong cache.

        Pure lookup — KHÔNG mutate thứ tự LRU (chỉ `add()` mới cập nhật
        "recently used", giữ `seen()` an toàn để gọi nhiều lần không side-effect).
        """
        return callback_id in self._store

    def add(self, callback_id: str) -> None:
        """Ghi nhận `callback_id` đã xử lý — best-effort, KHÔNG abort vòng
        polling nếu có lỗi (per design.md §3.3: "add() bọc try/except log
        warning nếu lỗi, không abort").
        """
        try:
            if callback_id in self._store:
                self._store.move_to_end(callback_id)
            else:
                self._store[callback_id] = None
                if len(self._store) > self._maxsize:
                    self._store.popitem(last=False)
        except Exception:  # noqa: BLE001 — best-effort, không được raise
            _logger.warning(
                "telegram_dedupe_cache_add_failed callback_id=%s",
                callback_id,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Type aliases cho handler được inject từ bootstrap.py
# ---------------------------------------------------------------------------

#: Nhận 1 "message" object (dict con `message` của Update Telegram).
UpdateHandler = Callable[[dict], Awaitable[None]]
#: Nhận 1 "callback_query" object (dict con `callback_query` của Update).
CallbackHandler = Callable[[dict], Awaitable[None]]


def _extract_bot_id(token: str) -> str:
    """Trích `bot_id` từ bot token Telegram — phần INT trước dấu ':' đầu
    tiên (format token: `<bot_id>:<auth_hash>`). `update_id` sequence của
    Telegram là PER-BOT nên `bot_id` là mốc để biết offset có còn hợp lệ
    khi token đổi không.

    Phòng thủ: nếu token KHÔNG chứa ':' (token dị dạng), coi nguyên chuỗi
    là `bot_id` — vẫn cho phép so sánh nhất quán, không raise.
    """
    return token.split(":", 1)[0]


# ---------------------------------------------------------------------------
# CallbackPollingTask
# ---------------------------------------------------------------------------


class CallbackPollingTask:
    """1 vòng đời long-poll `getUpdates(offset, timeout=25)`.

    Vòng lặp trong `run()` chạy tới khi:
        - Bị cancel từ ngoài (`PollingSupervisor` gọi `task.cancel()`) —
          `asyncio.CancelledError` PHẢI propagate, không được swallow.
        - Telegram trả HTTP 409 Conflict (instance polling khác đang chạy
          cùng token) — log `"telegram_polling_conflict"` rồi `return`
          (exit sạch, KHÔNG raise). `self.conflict_detected` được set
          `True` NGAY TRƯỚC khi return để `PollingSupervisor` phân biệt
          "409 clean exit" (nên suppress respawn) với "crash bất ngờ"
          (nên cho phép respawn ngay tick kế).
    """

    def __init__(
        self,
        settings: "SettingsRepository",
        client: TelegramBotClient,
        dedupe: CallbackDedupeCache,
        bot_token: str,
        on_message: UpdateHandler,
        on_callback_query: CallbackHandler,
    ) -> None:
        self._settings = settings
        self._client = client
        self._dedupe = dedupe
        self._bot_token = bot_token
        self._on_message = on_message
        self._on_callback_query = on_callback_query

        #: Set `True` ngay trước khi `run()` return do 409 Conflict — xem
        #: docstring class. `PollingSupervisor` inspect field này SAU khi
        #: task hoàn thành để quyết định có suppress respawn hay không.
        self.conflict_detected = False

    async def run(self) -> None:
        """Long-poll loop chính — xem docstring class.

        Đọc `telegram.polling_offset` MỘT LẦN lúc khởi động (R13.2 spec
        `telegram-callback-verify-plus`), sau đó tự quản lý offset
        in-memory cho các lần `getUpdates` kế tiếp, chỉ persist lại vào
        Settings sau mỗi batch (TRƯỚC lần gọi kế — R13.1 spec đó).
        """
        raw_offset = await self._settings.get("telegram.polling_offset")
        offset = raw_offset if isinstance(raw_offset, int) else 0

        while True:
            try:
                updates = await self._client.get_updates(
                    self._bot_token,
                    offset,
                    timeout=_LONG_POLL_TIMEOUT_SECONDS,
                )
            except TelegramApiError as exc:
                if exc.status_code == 409:
                    # Instance polling khác đang chạy cùng token (VD user
                    # mở 2 backend cùng lúc, hoặc bot cũ chưa kịp tắt
                    # polling session). Exit sạch — KHÔNG raise, để
                    # Supervisor coi đây là "conflict" và suppress respawn
                    # tới khi token đổi hoặc enabled toggle (xem
                    # `PollingSupervisor`).
                    _logger.warning(
                        "telegram_polling_conflict http_status=409 "
                        "description=%s",
                        exc.description,
                    )
                    self.conflict_detected = True
                    return
                # Lỗi API khác (5xx, 429...) — coi là transient, backoff
                # ngắn rồi retry cùng offset (KHÔNG mất update nào).
                _logger.warning(
                    "telegram_polling_api_error http_status=%s "
                    "error_code=%s description=%s",
                    exc.status_code,
                    exc.error_code,
                    exc.description,
                )
                await asyncio.sleep(_TRANSIENT_ERROR_BACKOFF_SECONDS)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — network/transport error
                # KHÔNG dùng `exc_info=True`: bot token nằm trong URL
                # request nên full traceback của lỗi transport có thể lộ
                # token vào log. Chỉ log loại + message ngắn gọn.
                _logger.warning(
                    "telegram_polling_network_error error_type=%s detail=%s",
                    type(exc).__name__,
                    str(exc),
                )
                await asyncio.sleep(_TRANSIENT_ERROR_BACKOFF_SECONDS)
                continue

            for update in updates:
                await self._dispatch_one(update)

            if updates:
                # Phòng thủ: 1 update thiếu `update_id` (hoặc không phải
                # int) không được làm crash vòng polling. Lọc ra các
                # update_id hợp lệ; chỉ advance offset khi có ít nhất 1.
                update_ids = [
                    u.get("update_id")
                    for u in updates
                    if isinstance(u.get("update_id"), int)
                ]
                if update_ids:
                    new_offset = max(update_ids) + 1
                    # Persist TRƯỚC lần getUpdates kế (R13.1 spec kia) —
                    # thoả tự nhiên vì dòng này chạy trước khi loop quay
                    # lại đầu.
                    await self._settings.set("telegram.polling_offset", new_offset)
                    offset = new_offset

    async def _dispatch_one(self, update: dict) -> None:
        """Xử lý 1 Update object — dispatch theo field có mặt (R18.8), swallow
        exception TẠI BOUNDARY NÀY (R15.1 spec kia) để 1 update lỗi không
        chặn các update còn lại trong batch.
        """
        try:
            if "callback_query" in update:
                callback_query = update["callback_query"]
                callback_id = callback_query.get("id")
                if callback_id is not None and self._dedupe.seen(callback_id):
                    # Đã xử lý callback này trước đó (VD retry do offset
                    # chưa persist kịp trước khi restart) — skip handler,
                    # vẫn tính update_id này vào offset advancement.
                    return
                await self._on_callback_query(callback_query)
                if callback_id is not None:
                    self._dedupe.add(callback_id)
            elif "message" in update:
                await self._on_message(update["message"])
            # else: update không có "message" hay "callback_query" (VD
            # edited_message, channel_post...) — bỏ qua theo R18.8, vẫn
            # tính vào offset advancement (đã nằm trong `updates` batch).
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — swallow per-update, xem docstring
            _logger.warning(
                "telegram_polling_dispatch_failed update_id=%s",
                update.get("update_id"),
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# PollingSupervisor
# ---------------------------------------------------------------------------


class PollingSupervisor:
    """Đọc `telegram.polling_enabled` + `telegram.bot_token` mỗi 5s, spawn/
    cancel `CallbackPollingTask` tương ứng.

    Thuật toán suppress-respawn-sau-409 (KHÔNG được đặc tả chi tiết ở
    design.md — quyết định implementation ở đây, xem `_reconcile`):
    khi 1 `CallbackPollingTask` exit sạch do 409 (`conflict_detected=True`),
    Supervisor ghi nhận `(token, enabled)` tại thời điểm đó vào
    `self._suppressed_pair`. Ở mỗi tick 5s kế tiếp, NẾU `(token, enabled)`
    hiện tại VẪN giống hệt pair đã suppress → không spawn lại. Điều kiện
    "token đổi HOẶC enabled toggle" (design.md §3.3) được thoả bởi: chỉ cần
    1 trong 2 giá trị của tuple thay đổi (bao gồm cả trường hợp user tắt
    rồi bật lại `enabled` — lúc tắt, `enabled=False` đã khác tuple suppress
    ngay, Supervisor clear suppression; lúc bật lại, spawn bình thường).
    """

    def __init__(
        self,
        settings: "SettingsRepository",
        client: TelegramBotClient,
        dedupe: CallbackDedupeCache,
        on_message: UpdateHandler,
        on_callback_query: CallbackHandler,
    ) -> None:
        self._settings = settings
        self._client = client
        self._dedupe = dedupe
        self._on_message = on_message
        self._on_callback_query = on_callback_query

        #: Task asyncio đang chạy (wrap `CallbackPollingTask.run()`), hoặc
        #: `None` nếu hiện không có polling nào active.
        self._task: asyncio.Task[None] | None = None
        #: Instance `CallbackPollingTask` tương ứng `_task` — giữ riêng để
        #: có thể inspect `conflict_detected` sau khi task hoàn thành
        #: (asyncio.Task không tự expose attribute của coroutine object).
        self._task_instance: CallbackPollingTask | None = None
        #: Token đang được `_task` polling — dùng để phát hiện đổi token.
        self._active_token: str | None = None
        #: `bot_id` (phần trước dấu ':' trong token) của token ĐÃ spawn gần
        #: nhất. KHÁC `_active_token` ở chỗ: field này KHÔNG bị clear khi
        #: task bị cancel/reap — nó "sống dai" để so sánh bot_id khi spawn
        #: token mới, kể cả khi token cũ đã kết thúc từ tick trước (VD sau
        #: 409-suppress rồi admin mới đổi token). `None` = chưa từng spawn
        #: task nào (lần spawn đầu tiên KHÔNG reset offset — resume từ
        #: offset đã persist).
        self._previous_bot_id: str | None = None

        #: `(token, enabled)` đã gây ra 409 gần nhất — spawn bị suppress
        #: khi tuple hiện tại (đọc mỗi tick) còn khớp giá trị này. `None`
        #: = không có suppression đang active.
        self._suppressed_pair: tuple[str, bool] | None = None

    async def run_forever(self) -> None:
        """Vòng lặp giám sát vô hạn — `bootstrap.py` wrap bằng
        `asyncio.create_task`.

        Check-rồi-sleep (không sleep trước) để reconcile có hiệu lực ngay
        từ lần gọi đầu, không cần chờ 5s mới bắt đầu polling lần đầu.
        """
        while True:
            try:
                await self._reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — supervisor không được chết
                _logger.warning("telegram_polling_supervisor_tick_failed", exc_info=True)
            await asyncio.sleep(_SUPERVISOR_TICK_SECONDS)

    async def shutdown(self) -> None:
        """Cancel task đang chạy (nếu có) + chờ nó dừng sạch. Idempotent."""
        await self._cancel_current_task()

    async def _reconcile(self) -> None:
        """1 tick giám sát — đọc setting live, quyết định spawn/cancel."""
        raw_enabled = await self._settings.get("telegram.polling_enabled")
        raw_token = await self._settings.get("telegram.bot_token")
        enabled = bool(raw_enabled)
        token = raw_token if isinstance(raw_token, str) else ""

        # Trước tiên, nếu task cũ đã tự kết thúc (409 hoặc crash) — xử lý
        # trước khi quyết định spawn mới, để không nhầm "task done" với
        # "task còn sống".
        await self._reap_finished_task()

        current_pair = (token, enabled)
        if self._suppressed_pair is not None:
            if current_pair == self._suppressed_pair:
                # Vẫn cùng (token, enabled) đã gây 409 — không respawn.
                return
            # token đổi HOẶC enabled toggle — clear suppression, tiếp tục
            # reconcile bình thường ở dưới.
            self._suppressed_pair = None

        if not enabled or not token:
            await self._cancel_current_task()
            return

        if self._task is not None and not self._task.done():
            if self._active_token == token:
                # Đang polling đúng token hiện tại — không làm gì (idempotent).
                return
            # Token đổi khi task cũ vẫn còn sống — cancel task cũ + đóng
            # polling session cũ TRƯỚC khi mở session mới cho token mới.
            await self._cancel_current_task()

        # FIX D: trước khi spawn task cho token mới, nếu bot_id của token
        # mới KHÁC bot_id của token đã spawn gần nhất → reset
        # `telegram.polling_offset` về 0. `update_id` sequence là PER-BOT
        # nên offset (thường cao) của bot cũ sẽ khiến getUpdates của bot
        # MỚI trả rỗng cho tới khi update_id của bot mới vượt qua → bot mới
        # "như chết". Với ROTATE token CÙNG bot (revoke+reissue, cùng
        # bot_id) thì offset PHẢI giữ nguyên (sequence tiếp tục) nên chỉ
        # reset khi bot_id thực sự đổi. Lần spawn ĐẦU (previous=None) KHÔNG
        # reset — resume từ offset đã persist.
        await self._reset_offset_if_bot_changed(token)

        await self._spawn_task(token)

    async def _reap_finished_task(self) -> None:
        """Kiểm tra task hiện tại đã `done()` chưa (tự kết thúc, không do
        Supervisor cancel) — nếu có, phân loại 409-clean-exit vs crash.
        """
        if self._task is None or not self._task.done():
            return

        task_instance = self._task_instance
        exception = self._task.exception() if not self._task.cancelled() else None

        if task_instance is not None and task_instance.conflict_detected:
            # 409 clean exit — suppress respawn tới khi token/enabled đổi.
            self._suppressed_pair = (self._active_token or "", True)
        elif exception is not None:
            _logger.warning(
                "telegram_polling_task_crashed token=%s", self._active_token,
                exc_info=exception,
            )
            # Crash bất ngờ — KHÔNG suppress, cho phép respawn ngay tick này.

        self._task = None
        self._task_instance = None
        self._active_token = None

    async def _reset_offset_if_bot_changed(self, token: str) -> None:
        """Reset `telegram.polling_offset` về 0 khi `bot_id` của `token`
        sắp spawn khác `bot_id` của token đã spawn gần nhất (FIX D).

        Idempotent + phòng thủ: chỉ 1 lần `settings.set`. Cập nhật
        `_previous_bot_id` cho mọi lần spawn (kể cả lần đầu) để tick sau so
        sánh đúng. KHÔNG log giá trị token (token chứa auth hash) — chỉ log
        rằng phát hiện đổi bot + đã reset offset.
        """
        new_bot_id = _extract_bot_id(token)
        if self._previous_bot_id is not None and self._previous_bot_id != new_bot_id:
            await self._settings.set("telegram.polling_offset", 0)
            _logger.info(
                "telegram_polling_offset_reset_bot_changed"
            )
        self._previous_bot_id = new_bot_id

    async def _spawn_task(self, token: str) -> None:
        """Tạo `CallbackPollingTask` mới cho `token` + `asyncio.create_task`."""
        task_instance = CallbackPollingTask(
            settings=self._settings,
            client=self._client,
            dedupe=self._dedupe,
            bot_token=token,
            on_message=self._on_message,
            on_callback_query=self._on_callback_query,
        )
        self._task_instance = task_instance
        self._active_token = token
        self._task = asyncio.create_task(
            task_instance.run(), name="telegram-polling-task"
        )
        _logger.info("telegram_polling_task_spawned")

    async def _cancel_current_task(self) -> None:
        """Cancel task đang chạy (nếu có) + await cancellation + đóng lại
        polling session (để token mới, nếu có, mở session sạch). Idempotent.
        """
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — task lỗi trong lúc cancel
                _logger.warning(
                    "telegram_polling_task_error_during_cancel", exc_info=True
                )
            await self._client.reopen_polling_session()

        self._task = None
        self._task_instance = None
        self._active_token = None
