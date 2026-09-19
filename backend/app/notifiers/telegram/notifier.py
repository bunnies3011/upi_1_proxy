"""TelegramNotifier — orchestrator kết nối JobManager terminal hook với
`TelegramBotClient` + `RoundRobinDistributor`, gửi qua queue nội bộ.

Trách nhiệm:
    1. Đọc live 4 setting (`telegram.push_mode.enabled`, `telegram.bot_token`,
       `telegram.push_mode.send_photo`, `telegram.push_mode.chat_targets`)
       tại mỗi lần notify — user đổi setting qua UI có hiệu lực NGAY,
       không cần restart.
    2. Filter `chat_targets` theo `enabled=True` — chỉ chat được bật
       tham gia round-robin. Danh sách rỗng (tất cả tắt hoặc chưa cấu
       hình) → skip silent, không raise.
    3. `notify_qr_ready` build caption qua `formatter.build_caption`, đọc
       file QR PNG (chỉ khi `send_photo=True`) từ `record.artifact_path`
       rồi ENQUEUE 1 `_PendingSend` vào `_queue` và return ngay — KHÔNG
       tự gọi Telegram API tại đây.
    4. 1 worker task nền (`_worker_loop`) tiêu thụ queue tuần tự, gọi
       `client.send_photo` (khi flag `send_photo=True`) HOẶC
       `client.send_message` (khi flag `send_photo=False` — text-only,
       không kèm ảnh) và retry tối đa 3 lần (backoff 2s/5s/10s, tôn
       trọng `retry_after` khi Telegram trả 429) cho lỗi transient
       (429/5xx/network). Lỗi permanent (400/401/403) fail ngay, không
       retry.
    5. Fail_Fast tại notifier boundary: `notify_qr_ready` không raise
       (chỉ log + skip) nên `_invoke_terminal_hooks` return rất nhanh —
       KHÔNG giữ proxy lease/concurrency slot của job chính trong lúc
       gửi Telegram (đó là việc của worker chạy độc lập).

Tách "enqueue" (nhanh, trong `_run_handler`) khỏi "gửi + retry" (chậm,
worker nền) là fix cho bug "gửi Telegram fail 1 lần thì mất luôn, không
retry" — trước đây `notify_qr_ready` gọi `send_photo` trực tiếp và bị
`await` bởi job chính nên KHÔNG thể thêm retry/backoff mà không làm chậm
scheduler.

State: `_distributor.cursor` (round-robin) + `_queue`/`_worker_task`
(pending sends) — cả 2 đều in-memory, mất khi restart backend (chấp
nhận, cùng semantic với cursor cũ). Đổi settings, thêm/xóa chat → có
hiệu lực ngay lần `notify_qr_ready` kế (item đã enqueue dùng token/label
snapshot cũ, xem docstring `_PendingSend`).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from app.notifiers.telegram.client import TelegramApiError, TelegramBotClient
from app.notifiers.telegram.distributor import RoundRobinDistributor
from app.notifiers.telegram.formatter import (
    append_plan_outcome_status,
    build_batch_tally_text,
    build_caption,
    build_period_close_text,
    expired_tag_text,
    format_vn_time,
    mask_email,
    plus_tag_text,
)

if TYPE_CHECKING:
    from app.core.job_manager import JobManager, _JobRecord
    from app.core.settings_store import SettingsRepository
    from app.notifiers.telegram.live_qr_gate import LiveQrGate
    from app.notifiers.telegram.push_gate import PushSuccessGate


logger = logging.getLogger(__name__)

# Whitelist key trong Settings — copy hardcoded để tránh import cycle
# `notifier ↔ __init__.py`. Nếu tương lai đổi key, đổi cả 2 chỗ (hoặc
# refactor lên `keys.py` shared).
# Namespace `telegram.push_mode.*` — phân biệt với `telegram.pull_mode.*`
# (PullJobNotifier). `bot_token` giữ ở root level, dùng chung cho cả 2 mode.
_KEY_ENABLED = "telegram.push_mode.enabled"
_KEY_BOT_TOKEN = "telegram.bot_token"
_KEY_SEND_PHOTO = "telegram.push_mode.send_photo"
_KEY_CHAT_TARGETS = "telegram.push_mode.chat_targets"

# Bounded re-check while Live QR Gate is full — prevents infinite worker freeze
# if a wake is missed (stale slot / race). Success Wait alone still waits forever.
_LIVE_GATE_WAIT_TIMEOUT_SECONDS: Final[float] = 30.0
_LIVE_SWEEP_INTERVAL_SECONDS: Final[float] = 60.0

# ---------------------------------------------------------------------------
# Retry policy cho worker gửi Telegram (queue-based — xem `_worker_loop`).
#
# `_MAX_RETRIES` = số lần retry SAU lần gửi đầu (tổng số attempt =
# 1 + _MAX_RETRIES = 4). `_RETRY_DELAYS_SECONDS[i]` là thời gian chờ
# TRƯỚC attempt thứ (i+2) — giãn dần (2s → 5s → 10s) để tránh dồn request
# vào Telegram khi lỗi transient (network chập chờn, Telegram 5xx tạm
# thời) đồng thời không giữ job/queue quá lâu.
#
# Rate limit (429): Telegram trả `parameters.retry_after` (giây) — ưu
# tiên tôn trọng giá trị này nếu nó LỚN HƠN backoff theo lịch, tránh bị
# Telegram phạt nặng hơn vì gọi lại quá sớm (xem `_compute_backoff_delay`).
# ---------------------------------------------------------------------------
_MAX_RETRIES: Final[int] = 3
_RETRY_DELAYS_SECONDS: Final[tuple[float, ...]] = (2.0, 5.0, 10.0)


@dataclass(slots=True)
class _PendingSend:
    """1 item trong queue nội bộ — đủ dữ liệu để worker gửi độc lập với
    `_JobRecord` gốc (record có thể bị mutate/GC bởi JobManager trong lúc
    item còn nằm trong queue chờ tới lượt hoặc đang retry).

    `chat_id` đã được `RoundRobinDistributor.pick()` chọn TẠI THỜI ĐIỂM
    enqueue (trong `notify_qr_ready`) — worker KHÔNG pick lại khi retry,
    giữ đúng semantic "1 job = 1 chat cố định" dù phải gửi lại nhiều lần.
    `bot_token` cũng snapshot tại thời điểm enqueue — nếu user đổi token
    giữa lúc item còn trong queue, lần gửi/retry vẫn dùng token cũ (nhất
    quán với hành vi gốc: `_read_config()` chỉ đọc 1 lần mỗi `notify_qr_ready`).

    `send_photo`: snapshot flag `telegram.push_mode.send_photo` tại thời
    điểm enqueue. `True` → worker gọi `client.send_photo` (photo + caption).
    `False` → worker gọi `client.send_message` (text-only), `photo_bytes`
    khi đó = `b""` (không đọc file để tiết kiệm I/O). Snapshot giúp việc
    user toggle setting giữa lúc item chờ trong queue KHÔNG đổi cách gửi
    của item đó — nhất quán với semantic snapshot của `bot_token`.
    """

    send_key: str
    job_id: str
    chat_id: str
    chat_label: str
    bot_token: str
    photo_bytes: bytes
    caption: str
    account_line_masked: str
    send_photo: bool
    account_line: str
    finished_at: float
    payment_link: str | None
    payment_method: str | None
    order: int | None
    qr_expires_at: float | None
    sent_at: float
    live_slot_reserved: bool = False


def _is_retryable_api_error(exc: TelegramApiError) -> bool:
    """True nếu lỗi Telegram API là transient — đáng thử lại.

    Retryable:
        - `error_code == 429` (Too Many Requests — rate limit tạm thời).
        - `status_code >= 500` (lỗi Telegram-side, VD 502/503 khi bot API
          quá tải).

    KHÔNG retryable (lỗi cấu hình/permanent — retry chỉ tốn thời gian và
    delay queue vô ích):
        - 400 Bad Request (VD "chat not found", caption sai format).
        - 401 Unauthorized (bot_token sai/revoked).
        - 403 Forbidden (bot bị kick khỏi chat/group).
    """
    if exc.error_code == 429:
        return True
    return exc.status_code >= 500


def _compute_backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Tính thời gian chờ trước attempt kế tiếp.

    Args:
        attempt: Số thứ tự attempt VỪA fail (1-based — attempt=1 là lần
            gửi đầu tiên fail).
        retry_after: Giá trị `retry_after` Telegram trả (chỉ có ở lỗi
            429). `None` cho mọi lỗi khác.

    Returns:
        `max(scheduled_delay, retry_after)` nếu Telegram yêu cầu chờ lâu
        hơn lịch backoff mặc định — tôn trọng rate limit thực tế. Ngược
        lại dùng lịch backoff cố định `_RETRY_DELAYS_SECONDS`.
    """
    index = min(attempt - 1, len(_RETRY_DELAYS_SECONDS) - 1)
    scheduled = _RETRY_DELAYS_SECONDS[index]
    if retry_after is not None and retry_after > scheduled:
        return retry_after
    return scheduled


