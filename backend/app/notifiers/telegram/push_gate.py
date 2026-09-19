"""Push_Success_Gate — chặn scheduler chạy job Push_Mode sau N lần gửi Telegram thành công.

Ngữ cảnh: người quét (worker human) cần thời gian scan xong batch QR đã
gửi trước khi nhận batch mới. Feature này cho phép user cấu hình ngưỡng
N; khi Notifier đã gửi ĐỦ N lần Push_Mode thành công (retry vẫn tính lần
cuối), gate tự set `paused=True` và Scheduler bỏ qua các job Push đang
chờ trong `_pending_order`. Job Pull_Mode KHÔNG bị gate này chặn (worker
Telegram_Worker tự quyết định nhận job qua nút "Nhận job").

State in-memory (chấp nhận reset khi restart, cùng semantic với
`RoundRobinDistributor.cursor`):
    - `_counter`: số lần Notifier gửi thành công Push_Mode kể từ resume
      gần nhất. Reset về 0 mỗi lần user bấm "Tiếp tục".
    - `_paused`: True khi `_counter >= threshold` (đọc setting live tại
      thời điểm on_success_sent).

Setting đọc live mỗi lần `on_success_sent`:
    - `telegram.push_mode.success_wait.enabled` (bool)
    - `telegram.push_mode.success_wait.threshold` (int 1..1000)

Threshold đổi giữa lúc đếm dở (VD 5/10 → threshold=3): chỉ ảnh hưởng
LẦN tăng counter tiếp theo — giữ counter hiện tại, KHÔNG tự pause ngay
dù counter đã ≥ threshold mới. Design decision: predictable, tránh
"pause bất ngờ" do user vô ý nhập số nhỏ.

Concurrency: 1 `asyncio.Lock` bảo vệ read-modify-write của `_counter` +
`_paused`. TelegramNotifier gọi `on_success_sent` trong worker task nền
tuần tự (1 item/1 lúc — xem `_worker_loop`), race gần như không có,
nhưng vẫn giữ lock để đảm bảo endpoint `resume` không xung đột với
Notifier đang tăng counter.

Wake callback: khi resume, gate cần đánh thức JobManager scheduler (đang
chờ `_new_job_event` khi queue chỉ còn push job và gate paused). Wake
callback là sync — chỉ set 1 event, KHÔNG được I/O. JobManager truyền
`self.wake_scheduler` (sync method gọi `_new_job_event.set()`).

Boundary: gate KHÔNG import `JobManager` (Payment_Module_Boundary — module
`notifiers/` không được import ngược lên `core/`). Wiring 2 chiều
(Notifier → gate → JobManager wake) đi qua callback do `bootstrap.py`
inject.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from app.core.settings_store import SettingsRepository
    from app.core.sse import SseBroadcaster


logger = logging.getLogger(__name__)


_KEY_ENABLED = "telegram.push_mode.success_wait.enabled"
_KEY_THRESHOLD = "telegram.push_mode.success_wait.threshold"

#: Fallback threshold khi setting corrupt/missing — chỉ dùng nếu default
#: seed trong `register_telegram_namespace` thất bại. Giữ giống seed default
#: (10) để không đổi ngữ nghĩa mặc định. Nếu ngày nào đổi seed, đổi cả
#: constant này để consistent.
_DEFAULT_THRESHOLD_FALLBACK: int = 10


def _coerce_threshold(raw: Any) -> int:
    """Ép `raw` (từ Settings.get) về int ≥ 1 hợp lệ.

    Nguyên tắc:
        - `type(raw) is int` (KHÔNG dùng `isinstance` — chặn `bool` vì
          `bool` là subclass `int` trong Python, `isinstance(True, int)
          == True` sẽ nhận True/False lọt qua).
        - `raw < 1` → clamp về 1 (backend đã validate `min=1` khi bulk_set
          setting, nhưng defensive nếu setting DB corrupt).
        - Type sai (None, str, float, bool...) → log warning + fallback
          `_DEFAULT_THRESHOLD_FALLBACK`. KHÔNG raise vì on_success_sent
          không được vỡ (làm tắt notifier); log để dev thấy được vấn đề
          tầng seed setting.
    """
    if type(raw) is int and raw >= 1:  # noqa: E721 — muốn chặn bool
        return raw
    if type(raw) is int:  # noqa: E721 — raw < 1
        logger.warning(
            "push_gate: threshold=%d < 1 — clamp về 1 (setting corrupt?)",
            raw,
        )
        return 1
    logger.warning(
        "push_gate: threshold không phải int (type=%s value=%r) — "
        "fallback về %d. Kiểm tra key %s trong DB Settings.",
        type(raw).__name__,
        raw,
        _DEFAULT_THRESHOLD_FALLBACK,
        _KEY_THRESHOLD,
    )
    return _DEFAULT_THRESHOLD_FALLBACK


class PushSuccessGate:
    """Gate đếm success + broadcast state qua SSE.

    Attributes:
        _settings: đọc live 2 key (enabled/threshold) mỗi lần
            `on_success_sent`.
        _sse: broadcast `push_gate_updated` event khi state đổi.
        _wake_scheduler: callback sync do JobManager inject để wake
            scheduler khi resume. `None` nếu chưa wire (test unit).
        _counter: số lần gửi Push thành công kể từ resume gần nhất.
        _paused: True khi counter ≥ threshold (áp dụng cho lần tăng
            gây tràn — không auto re-check khi user đổi threshold).
    """

    def __init__(
        self,
        settings: "SettingsRepository",
        sse: "SseBroadcaster",
    ) -> None:
        self._settings = settings
        self._sse = sse
        self._wake_scheduler: Callable[[], None] | None = None
        # `_pause_running_push_jobs`: callback SYNC do JobManager cung cấp,
        # gate gọi 1 lần TẠI THỜI ĐIỂM hit threshold (transition
        # `not paused → paused`). Callback set cờ pause trên
        # cancellation_token của mọi job Push_Mode đang RUNNING, khiến
        # chúng dừng sớm sau checkpoint an toàn và về PENDING chờ user
        # resume. Xem `JobManager.signal_pause_running_push_jobs`.
        self._pause_running_push_jobs: Callable[[], None] | None = None
        # `_wake_worker`: callback SYNC do `TelegramNotifier` inject để
        # đánh thức worker gửi Telegram khi user bấm "Tiếp tục". Worker
        # đứng chờ (asyncio.Event) trước mỗi item nếu gate paused; resume
        # cần đánh thức để worker tiếp tục drain queue. Sync (chỉ set
        # 1 event) — an toàn gọi trong khi giữ lock.
        self._wake_worker: Callable[[], None] | None = None
        self._counter: int = 0
        self._paused: bool = False
        self._lock: asyncio.Lock = asyncio.Lock()

    def set_wake_scheduler(self, callback: Callable[[], None]) -> None:
        """Inject callback SYNC để wake JobManager scheduler khi resume.

        Được `bootstrap.py` gọi 1 lần sau khi tạo JobManager. Callback
        phải sync (chỉ set 1 `asyncio.Event`), KHÔNG được I/O — gate
        gọi trong context giữ lock.
        """
        self._wake_scheduler = callback

    def set_pause_running_push_jobs_callback(
        self, callback: Callable[[], None]
    ) -> None:
        """Inject callback SYNC set cờ pause cho job Push_Mode đang RUNNING.

        Được `bootstrap.py` gọi 1 lần sau khi tạo JobManager. Gate gọi
        callback DUY NHẤT 1 lần tại thời điểm transition `paused=False
        → True` trong `on_success_sent`. Callback phải sync (chỉ iterate
        `_jobs` và set token pause event), KHÔNG được I/O — gate gọi
        NGOÀI lock để không block các call `on_success_sent` khác.
        """
        self._pause_running_push_jobs = callback

    def set_wake_worker_callback(self, callback: Callable[[], None]) -> None:
        """Inject callback SYNC để wake worker Telegram khi resume.

        `TelegramNotifier._worker_loop` chờ trên `asyncio.Event` khi gate
        paused (trước mỗi lần gửi item). `resume()` gọi callback này để
        set event → worker wake → tiếp tục drain queue. Sync, chỉ set 1
        event, an toàn gọi trong context giữ lock.

        Nếu chưa wire (test unit), `resume()` skip bước wake worker —
        không phá vỡ hành vi hiện tại.
        """
        self._wake_worker = callback

    def is_paused(self) -> bool:
        """Snapshot sync `_paused` — dùng cho JobManager scheduler decision.

        KHÔNG lock — đọc `bool` là atomic trong CPython, race chỉ dẫn
        tới scheduler quyết định sai 1 tick (chờ 1 event kế tiếp), không
        gây corruption state.
        """
        return self._paused

    async def snapshot(self) -> dict[str, Any]:
        """Trả state đầy đủ để API/SSE gửi cho FE.

        Đọc live setting để user thấy giá trị đúng (kể cả khi vừa đổi
        threshold qua Settings API mà chưa có event push_gate_updated).
        """
        enabled = await self._settings.get(_KEY_ENABLED)
        threshold_raw = await self._settings.get(_KEY_THRESHOLD)
        return {
            "enabled": bool(enabled),
            "threshold": _coerce_threshold(threshold_raw),
            "counter": self._counter,
            "paused": self._paused,
        }

    async def on_success_sent(self) -> None:
        """Notifier gọi sau mỗi lần gửi Telegram Push thành công.

        Semantic (hard-cap, không overshoot):
            - `enabled=False` → no-op (không tăng counter, không đụng paused).
            - Đã `paused=True` → no-op TUYỆT ĐỐI (không tăng counter,
              không broadcast). Layer defense-in-depth: kết hợp với
              worker check `is_paused()` trước khi gửi ở `_worker_loop`,
              gate GUARANTEED counter không vượt threshold.
            - `enabled=True and not paused` → `counter += 1`; nếu chạm
              threshold → `paused=True`, gọi callback pause các job RUNNING.
            - Broadcast SSE CHỈ khi counter đổi (tránh spam FE khi
              worker drain queue lớn — mỗi tick 1 event `push_gate_updated`
              có thể lấp queue SSE per-client `maxsize=100` gây drop
              `job_status` event).

        Fail-safe: exception khi đọc setting hoặc broadcast SSE được
        swallow (log warning) — gate là observability, không được làm
        vỡ pipeline gửi Telegram (`_send_with_retry` đã hoàn tất).
        """
        try:
            enabled_raw = await self._settings.get(_KEY_ENABLED)
            threshold_raw = await self._settings.get(_KEY_THRESHOLD)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "push_gate: đọc setting fail — bỏ qua tick: %s: %s",
                type(exc).__name__,
                exc,
            )
            return

        if not bool(enabled_raw):
            return
        threshold = _coerce_threshold(threshold_raw)

        pause_just_triggered = False
        async with self._lock:
            # HARD-CAP: nếu đã paused, KHÔNG tăng counter và KHÔNG broadcast.
            # Đây là fallback phòng khi `_worker_loop` bỏ sót check
            # `is_paused()` (VD race giữa transition paused ở tick N và
            # worker đã lấy item N+1 khỏi queue). Không có nhánh này,
            # UI sẽ thấy "55/50", "60/50" như bug user report.
            if self._paused:
                return
            self._counter += 1
            if self._counter >= threshold:
                self._paused = True
                pause_just_triggered = True
                logger.info(
                    "push_gate: PAUSED — counter=%d >= threshold=%d",
                    self._counter,
                    threshold,
                )
            snapshot = {
                "enabled": True,
                "threshold": threshold,
                "counter": self._counter,
                "paused": self._paused,
            }

        # Set cờ pause cho các job Push_Mode đang RUNNING — CHỈ gọi tại
        # thời điểm transition `paused=False → True` (không mỗi lần tăng
        # counter). Callback sync, chạy nhanh (iterate `_jobs` set event),
        # gọi NGOÀI lock để không giữ lock lâu hơn cần thiết.
        if pause_just_triggered and self._pause_running_push_jobs is not None:
            try:
                self._pause_running_push_jobs()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "push_gate: pause_running_push_jobs callback raise "
                    "(bỏ qua): %s: %s",
                    type(exc).__name__,
                    exc,
                )

        await self._broadcast(snapshot)

    async def resume(self) -> dict[str, Any]:
        """Reset counter = 0, paused = False, broadcast SSE, wake scheduler.

        Idempotent: gọi khi gate đã ở state (0, False) là no-op broadcast
        (FE sync lại state cũng OK).

        Returns:
            State mới sau reset — endpoint dùng làm response body.
        """
        async with self._lock:
            self._counter = 0
            self._paused = False
            # Đọc lại setting để trả cho FE đúng snapshot hiện tại.
            enabled_raw = await self._settings.get(_KEY_ENABLED)
            threshold_raw = await self._settings.get(_KEY_THRESHOLD)
            snapshot = {
                "enabled": bool(enabled_raw),
                "threshold": _coerce_threshold(threshold_raw),
                "counter": 0,
                "paused": False,
            }

        logger.info("push_gate: RESUMED — counter reset to 0")

        # Wake scheduler + worker Telegram NGOÀI lock — cả 2 callback đều
        # sync (chỉ set 1 asyncio.Event), nhưng vẫn gọi ngoài lock để
        # tuân pattern "không gọi callback ngoài trong khi giữ lock".
        if self._wake_scheduler is not None:
            try:
                self._wake_scheduler()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "push_gate: wake_scheduler callback raise (bỏ qua): "
                    "%s: %s",
                    type(exc).__name__,
                    exc,
                )
        if self._wake_worker is not None:
            try:
                self._wake_worker()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "push_gate: wake_worker callback raise (bỏ qua): "
                    "%s: %s",
                    type(exc).__name__,
                    exc,
                )

        await self._broadcast(snapshot)
        return snapshot

    async def _broadcast(self, snapshot: dict[str, Any]) -> None:
        """Broadcast qua SSE — best-effort, exception swallow tại đây."""
        try:
            await self._sse.broadcast_push_gate_updated(snapshot)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "push_gate: broadcast SSE fail (bỏ qua): %s: %s",
                type(exc).__name__,
                exc,
            )