class TelegramConfigError(Exception):
    """Raise khi cấu hình Telegram không hợp lệ (test endpoint dùng để
    phân biệt lỗi config vs lỗi API).

    Notifier hook (`notify_qr_ready`) KHÔNG raise class này — chỉ log
    và skip. Endpoint `/api/notifications/telegram/test` raise class này
    để trả 400 với message rõ ràng cho user.
    """


class TelegramNotifier:
    """Callable notifier — instance được đăng ký làm `JobTerminalHook`.

    Attributes:
        _settings: `SettingsRepository` để đọc live cấu hình.
        _client: `TelegramBotClient` shared.
        _distributor: `RoundRobinDistributor` shared cursor.
    """

    def __init__(
        self,
        settings: "SettingsRepository",
        client: TelegramBotClient,
        distributor: RoundRobinDistributor,
        job_manager: "JobManager | None" = None,
        push_gate: "PushSuccessGate | None" = None,
        live_qr_gate: "LiveQrGate | None" = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._distributor = distributor
        # `job_manager` optional để giữ backward compat với test unit tự
        # construct notifier không cần track history. Khi bootstrap gọi
        # `register_telegram_notifier(job_manager=...)` → inject để hook
        # có thể ghi lại lần gửi vào `_JobRecord.telegram_notifications`.
        self._job_manager = job_manager
        # `push_gate` optional (giống `job_manager`) — chỉ có trong flow
        # bootstrap production. Test unit tự construct notifier không
        # cần gate vẫn OK; các nhánh success branch dưới đây phòng thủ
        # `None`. Public attribute để routes_notifications truy cập qua
        # `notifier.push_gate.snapshot()`/`.resume()` mà không cần
        # DI singleton riêng.
        self.push_gate = push_gate
        # Live QR capacity gate — optional; acquire after send, release on
        # plus|timeout|lifecycle|TTL sweep. Public for routes snapshot.
        self.live_qr_gate = live_qr_gate

        # ------------------------------------------------------------------
        # Queue + worker nền — TÁCH gửi Telegram khỏi `_run_handler` của
        # JobManager (Bug fix "0/1" không retry).
        #
        # Trước đây `notify_qr_ready` gọi `client.send_photo` TRỰC TIẾP và
        # được `await` bởi `JobManager._invoke_terminal_hooks` — nghĩa là
        # nếu thêm retry+backoff ngay tại đó, mỗi job QR_READY sẽ giữ proxy
        # lease + concurrency slot lâu hơn (vài giây tới ~15s) chỉ để chờ
        # Telegram, ảnh hưởng throughput khi chạy Multi-N job song song.
        #
        # Giải pháp: `notify_qr_ready` giờ chỉ build `_PendingSend` rồi
        # `put_nowait` vào `_queue` và return NGAY — không block job chính.
        # 1 worker task tiêu thụ queue TUẦN TỰ (`_worker_loop`), tự nhiên
        # throttle tốc độ gửi (tránh burst nhiều job QR_READY cùng lúc dồn
        # Telegram 429) và có budget thời gian riêng để retry.
        #
        # `maxsize=0` (unbounded) — chấp nhận vì mỗi item chỉ ~vài chục KB
        # (photo_bytes QR PNG) và tốc độ enqueue (job hoàn thành) luôn thấp
        # hơn nhiều tốc độ dequeue thực tế (trừ khi Telegram down hoàn toàn
        # kéo dài, khi đó unbounded queue chấp nhận được — mất mát thà trễ
        # còn hơn rớt notify).
        self._queue: asyncio.Queue[_PendingSend] = asyncio.Queue()
        self._worker_task: asyncio.Task[None] | None = None
        self._sweep_task: asyncio.Task[None] | None = None
        # Cờ báo shutdown đang diễn ra — worker check để không cố retry
        # (sleep) vô ích khi backend sắp tắt.
        self._closing = False
        # Event để worker chờ khi Push_Success_Gate paused. Set trong
        # `_wake_worker` (được `push_gate.resume()` gọi khi user bấm
        # "Tiếp tục"). Ngoài ra được set ban đầu (`set()`) để worker
        # KHÔNG chờ khi gate không paused — worker `wait()` chỉ block
        # khi ta `clear()` event ngay trước wait.
        self._gate_resumed_event: asyncio.Event = asyncio.Event()
        self._gate_resumed_event.set()
        # Wire callback wake-worker vào gate — sync, chỉ set event.
        # Gate không None → callback tạo closure over `self._gate_resumed_event`.
        # Test unit tự construct notifier với `push_gate=None` bỏ qua.
        if self.push_gate is not None:
            self.push_gate.set_wake_worker_callback(self._on_gate_resumed)
        if self.live_qr_gate is not None:
            self.live_qr_gate.set_wake_worker_callback(self._on_gate_resumed)

        # Per-chat lock serializes tally send/edit so concurrent outcomes
        # for one chat cannot create duplicate tally messages. Counts,
        # message id, and outcome dedup live in SQLite via JobManager.
        self._tally_locks: dict[str, asyncio.Lock] = {}
        # Serializes period-close so two concurrent chốt kỳ cannot race
        # receipt/close/rearm on the same tallies.
        self._reset_batch_tally_lock = asyncio.Lock()
        # Caption gốc lúc gửi (để editMessageCaption ghép status).
        self._sent_captions: dict[str, str] = {}  # job_id → caption HTML
        # Dedup theo QR payload, không chỉ theo job_id: nếu hook QR_READY bị
        # gọi lại trong lúc worker chưa kịp lưu message_id, cùng QR vẫn chỉ
        # được enqueue/gửi 1 lần.
        self._send_state_lock = asyncio.Lock()
        self._queued_send_keys: set[str] = set()
        self._sent_send_keys: set[str] = set()

    # ------------------------------------------------------------------
    # Hook chính cho JobManager
    # ------------------------------------------------------------------
    async def notify_qr_ready(self, record: "_JobRecord") -> None:
        """Terminal hook — enqueue 1 lần gửi QR PNG + caption tới 1 chat
        theo round-robin. KHÔNG tự gửi ở đây — chỉ build `_PendingSend`
        và đẩy vào `_queue`, worker nền (`_worker_loop`) mới thực sự gọi
        Telegram API + retry khi cần (xem docstring `__init__`).

        Return NGAY sau khi enqueue để KHÔNG giữ proxy lease/concurrency
        slot của `_run_handler` trong lúc chờ Telegram — quan trọng khi
        chạy Multi-N job song song.

        Skip silent (log info, không raise) trong các trường hợp:
            - `telegram.enabled` = False.
            - `bot_token` rỗng.
            - Không có chat_target nào `enabled=True`.
            - `record.artifact_path` = None hoặc file không tồn tại.

        Đọc file QR PNG NGAY tại đây (trước khi enqueue) — record's
        `artifact_path` chỉ đảm bảo còn tồn tại lúc job vừa QR_READY, đọc
        sớm tránh race nếu file bị dọn dẹp trước khi worker tới lượt xử lý
        item trong queue.
        """
        # R1.6: job Pull_Mode (có `pull_assignment_state`) KHÔNG được
        # Push_Mode_Notifier này xử lý — chúng được `PullJobNotifier` xử
        # lý riêng qua flow assign/claim. Early-return trước khi đọc config
        # để tránh side-effect (enqueue gửi trùng) cho job Pull_Mode.
        if record.pull_assignment_state is not None:
            return
        job_id = record.job.job_id
        send_key = _notification_send_key(record)
        async with self._send_state_lock:
            if send_key in self._queued_send_keys or send_key in self._sent_send_keys:
                logger.info(
                    "telegram notify skipped duplicate: already queued/sent job_id=%s send_key=%s",
                    job_id,
                    send_key,
                )
                return
        config = await self._read_config()
        if not config.enabled:
            return
        if not config.bot_token:
            logger.info(
                "telegram notify skipped: bot_token rỗng (job_id=%s)",
                job_id,
            )
            return

        active_chat_ids = _filter_active_chat_ids(config.chat_targets)
        if not active_chat_ids:
            logger.info(
                "telegram notify skipped: không có chat_target enabled (job_id=%s)",
                job_id,
            )
            return

        # Chỉ đọc file QR khi thực sự cần gửi ảnh — khi user tắt
        # `send_photo`, notifier gửi text-only qua `send_message` (giữ
        # nguyên caption với email masked + link thanh toán). Tách nhánh
        # ở đây để không I/O phí file khi user chỉ muốn nhận link.
        if config.send_photo and not record.artifact_path and record.payment_link:
            logger.info(
                "telegram notify text-only: artifact_path=None but "
                "payment_link present (job_id=%s)",
                job_id,
            )
            config.send_photo = False

        if config.send_photo:
            artifact = record.artifact_path
            if not artifact:
                logger.warning(
                    "telegram notify skipped: artifact_path=None cho QR_READY job "
                    "(job_id=%s)",
                    job_id,
                )
                return

            artifact_path = Path(artifact)
            if not artifact_path.is_file():
                logger.warning(
                    "telegram notify skipped: file QR không tồn tại "
                    "(job_id=%s path=%s)",
                    job_id,
                    artifact_path,
                )
                return

            try:
                photo_bytes = artifact_path.read_bytes()
            except OSError as exc:
                logger.warning(
                    "telegram notify skipped: đọc file QR fail "
                    "(job_id=%s err=%s)",
                    job_id,
                    exc,
                )
                return
        else:
            photo_bytes = b""

        async with self._send_state_lock:
            if (
                send_key in self._queued_send_keys
                or send_key in self._sent_send_keys
            ):
                logger.info(
                    "telegram notify skipped duplicate: job_id=%s send_key=%s",
                    job_id,
                    send_key,
                )
                return
            self._queued_send_keys.add(send_key)
        live_slot_reserved = False
        reserved_chat_id: str | None = None
        if self.live_qr_gate is not None:
            await self.live_qr_gate.refresh_config()
            if self.live_qr_gate.snapshot().get("enabled"):
                reserved = await self.live_qr_gate.reserve_available(
                    job_id,
                    active_chat_ids,
                    send_key,
                )
                if reserved is None:
                    await self._release_send_reservation(send_key)
                    logger.info(
                        "telegram notify deferred: live_qr full before enqueue "
                        "job_id=%s",
                        job_id,
                    )
                    if self._job_manager is not None:
                        defer = getattr(
                            self._job_manager,
                            "defer_qr_ready_for_gate",
                            None,
                        )
                        if defer is not None:
                            await defer(
                                job_id,
                                reason="live_qr_full_before_notify",
                            )
                    return
                live_slot_reserved = True
                _slot_id, reserved_chat_id = reserved
        try:
            chat_id = (
                reserved_chat_id
                if reserved_chat_id is not None
                else await self._distributor.pick(active_chat_ids)
            )
            # Resolve label cho chat_id đã pick — dùng cho UI hiển thị "Đã gửi
            # tới <label> (<chat_id>)". Fallback về chat_id nếu label rỗng.
            chat_label = _resolve_chat_label(config.chat_targets, chat_id)
            queued_at = time.time()
            caption = build_caption(
                account_line=record.job.account_line,
                finished_at=record.finished_at,
                payment_link=record.payment_link,
                payment_method=record.job.payment_method,
                order=record.order,
                qr_expires_at=record.qr_expires_at,
                chat_label=chat_label,
                chat_id=chat_id,
                sent_at=queued_at,
                username=record.pull_assigned_username,
                first_name=record.pull_assigned_first_name,
            )

            item = _PendingSend(
                send_key=send_key,
                job_id=job_id,
                chat_id=chat_id,
                chat_label=chat_label,
                bot_token=config.bot_token,
                photo_bytes=photo_bytes,
                caption=caption,
                account_line_masked=mask_email(record.job.account_line),
                send_photo=config.send_photo,
                account_line=record.job.account_line,
                finished_at=record.finished_at,
                payment_link=record.payment_link,
                payment_method=record.job.payment_method,
                order=record.order,
                qr_expires_at=record.qr_expires_at,
                sent_at=queued_at,
                live_slot_reserved=live_slot_reserved,
            )
            self._ensure_worker_started()
            self._queue.put_nowait(item)
        except Exception:
            await self._release_send_reservation(send_key)
            if live_slot_reserved:
                await self._release_live_slots_safe(job_id)
            raise
        logger.info(
            "telegram notify enqueued: job_id=%s chat_id=%s label=%s "
            "send_photo=%s queue_depth=%d",
            job_id,
            chat_id,
            chat_label,
            config.send_photo,
            self._queue.qsize(),
        )

    # ------------------------------------------------------------------
    # Worker nền — tiêu thụ `_queue` tuần tự, retry theo policy module-level
    # ------------------------------------------------------------------
    def _ensure_worker_started(self) -> None:
        """Lazy-start `_worker_loop` task — gọi mỗi lần enqueue để tự phục
        hồi nếu worker task cũ chết vì bug (không nên xảy ra vì
        `_worker_loop` tự swallow exception, nhưng defensive tốt hơn im
        lặng ngừng xử lý queue vĩnh viễn).
        """
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(
                self._worker_loop(), name="telegram-notifier-worker"
            )
        self._ensure_sweep_started()

    def _on_gate_resumed(self) -> None:
        """Callback SYNC — wake worker waiting in `_wait_for_gate_open`.

        Used by PushSuccessGate.resume() and LiveQrGate release/sweep.
        """
        self._gate_resumed_event.set()

    def _is_push_gate_blocked(self) -> bool:
        return self.push_gate is not None and self.push_gate.is_paused()

    def _is_live_gate_blocked(self) -> bool:
        return self.live_qr_gate is not None and self.live_qr_gate.is_blocked()

    async def _wait_for_gate_open(self) -> None:
        """Block worker until Success Wait is not paused AND Live Gate not full.

        Success Wait alone: wait forever for user resume.
        Live full: bounded wait + re-check so a missed wake cannot freeze
        the single worker loop permanently.
        """
        if self.push_gate is None and self.live_qr_gate is None:
            return
        while True:
            push_blocked = self._is_push_gate_blocked()
            live_blocked = self._is_live_gate_blocked()
            if not push_blocked and not live_blocked:
                return
            self._gate_resumed_event.clear()
            push_blocked = self._is_push_gate_blocked()
            live_blocked = self._is_live_gate_blocked()
            if not push_blocked and not live_blocked:
                self._gate_resumed_event.set()
                return
            logger.info(
                "telegram worker: gate blocked push=%s live=%s "
                "(queue_depth=%d)",
                push_blocked,
                live_blocked,
                self._queue.qsize(),
            )
            if live_blocked:
                try:
                    await asyncio.wait_for(
                        self._gate_resumed_event.wait(),
                        timeout=_LIVE_GATE_WAIT_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    # Re-evaluate is_blocked (sweep may have freed slots).
                    if self.live_qr_gate is not None:
                        try:
                            await self.live_qr_gate.refresh_config()
                        except Exception:  # noqa: BLE001
                            pass
                    continue
            else:
                await self._gate_resumed_event.wait()

    def schedule_release_live_slots(self, job_id: str) -> None:
        """SYNC entry for lifecycle hooks — schedule async release_job."""
        if self.live_qr_gate is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(
            self._release_live_slots_safe(job_id),
            name=f"live-qr-release-{job_id}",
        )

    async def _release_live_slots_safe(self, job_id: str) -> None:
        if self.live_qr_gate is None:
            return
        try:
            await self.live_qr_gate.release_job(job_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "live_qr release_job fail job_id=%s: %s: %s",
                job_id,
                type(exc).__name__,
                exc,
            )

    async def _maybe_acquire_live_slot(
        self, job_id: str, chat_id: str, message_id: int | None
    ) -> None:
        if self.live_qr_gate is None:
            return
        try:
            await self.live_qr_gate.refresh_config()
            if not self.live_qr_gate.snapshot().get("enabled"):
                return
            # Skip acquire when job already terminal for live accounting:
            # early-plus race, or stop/delete between send OK and acquire.
            if self._job_manager is not None:
                rec = self._job_manager.get_job(job_id)
                if rec is None:
                    return
                if rec.plan == "plus":
                    return
                status = getattr(rec.status, "value", rec.status)
                if status in ("stopped", "error"):
                    return
            await self.live_qr_gate.acquire(job_id, chat_id, message_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "live_qr acquire fail job_id=%s: %s: %s",
                job_id,
                type(exc).__name__,
                exc,
            )

    async def _maybe_repick_live_target(self, item: "_PendingSend") -> bool:
        """When Live Gate on, pick least-loaded chat; return False if all full."""
        if self.live_qr_gate is None:
            return True
        try:
            await self.live_qr_gate.refresh_config()
            if not self.live_qr_gate.snapshot().get("enabled"):
                return True
            config = await self._read_config()
            active = _filter_active_chat_ids(config.chat_targets)
            target = self.live_qr_gate.least_loaded_available_chat(active)
            if target is None:
                return False
            item.chat_id = target
            item.chat_label = _resolve_chat_label(config.chat_targets, target)
            item.caption = build_caption(
                account_line=item.account_line,
                finished_at=item.finished_at,
                payment_link=item.payment_link,
                payment_method=item.payment_method,
                order=item.order,
                qr_expires_at=item.qr_expires_at,
                chat_label=item.chat_label,
                chat_id=item.chat_id,
                sent_at=item.sent_at,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "live_qr repick fail job_id=%s: %s: %s",
                item.job_id,
                type(exc).__name__,
                exc,
            )
            return True

    def _ensure_sweep_started(self) -> None:
        if self.live_qr_gate is None or self._closing:
            return
        if self._sweep_task is None or self._sweep_task.done():
            self._sweep_task = asyncio.create_task(
                self._live_sweep_loop(), name="telegram-live-qr-sweep"
            )

    async def _live_sweep_loop(self) -> None:
        while not self._closing:
            try:
                await asyncio.sleep(_LIVE_SWEEP_INTERVAL_SECONDS)
                if self.live_qr_gate is None or self._closing:
                    return
                await self.live_qr_gate.refresh_config()
                await self.live_qr_gate.sweep_expired()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "live_qr sweep loop error: %s: %s",
                    type(exc).__name__,
                    exc,
                )

    async def _worker_loop(self) -> None:
        """Vòng lặp vô hạn: lấy `_PendingSend` từ queue, gửi + retry.

        Chạy tuần tự (1 item tại 1 thời điểm) — đây chính là cơ chế
        throttle tự nhiên: nếu 5 job QR_READY cùng lúc, chat Telegram
        nhận request rải ra theo thời gian xử lý + backoff thay vì burst
        5 request đồng thời (giảm khả năng 429).

        TRƯỚC mỗi lần gửi, worker chờ `_wait_for_gate_open` — nếu
        Push_Success_Gate paused, block đến khi user bấm "Tiếp tục".
        Đây là root cause fix cho bug "cài 50 vẫn gửi 65": các item đã
        enqueue TRƯỚC lúc gate hit threshold vẫn nằm chờ trong queue,
        worker sẽ dừng gửi khi gate paused thay vì drain sạch.

        Thoát vòng lặp khi `asyncio.CancelledError` (shutdown) — item
        đang xử lý bị bỏ giữa chừng, các item còn lại trong queue KHÔNG
        được gửi (chấp nhận mất, giống in-memory cursor — xem docstring
        module `distributor.py`).
        """
        while True:
            item = await self._queue.get()
            try:
                if not item.live_slot_reserved:
                    while True:
                        await self._wait_for_gate_open()
                        if await self._maybe_repick_live_target(item):
                            break
                        # All chats full after wait — re-enter gate wait.
                        self._gate_resumed_event.clear()
                        try:
                            await asyncio.wait_for(
                                self._gate_resumed_event.wait(),
                                timeout=_LIVE_GATE_WAIT_TIMEOUT_SECONDS,
                            )
                        except asyncio.TimeoutError:
                            pass
                await self._send_with_retry(item)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — worker không được chết
                logger.exception(
                    "telegram worker: lỗi không mong đợi xử lý item "
                    "job_id=%s chat_id=%s",
                    item.job_id,
                    item.chat_id,
                )
            finally:
                self._queue.task_done()

    async def _send_with_retry(self, item: "_PendingSend") -> None:
        """Gửi 1 `_PendingSend`, retry tối đa `_MAX_RETRIES` lần cho lỗi
        transient (xem `_is_retryable_api_error`). Track ĐÚNG 1 entry vào
        job history sau khi kết thúc (thành công hoặc hết retry) — không
        spam nhiều dòng lịch sử cho các attempt fail giữa đường.

        Backoff: `_compute_backoff_delay` — ưu tiên `retry_after` Telegram
        trả (429) nếu lớn hơn lịch mặc định.
        """
        # Tên method dùng cho log — cùng retry policy áp dụng cho cả 2
        # endpoint (`sendPhoto` và `sendMessage`) vì Telegram trả cùng
        # kiểu error_code/retry_after cho cả hai. Chỉ khác endpoint được
        # gọi + cách log dispatch.
        api_method = "send_photo" if item.send_photo else "send_message"
        attempt = 0
        last_error: dict[str, Any] | None = None
        while True:
            attempt += 1
            try:
                if item.send_photo:
                    send_result = await self._client.send_photo(
                        token=item.bot_token,
                        chat_id=item.chat_id,
                        photo_bytes=item.photo_bytes,
                        caption=item.caption,
                    )
                else:
                    send_result = await self._client.send_message(
                        token=item.bot_token,
                        chat_id=item.chat_id,
                        text=item.caption,
                    )
            except TelegramApiError as exc:
                last_error = {
                    "status_code": exc.status_code,
                    "error_code": exc.error_code,
                    "description": exc.description,
                }
                retryable = _is_retryable_api_error(exc)
                logger.warning(
                    "telegram %s API error (attempt=%d retryable=%s): "
                    "job_id=%s chat_id=%s http_status=%s error_code=%s "
                    "description=%s",
                    api_method,
                    attempt,
                    retryable,
                    item.job_id,
                    item.chat_id,
                    exc.status_code,
                    exc.error_code,
                    exc.description,
                )
                if not retryable or attempt > _MAX_RETRIES:
                    break
                delay = _compute_backoff_delay(attempt, exc.retry_after)
                await self._sleep_before_retry(delay)
                continue
            except Exception as exc:  # noqa: BLE001 — network/transport error
                last_error = {
                    "status_code": None,
                    "error_code": None,
                    "description": f"{type(exc).__name__}: {exc}",
                }
                logger.warning(
                    "telegram %s unexpected error (attempt=%d): "
                    "job_id=%s chat_id=%s type=%s msg=%s",
                    api_method,
                    attempt,
                    item.job_id,
                    item.chat_id,
                    type(exc).__name__,
                    exc,
                )
                # Network/transport error luôn coi là transient — retry.
                if attempt > _MAX_RETRIES:
                    break
                delay = _compute_backoff_delay(attempt, None)
                await self._sleep_before_retry(delay)
                continue
            else:
                # message_id từ Telegram result → plus-check reply đúng QR.
                message_id: int | None = None
                if isinstance(send_result, dict):
                    raw_mid = send_result.get("message_id")
                    if isinstance(raw_mid, int):
                        message_id = raw_mid
                logger.info(
                    "telegram notified via %s: job_id=%s chat_id=%s "
                    "label=%s email=%s attempt=%d message_id=%s",
                    api_method,
                    item.job_id,
                    item.chat_id,
                    item.chat_label,
                    item.account_line_masked,
                    attempt,
                    message_id,
                )
                self._sent_captions[item.job_id] = item.caption
                await self._track_notification(
                    item.job_id,
                    {
                        "chat_id": item.chat_id,
                        "chat_label": item.chat_label,
                        "sent_at": time.time(),
                        "success": True,
                        "error": None,
                        "attempts": attempt,
                        "message_id": message_id,
                    },
                )
                await self._mark_send_success(item.send_key)
                # Push_Success_Gate: tăng counter + set paused nếu đủ
                # ngưỡng. Đọc setting `enabled` live bên trong (no-op
                # khi disabled). Best-effort: exception đã swallow trong
                # `on_success_sent`. Đặt SAU `_track_notification` để giữ
                # thứ tự SSE event: `job_notified` trước, `push_gate_updated`
                # sau — FE dễ suy luận trạng thái.
                if self.push_gate is not None:
                    await self.push_gate.on_success_sent()
                if not item.live_slot_reserved:
                    await self._maybe_acquire_live_slot(
                        item.job_id, item.chat_id, message_id
                    )
                return

        # Hết retry (hoặc lỗi permanent ngay từ đầu) — track FAILURE kèm
        # `attempts` để UI/log biết đã cố gắng bao nhiêu lần trước khi bỏ.
        logger.error(
            "telegram notify FAILED sau %d attempt(s): job_id=%s chat_id=%s "
            "error=%s",
            attempt,
            item.job_id,
            item.chat_id,
            last_error,
        )
        await self._track_notification(
            item.job_id,
            {
                "chat_id": item.chat_id,
                "chat_label": item.chat_label,
                "sent_at": time.time(),
                "success": False,
                "error": last_error,
                "attempts": attempt,
            },
        )
        if item.live_slot_reserved:
            await self._release_live_slots_safe(item.job_id)
        await self._release_send_reservation(item.send_key)

    async def _sleep_before_retry(self, delay: float) -> None:
        """Chờ `delay` giây trước attempt kế — no-op nếu đang shutdown
        (`_closing=True`) để tránh giữ event loop lúc backend đang tắt.
        """
        if self._closing:
            return
        await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # Test endpoint helper — dùng cho `POST /api/notifications/telegram/test`
    # ------------------------------------------------------------------
    async def send_test_message(self) -> dict[str, Any]:
        """Gửi text-only ping tới TẤT CẢ chat đang enabled.

        Khác `notify_qr_ready` (round-robin), test message broadcast để
        user thấy toàn bộ chat đã cấu hình đúng. Trả dict metadata cho
        UI hiển thị: `{sent: [...], failed: [...]}`.

        Raise `TelegramConfigError` nếu bot_token rỗng hoặc không có chat
        target enabled — user cần biết ngay cấu hình chưa đúng (khác
        `notify_qr_ready` skip silent vì đó là async background hook,
        không có UI feedback).
        """
        config = await self._read_config()
        if not config.bot_token:
            raise TelegramConfigError("bot_token has not been configured")

        active_chat_ids = _filter_active_chat_ids(config.chat_targets)
        if not active_chat_ids:
            raise TelegramConfigError(
                "No chat_target is enabled — add a chat_id "
                "and enable it before testing"
            )

        text = (
            "🧪 <b>iDEAL QR Tool</b>\n"
            "Telegram bot connection test — if you see this message, "
            "the configuration is correct."
        )
        sent: list[str] = []
        failed: list[dict[str, str]] = []
        for chat_id in active_chat_ids:
            try:
                await self._client.send_message(
                    token=config.bot_token,
                    chat_id=chat_id,
                    text=text,
                )
                sent.append(chat_id)
            except TelegramApiError as exc:
                failed.append(
                    {
                        "chat_id": chat_id,
                        "error": f"http_status={exc.status_code} "
                        f"error_code={exc.error_code} "
                        f"description={exc.description}",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                failed.append(
                    {"chat_id": chat_id, "error": f"{type(exc).__name__}: {exc}"}
                )
        return {"sent": sent, "failed": failed}

    # ------------------------------------------------------------------
    # Reset cursor — expose cho endpoint API
    # ------------------------------------------------------------------
    async def reset_round(self) -> int:
        """Reset cursor round-robin về 0. Trả cursor cũ trước khi reset."""
        return await self._distributor.reset()

    def cursor(self) -> int:
        """Snapshot cursor hiện tại (không mutate) — hữu ích cho UI hiển thị."""
        return self._distributor.snapshot_cursor()

    async def aclose(self) -> None:
        """Đóng worker task + HTTP session bên trong — gọi tại shutdown backend.

        Thứ tự: set `_closing=True` (worker đang sleep giữa retry sẽ bỏ
        sleep ở lượt check kế — xem `_sleep_before_retry`) → cancel worker
        task + chờ nó thực sự dừng → đóng HTTP session. Item còn lại
        trong queue (nếu có) bị bỏ — chấp nhận mất, giống cursor in-memory.

        Idempotent. Public method để `main.py` shutdown không cần reach
        vào `notifier._client` (private).
        """
        self._closing = True
        if self._sweep_task is not None and not self._sweep_task.done():
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except asyncio.CancelledError:
                pass
        if self._worker_task is not None and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        await self._client.aclose()

    async def notify_plus_verified(self, job_id: str) -> None:
        """Hook khi job vừa `plan=plus` — free live slot then tag/tally."""
        # Release BEFORE outcome guards so missing message_id / disabled
        # telegram cannot leave an orphan live slot.
        await self._release_live_slots_safe(job_id)
        await self._notify_plan_outcome(job_id, "plus")

    async def notify_plan_check_timeout(self, job_id: str) -> None:
        """Hook khi auto-poll 5 phút hết — free live slot then tag expired."""
        await self._release_live_slots_safe(job_id)
        await self._notify_plan_outcome(job_id, "expired")

    def _resolve_telegram_target(
        self, record: "_JobRecord"
    ) -> tuple[str | None, int | None]:
        """Lấy (chat_id, message_id) từ record hoặc history notify."""
        chat_id = record.telegram_message_chat_id
        message_id = record.telegram_message_id
        if chat_id and message_id is not None:
            return chat_id, message_id
        for entry in reversed(record.telegram_notifications):
            if entry.get("success") and entry.get("message_id") and entry.get("chat_id"):
                try:
                    mid = int(entry["message_id"])
                except (TypeError, ValueError):
                    continue
                return str(entry["chat_id"]), mid
        return None, None

    async def _notify_plan_outcome(self, job_id: str, outcome: str) -> None:
        """Tag reply + edit caption + tally cho outcome plus|expired.

        Worker group thấy QR nào thành công / hết poll. Best-effort.
        Dedup + count are one atomic claim-and-count in SQLite,
        committed before the tag send so a failed send still leaves
        payroll correct.
        """
        if self._job_manager is None:
            return
        record = self._job_manager.get_job(job_id)
        if record is None:
            return
        # Timeout hook sau khi đã plus (race) → bỏ qua.
        if outcome == "expired" and record.plan == "plus":
            return

        chat_id: str | None = None
        message_id: int | None = None
        for attempt in range(6):
            chat_id, message_id = self._resolve_telegram_target(record)
            if chat_id and message_id is not None:
                break
            if attempt < 5:
                await asyncio.sleep(2)
        if not chat_id or message_id is None:
            logger.info(
                "plan outcome notify skip: job_id=%s outcome=%s missing message_id",
                job_id,
                outcome,
            )
            return

        config = await self._read_config()
        if not config.enabled or not config.bot_token:
            logger.info(
                "plan outcome notify skip: job_id=%s telegram push disabled/no token",
                job_id,
            )
            return

        # Atomic claim + increment after config/target guards so a
        # disabled send does not burn the claim; before the tag send so
        # a crash/send failure cannot leave claimed-but-uncounted.
        claim_kwargs = (
            {"plus_delta": 1} if outcome == "plus" else {"expired_delta": 1}
        )
        claimed, counts = await self._job_manager.claim_and_count_plan_outcome(
            chat_id, job_id, **claim_kwargs
        )
        if not claimed:
            return

        plus_n: int | None = counts[0] if outcome == "plus" and counts else None
        tag = plus_tag_text(plus_n=plus_n) if outcome == "plus" else expired_tag_text()

        try:
            await self._client.send_message_with_reply_to(
                token=config.bot_token,
                chat_id=chat_id,
                text=tag,
                reply_to_message_id=message_id,
            )
            logger.info(
                "plan outcome tag sent: job_id=%s outcome=%s chat_id=%s "
                "message_id=%s plus_n=%s",
                job_id,
                outcome,
                chat_id,
                message_id,
                plus_n,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "plan outcome tag failed: job_id=%s outcome=%s type=%s msg=%s",
                job_id,
                outcome,
                type(exc).__name__,
                exc,
            )

        last_chat_label, _, last_sent_at = _latest_success_notification(record)
        base_caption = self._sent_captions.get(job_id) or build_caption(
            account_line=record.job.account_line,
            finished_at=record.finished_at,
            payment_link=record.payment_link,
            payment_method=record.job.payment_method,
            order=record.order,
            qr_expires_at=record.qr_expires_at,
            chat_label=last_chat_label,
            chat_id=chat_id,
            sent_at=last_sent_at,
        )
        new_caption = append_plan_outcome_status(
            base_caption, outcome, plus_n=plus_n
        )
        try:
            await self._client.edit_message_caption(
                token=config.bot_token,
                chat_id=chat_id,
                message_id=message_id,
                caption=new_caption,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "plan outcome caption edit failed: job_id=%s type=%s msg=%s",
                job_id,
                type(exc).__name__,
                exc,
            )

        await self._push_group_tally(chat_id, config.bot_token)

    async def _persist_new_tally_message_id(
        self, chat_id: str, bot_token: str, new_mid: int
    ) -> None:
        """Durable first-send message-id persist.

        On failure, try to delete the just-sent Telegram message so a
        swallowed persist can never leave mid=None with an orphan that
        would duplicate after restart. Always re-raises when the id is
        not safely stored (or the orphan cannot be removed).
        """
        assert self._job_manager is not None
        try:
            await self._job_manager.persist_tally_message_id(
                chat_id, new_mid, durable=True
            )
        except Exception:
            logger.exception(
                "durable tally message_id persist failed: chat_id=%s mid=%s",
                chat_id,
                new_mid,
            )
            delete = getattr(self._client, "delete_message", None)
            if callable(delete):
                try:
                    await delete(
                        token=bot_token,
                        chat_id=chat_id,
                        message_id=new_mid,
                    )
                    # Deleted → mid stays None in DB; next push resends.
                    return
                except Exception:
                    logger.warning(
                        "failed to delete orphan tally message: "
                        "chat_id=%s mid=%s",
                        chat_id,
                        new_mid,
                    )
            raise

    async def _send_and_persist_tally_message(
        self, chat_id: str, bot_token: str, text: str
    ) -> None:
        """Send a new tally message and durable-persist its message_id."""
        result = await self._client.send_message(
            token=bot_token,
            chat_id=chat_id,
            text=text,
        )
        new_mid = result.get("message_id") if isinstance(result, dict) else None
        if isinstance(new_mid, int):
            await self._persist_new_tally_message_id(chat_id, bot_token, new_mid)

    async def _push_group_tally(self, chat_id: str, bot_token: str) -> None:
        """Gửi/edit 1 tally message per chat: Batch Plus ✅ N ⌛ M."""
        if self._job_manager is None:
            return
        lock = self._tally_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            plus, expired, mid = await self._job_manager.read_batch_tally(chat_id)
            text = build_batch_tally_text(plus, expired=expired)
            try:
                if mid is None:
                    await self._send_and_persist_tally_message(
                        chat_id, bot_token, text
                    )
                else:
                    try:
                        await self._client.edit_message_text(
                            token=bot_token,
                            chat_id=chat_id,
                            message_id=mid,
                            text=text,
                        )
                    except TelegramApiError:
                        await self._send_and_persist_tally_message(
                            chat_id, bot_token, text
                        )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "plan outcome tally failed: chat_id=%s type=%s msg=%s",
                    chat_id,
                    type(exc).__name__,
                    exc,
                )

    async def reset_batch_tally(self) -> dict[str, Any]:
        """Post a permanent period-close receipt per chat, then close.

        Snapshots counts first; decrements each successfully-receipted
        chat by the receipted amount (not absolute zero) so a Plus that
        lands after the snapshot carries into the next period. Chats
        whose receipt send fails are left un-closed and listed in
        `skipped`. Deferred outcomes re-arm only when every non-empty
        chat closed successfully — a partial skip must not re-open
        deferred jobs on un-closed chats for same-period recount.

        Returns:
            `{closed, skipped, plus_total, expired_total}` for the
            API/command echo.
        """
        empty: dict[str, Any] = {
            "closed": [],
            "skipped": [],
            "plus_total": 0,
            "expired_total": 0,
        }
        if self._job_manager is None:
            return empty

        async with self._reset_batch_tally_lock:
            return await self._reset_batch_tally_unlocked(empty)

    async def _reset_batch_tally_unlocked(
        self, empty: dict[str, Any]
    ) -> dict[str, Any]:
        config = await self._read_config()
        if not config.enabled or not config.bot_token:
            logger.info("reset_batch_tally skip: telegram push disabled/no token")
            return empty

        chats = await self._job_manager.list_batch_tally_chats()
        closed: list[str] = []
        skipped: list[str] = []
        plus_total = 0
        expired_total = 0
        reset_at = format_vn_time(time.time())

        for row in chats:
            snap_plus = int(row.plus_count)
            snap_expired = int(row.expired_count)
            if snap_plus == 0 and snap_expired == 0:
                continue

            receipt = build_period_close_text(
                snap_plus, expired=snap_expired, reset_at=reset_at
            )
            receipt_sent = False
            try:
                await self._client.send_message(
                    token=config.bot_token,
                    chat_id=row.chat_id,
                    text=receipt,
                )
                receipt_sent = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "period-close receipt send failed: chat_id=%s type=%s msg=%s",
                    row.chat_id,
                    type(exc).__name__,
                    exc,
                )
                skipped.append(row.chat_id)
                continue

            # Receipt already permanent — close must succeed (one retry)
            # so a later chốt kỳ does not post a second receipt for the
            # same open counters.
            close_error: Exception | None = None
            for attempt in (1, 2):
                try:
                    await self._job_manager.close_batch_period(
                        row.chat_id,
                        plus_receipted=snap_plus,
                        expired_receipted=snap_expired,
                    )
                    close_error = None
                    break
                except Exception as exc:  # noqa: BLE001
                    close_error = exc
                    logger.warning(
                        "period-close DB failed after receipt: "
                        "chat_id=%s attempt=%s type=%s msg=%s",
                        row.chat_id,
                        attempt,
                        type(exc).__name__,
                        exc,
                    )
            if close_error is not None:
                # Receipt already permanent; counters still open so operator
                # can retry close without counting as a clean rearm.
                logger.error(
                    "period-close left un-closed after receipt: "
                    "chat_id=%s receipt_sent=%s",
                    row.chat_id,
                    receipt_sent,
                )
                skipped.append(row.chat_id)
                continue

            closed.append(row.chat_id)
            plus_total += snap_plus
            expired_total += snap_expired

        # Only rearm when the period fully closed. A skipped chat may
        # still hold deferred (flag=2) jobs that must not become
        # claimable again until that chat is receipted.
        if closed and not skipped:
            try:
                await self._job_manager.rearm_deferred_plan_outcomes()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "rearm_deferred_plan_outcomes failed after reset: "
                    "type=%s msg=%s",
                    type(exc).__name__,
                    exc,
                )

        return {
            "closed": closed,
            "skipped": skipped,
            "plus_total": plus_total,
            "expired_total": expired_total,
        }

    async def _track_notification(
        self, job_id: str, entry: dict[str, Any]
    ) -> None:
        """Đẩy entry lên `JobManager.record_telegram_notification`.

        Best-effort: nếu `_job_manager` None (test unit) hoặc method raise
        (DB write fail) → chỉ log warning, KHÔNG propagate. Notify hook đã
        làm xong việc chính (gửi hoặc fail rõ ràng), track history là
        observability phụ.
        """
        if self._job_manager is None:
            return
        try:
            await self._job_manager.record_telegram_notification(job_id, entry)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "record_telegram_notification failed: job_id=%s type=%s msg=%s",
                job_id,
                type(exc).__name__,
                exc,
            )

    async def _mark_send_success(self, send_key: str) -> None:
        async with self._send_state_lock:
            self._queued_send_keys.discard(send_key)
            self._sent_send_keys.add(send_key)

    async def _release_send_reservation(self, send_key: str) -> None:
        async with self._send_state_lock:
            self._queued_send_keys.discard(send_key)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    async def _read_config(self) -> "_TelegramConfig":
        """Đọc live 4 setting và defensive-normalize về `_TelegramConfig`.

        Vì `TypeConstraint.list_object` chỉ validate `required_keys` không
        validate type của value, notifier tự coerce từng field:
            - `chat_id`: bỏ qua nếu không phải str non-empty.
            - `label`: default `""` nếu thiếu/không phải str.
            - `enabled`: default False nếu thiếu/không phải bool.

        `send_photo` default `True` nếu setting chưa từng được set (VD
        DB cũ trước migration, hoặc namespace fail-safe) — giữ hành vi
        cũ (gửi ảnh QR) cho những deployment không biết setting này.
        """
        enabled = await self._settings.get(_KEY_ENABLED)
        bot_token = await self._settings.get(_KEY_BOT_TOKEN)
        send_photo_raw = await self._settings.get(_KEY_SEND_PHOTO)
        chat_targets_raw = await self._settings.get(_KEY_CHAT_TARGETS)
        return _TelegramConfig(
            enabled=bool(enabled),
            bot_token=str(bot_token) if isinstance(bot_token, str) else "",
            send_photo=(
                bool(send_photo_raw) if send_photo_raw is not None else True
            ),
            chat_targets=_normalize_chat_targets(chat_targets_raw),
        )


# ---------------------------------------------------------------------------
# Data holders + helper functions
# ---------------------------------------------------------------------------


class _TelegramConfig:
    """Snapshot 4 setting cho 1 lần notify. Immutable-ish (dataclass đủ
    nhưng giữ class thường để type annotation trong `_read_config` gọn)."""

    __slots__ = ("enabled", "bot_token", "send_photo", "chat_targets")

    def __init__(
        self,
        enabled: bool,
        bot_token: str,
        send_photo: bool,
        chat_targets: list[dict[str, Any]],
    ) -> None:
        self.enabled = enabled
        self.bot_token = bot_token
        self.send_photo = send_photo
        self.chat_targets = chat_targets


def _normalize_chat_targets(raw: Any) -> list[dict[str, Any]]:
    """Coerce giá trị raw từ Settings về list dict shape đã biết.

    Bỏ qua phần tử không phải dict hoặc thiếu `chat_id` non-empty string.
    KHÔNG raise — giữ notifier best-effort.
    """
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        chat_id = item.get("chat_id")
        if not isinstance(chat_id, str) or not chat_id.strip():
            continue
        result.append(
            {
                "chat_id": chat_id.strip(),
                "label": item.get("label", "")
                if isinstance(item.get("label"), str)
                else "",
                "enabled": bool(item.get("enabled", False)),
            }
        )
    return result


def _filter_active_chat_ids(chat_targets: list[dict[str, Any]]) -> list[str]:
    """Extract chat_id của các target `enabled=True`, preserve thứ tự.

    Thứ tự quyết định thứ tự round-robin — user sắp xếp lại trong UI
    (VD kéo thả) sẽ ảnh hưởng cách phân bổ. Chấp nhận: user thay đổi
    thứ tự là hành động chủ động, cursor không tự reset (giữ cursor cũ
    modulo với len mới), nếu muốn phân bổ đúng lại từ đầu thì bấm nút
    "Reset round" của UI.
    """
    return [t["chat_id"] for t in chat_targets if t.get("enabled") is True]


def _resolve_chat_label(
    chat_targets: list[dict[str, Any]], chat_id: str
) -> str:
    """Tìm label do user đặt cho `chat_id`. Fallback về `""` khi không có.

    Chấp nhận label rỗng — FE tự fallback hiển thị `chat_id` nếu label
    trống. KHÔNG dùng `chat_id` làm label default ở đây để phân biệt rõ
    "user chưa đặt label" (empty) vs "user đặt label = chat_id" (giá
    trị thật).
    """
    for t in chat_targets:
        if t.get("chat_id") == chat_id:
            label = t.get("label", "")
            return label if isinstance(label, str) else ""
    return ""


def _notification_send_key(record: "_JobRecord") -> str:
    """Stable key cho đúng một QR send.

    Dùng thêm artifact/link/expiry để một job rerun tạo QR mới không bị khóa
    bởi QR cũ, nhưng cùng một QR bị hook gọi lặp vẫn bị dedup.
    """
    return "|".join(
        (
            str(record.job.job_id),
            str(record.artifact_path or ""),
            str(record.payment_link or ""),
            str(record.qr_expires_at or ""),
        )
    )


def _has_successful_qr_notification(record: "_JobRecord") -> bool:
    """True nếu record đã có message QR gửi thành công."""
    if record.telegram_message_id is not None:
        return True
    for entry in record.telegram_notifications:
        if entry.get("success") is True and entry.get("message_id") is not None:
            return True
    return False


def _latest_success_notification(
    record: "_JobRecord",
) -> tuple[str | None, str | None, float | None]:
    """Best-effort metadata for the latest successful Telegram QR send."""
    for entry in reversed(record.telegram_notifications):
        if entry.get("success") is not True:
            continue
        chat_label = entry.get("chat_label")
        chat_id = entry.get("chat_id")
        sent_at = entry.get("sent_at")
        return (
            str(chat_label) if isinstance(chat_label, str) and chat_label else None,
            str(chat_id) if isinstance(chat_id, str) and chat_id else None,
            float(sent_at) if isinstance(sent_at, (int, float)) else None,
        )
    return None, None, None
