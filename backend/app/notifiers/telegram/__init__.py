"""Điểm tích hợp DUY NHẤT giữa `core/` và `notifiers/telegram/`.

Module này export 2 hàm public để `bootstrap.py` gọi tại startup, theo cùng
pattern với `payments/ideal/__init__.py` (Payment_Module_Boundary applied to
notifier):

    - `register_telegram_namespace(settings, engine)`: migrate 2 key cũ
      `telegram.enabled`/`telegram.chat_targets` (nếu còn tồn tại từ bản
      trước) sang namespace mới, rồi đăng ký 8 key `telegram.*` vào
      `SettingsRepository` (whitelist bằng TypeConstraint) + seed default.

    - `register_telegram_notifier(job_manager, settings, sse, client=None)`:
      tạo `TelegramNotifier` với `TelegramBotClient` (dùng chung `client`
      nếu caller truyền vào, tránh mở 2 session HTTP riêng cho Push_Mode
      và Pull_Mode) + `RoundRobinDistributor` (in-memory) + `PushSuccessGate`
      (feature "success wait"), và đăng ký terminal hook vào `JobManager`.
      Gate được inject vào notifier qua constructor; wiring 2 chiều với
      `JobManager` do `bootstrap.py` thực hiện qua `register_push_pause_checker`
      + `PushSuccessGate.set_wake_scheduler`.

    - `register_pull_mode_infrastructure(job_manager, settings, client=None)`
      (task 32 — Migration Strategy điểm 5 design.md): khởi tạo
      `PollingSupervisor` + `PullJobCoordinator` + `PullJobNotifier`, đăng
      ký `PullJobNotifier.notify_pull_qr_ready` qua
      `job_manager.register_terminal_hook`,
      `PullJobNotifier.notify_pull_error` qua
      `job_manager.register_pull_error_hook`, và
      `PullJobNotifier.notify_mode_switch_cancelled` qua
      `job_manager.register_mode_switch_notify_hook` (gỡ nút QR +
      báo worker job bị hủy khi force đổi mode, R3.2 d/e). KHÔNG tự đăng ký hook dọn
      `_pull_job_locks` — hook đó đã được `JobManager.__init__` tự đăng ký
      nội bộ (task 12), không cần bootstrap wiring lặp lại.

Cả 3 hàm nhận singleton `core/` qua PARAMETER — module KHÔNG tự resolve
global state, KHÔNG có import-time side-effect. `bootstrap.py` gọi các hàm
này sau khi `JobManager` khởi tạo xong.

Không có Fail_Fast tại `register_telegram_namespace` nếu Telegram bị disable
(bot_token rỗng) — namespace/whitelist vẫn được đăng ký để user cấu hình
qua Settings API. Chỉ khi `telegram.push_mode.enabled=True` VÀ có
`bot_token`, hook Push_Mode mới thực sự gửi request.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final, TYPE_CHECKING

from app.core.db import DbEngine
from app.core.job_manager import JobManager
from app.core.settings_store import SettingsRepository, TypeConstraint
from app.notifiers.telegram import callback_router, commands
from app.notifiers.telegram.client import TelegramBotClient
from app.notifiers.telegram.distributor import RoundRobinDistributor
from app.notifiers.telegram.live_qr_gate import LiveQrGate
from app.notifiers.telegram.notifier import TelegramNotifier
from app.notifiers.telegram.polling import CallbackDedupeCache, PollingSupervisor
from app.notifiers.telegram.pull_mode import PullJobCoordinator
from app.notifiers.telegram.pull_notifier import PullJobNotifier
from app.notifiers.telegram.push_gate import PushSuccessGate
from app.notifiers.telegram.status_card import WorkerStatusCardTracker

if TYPE_CHECKING:
    from app.core.sse import SseBroadcaster

__all__ = [
    "register_telegram_namespace",
    "register_telegram_notifier",
    "register_pull_mode_infrastructure",
    "TelegramNotifier",
    "TelegramBotClient",
    "RoundRobinDistributor",
    "PullJobCoordinator",
    "PullJobNotifier",
    "PollingSupervisor",
    "PushSuccessGate",
    "LiveQrGate",
    "WorkerStatusCardTracker",
]


_NAMESPACE: Final[str] = "telegram"


#: Map key cũ (bản trước khi có Pull_Mode) → key mới tương ứng trong
#: namespace `telegram.push_mode.*`. Dùng bởi `_migrate_legacy_telegram_keys`
#: — chỉ 2 key này từng tồn tại ở bản cũ (R2.6, R2.7).
_LEGACY_KEY_MAP: Final[dict[str, str]] = {
    "telegram.enabled": "telegram.push_mode.enabled",
    "telegram.chat_targets": "telegram.push_mode.chat_targets",
}

#: Upsert SQL cho migration — cùng pattern với `_UPSERT_SETTING_SQL` của
#: `settings_store.py`, khai báo riêng ở đây (không import symbol private
#: xuyên module) vì migration chạy raw SQL TRƯỚC khi namespace mới được
#: đăng ký (không thể đi qua `SettingsRepository.set` — whitelist đã đổi).
_UPSERT_LEGACY_MIGRATION_SQL = """
INSERT INTO settings (key, value, updated_at)
VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
ON CONFLICT(key) DO UPDATE SET
    value = excluded.value,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now');
"""


#: Type constraint cho namespace `telegram.*` (whitelist R11.7 pattern),
#: tái cấu trúc theo §4.3 design.md (Requirement 2) để hỗ trợ song song
#: Push_Mode và Pull_Mode:
#:
#:  - `bot_token` (cấp gốc, dùng chung 2 mode): `type="string"` KHÔNG có
#:    enum/pattern. Format thực tế của Telegram bot token là
#:    `<int_bot_id>:<35_char_base64url>` nhưng không hardcode regex ở
#:    constraint layer — giữ generic, để `TelegramBotClient` tự fail khi
#:    gọi API nếu token sai format (Telegram trả 401 `Unauthorized`, dễ
#:    debug hơn regex reject sớm).
#:  - `mode` (cấp gốc): `"push"` hoặc `"pull"` — quyết định luồng nào đang
#:    active. Default `"push"` để giữ hành vi hiện có cho user chưa từng
#:    cấu hình.
#:  - `polling_enabled`/`polling_offset` (cấp gốc): điều khiển
#:    `PollingSupervisor` (long-polling Telegram `getUpdates`) — offset lưu
#:    cursor cuối cùng đã xử lý để resume sau restart.
#:  - `push_mode.enabled`: mặc định `False` để user CHỦ ĐỘNG bật sau khi
#:    cấu hình token+chat. Tránh case: token cũ còn trong DB, user cài lại
#:    bot mới → notify sai chat.
#:  - `push_mode.send_photo`: mặc định `True` giữ hành vi cũ (gửi ảnh QR
#:    kèm caption qua `sendPhoto`). Đặt `False` → notifier gửi TEXT-ONLY
#:    qua `sendMessage` (cùng nội dung caption: email masked + giờ VN +
#:    link thanh toán), KHÔNG kèm ảnh — dùng cho user chỉ cần link, không
#:    cần ảnh QR đè lên chat. Setting này CHỈ ảnh hưởng Push_Mode; Pull_Mode
#:    luôn gửi ảnh vì worker cần scan QR.
#:  - `push_mode.chat_targets` — mỗi phần tử `{chat_id, label, enabled}`:
#:    * `chat_id` (string): Telegram chat_id thực tế là int (negative cho
#:      group/channel) nhưng lưu string để tránh integer overflow ở JS
#:      (chat_id supergroup dạng `-1001234567890` vượt Number.MAX_SAFE_INTEGER).
#:      Backend pass thẳng qua Telegram API, không parse int.
#:    * `label` (string): tên do user đặt để dễ nhớ (VD "Sếp A", "Nhóm ops")
#:      — chỉ dùng cho UI, KHÔNG gửi qua Telegram API.
#:    * `enabled` (bool): flag bật/tắt riêng cho từng chat. Round-robin
#:      chỉ chạy trên subset `enabled=True` — cho phép user tạm tắt 1
#:      chat mà không cần xóa khỏi list (giữ chat_id + label để sau bật
#:      lại).
#:    `item_schema.required_keys` validate shape (đủ 3 key) — validate
#:    type của từng field do notifier tự defensive check khi đọc.
#:  - `pull_mode.max_concurrent_jobs_per_user`: giới hạn số job
#:    assigned+pending đồng thời của 1 Telegram_Worker (R22.2).
#:  - `pull_mode.allowed_chat_ids`: whitelist chat_id được phép nhận job
#:    Pull_Mode (list string, cùng lý do giữ string như `chat_id` ở trên).
#:  - `pull_mode.admin_user_ids`: Telegram user ids allowed to run
#:    `/chotky` (period-close). Empty list refuses everyone.
_TELEGRAM_KEY_CONSTRAINTS: Final[dict[str, TypeConstraint]] = {
    "bot_token": TypeConstraint(type="string", min=0, max=200),
    "mode": TypeConstraint(type="string", enum=["push", "pull"]),
    "polling_enabled": TypeConstraint(type="bool"),
    "polling_offset": TypeConstraint(type="int", min=0),
    "push_mode.enabled": TypeConstraint(type="bool"),
    "push_mode.send_photo": TypeConstraint(type="bool"),
    "push_mode.chat_targets": TypeConstraint(
        type="list_object",
        min=0,
        max=200,
        item_schema={"required_keys": ["chat_id", "label", "enabled"]},
    ),
    # Push_Success_Gate (feature "success wait"): sau N lần gửi Telegram
    # Push thành công, scheduler dừng cấp slot cho job Push_Mode để người
    # quét có thời gian xử lý batch QR đã gửi. `enabled=False` mặc định
    # để giữ hành vi cũ (không tự pause) cho user chưa từng cấu hình.
    # `threshold=10` là giá trị người quét đề xuất — batch cỡ này vừa tay,
    # không quá nhỏ (pause liên tục) hoặc quá lớn (mất kiểm soát).
    "push_mode.success_wait.enabled": TypeConstraint(type="bool"),
    "push_mode.success_wait.threshold": TypeConstraint(
        type="int", min=1, max=1000
    ),
    # Live QR Gate (rolling-window per-chat cap on concurrent live QRs).
    # Mutex with success_wait enforced at settings route layer (auto-clear).
    # Default off; max_per_chat=5 (1..50). Release: plus|timeout|lifecycle|TTL.
    "push_mode.live_qr.enabled": TypeConstraint(type="bool"),
    "push_mode.live_qr.max_per_chat": TypeConstraint(
        type="int", min=1, max=50
    ),
    "pull_mode.max_concurrent_jobs_per_user": TypeConstraint(
        type="int", min=1, max=20
    ),
    "pull_mode.allowed_chat_ids": TypeConstraint(type="list_str", min=0, max=500),
    "pull_mode.admin_user_ids": TypeConstraint(type="list_str", min=0, max=500),
}


async def _migrate_legacy_telegram_keys(engine: DbEngine) -> None:
    """Migrate 2 key cũ `telegram.enabled`/`telegram.chat_targets` (bản
    trước khi có Pull_Mode) sang key mới tương ứng trong
    `telegram.push_mode.*`, rồi xoá row cũ khỏi bảng `settings`.

    Chạy bằng RAW SQL (không qua `SettingsRepository.get/set`) vì hàm này
    PHẢI chạy TRƯỚC `settings.register_namespace("telegram", ...)` — tại
    thời điểm đó whitelist mới chưa tồn tại nên `SettingsRepository` sẽ
    reject cả key cũ (không còn trong whitelist) lẫn key mới (namespace
    chưa đăng ký).

    Idempotent (Requirement 2.10): nếu row key cũ không tồn tại (đã migrate
    từ lần chạy trước, hoặc DB mới hoàn toàn) → bỏ qua, KHÔNG raise. Nếu
    key mới đã có giá trị (user đã cấu hình lại, hoặc migration lần trước
    đã chạy) → KHÔNG ghi đè, chỉ xoá row cũ.

    Fail-fast (Requirement 2.10): không try/except quanh I/O — lỗi SQLite
    (disk full, DB corrupt, ...) propagate thẳng lên caller (`bootstrap_services`),
    dừng startup thay vì khởi động với state Settings không xác định.

    Args:
        engine: `DbEngine` dùng chung của Backend_Service — lấy connection
            qua `engine.get_connection()`, cùng pattern với
            `SettingsRepository`.
    """
    connection = await engine.get_connection()

    legacy_keys = list(_LEGACY_KEY_MAP.keys())
    placeholders = ",".join("?" for _ in legacy_keys)
    async with connection.execute(
        f"SELECT key, value FROM settings WHERE key IN ({placeholders});",
        legacy_keys,
    ) as cursor:
        legacy_rows = await cursor.fetchall()

    if not legacy_rows:
        return

    for legacy_key, legacy_value in legacy_rows:
        new_key = _LEGACY_KEY_MAP[legacy_key]
        async with connection.execute(
            "SELECT 1 FROM settings WHERE key = ?;", (new_key,)
        ) as cursor:
            new_key_already_has_value = await cursor.fetchone() is not None

        if not new_key_already_has_value:
            await connection.execute(
                _UPSERT_LEGACY_MIGRATION_SQL, (new_key, legacy_value)
            )

    await connection.execute(
        f"DELETE FROM settings WHERE key IN ({placeholders});", legacy_keys
    )
    await connection.commit()


async def register_telegram_namespace(
    settings: SettingsRepository, engine: DbEngine
) -> None:
    """Migrate key cũ, đăng ký namespace `telegram.*` mới + seed defaults.

    Thứ tự (Requirement 2.6, 2.7, 2.8, 2.10):
        1. `_migrate_legacy_telegram_keys(engine)` — copy key cũ sang key
           mới TRƯỚC khi whitelist đổi (raw SQL, xem docstring hàm đó).
        2. `settings.register_namespace(...)` — đăng ký 8 key `telegram.*`
           mới (§4.3 design.md).
        3. Seed default cho key CHƯA từng được set (giá trị `None`) —
           KHÔNG đè giá trị đã migrate ở bước 1 hoặc giá trị user chủ động
           thay đổi.

    Raises:
        NamespaceAlreadyRegisteredError: `telegram` đã đăng ký từ trước.
    """
    await _migrate_legacy_telegram_keys(engine)

    settings.register_namespace(_NAMESPACE, _TELEGRAM_KEY_CONSTRAINTS)

    _defaults: dict[str, object] = {
        "telegram.bot_token": "",
        "telegram.mode": "push",
        "telegram.polling_enabled": False,
        "telegram.polling_offset": 0,
        "telegram.push_mode.enabled": False,
        "telegram.push_mode.send_photo": True,
        "telegram.push_mode.chat_targets": [],
        "telegram.push_mode.success_wait.enabled": False,
        "telegram.push_mode.success_wait.threshold": 10,
        "telegram.push_mode.live_qr.enabled": False,
        "telegram.push_mode.live_qr.max_per_chat": 5,
        "telegram.pull_mode.max_concurrent_jobs_per_user": 1,
        "telegram.pull_mode.allowed_chat_ids": [],
        "telegram.pull_mode.admin_user_ids": [],
    }
    for key, default_value in _defaults.items():
        if await settings.get(key) is None:
            await settings.set(key, default_value)


def register_telegram_notifier(
    job_manager: JobManager,
    settings: SettingsRepository,
    sse: "SseBroadcaster",
    client: TelegramBotClient | None = None,
) -> TelegramNotifier:
    """Tạo `TelegramNotifier` + `PushSuccessGate` + đăng ký hook vào `JobManager`.

    Được `bootstrap.py` gọi 1 lần tại startup, SAU `register_telegram_namespace`
    (notifier đọc `telegram.*` live tại mỗi call → namespace phải whitelist
    trước).

    Notifier state:
        - `TelegramBotClient`: curl_cffi AsyncSession share cho mọi request
          Telegram API — 1 kết nối pool cho toàn hệ thống. Timeout đủ dài
          (15s) vì sendPhoto upload PNG có thể chậm trên mạng chậm.
          Nếu caller (`bootstrap.py`) truyền sẵn `client` — dùng lại
          instance đó để Push_Mode và Pull_Mode chia sẻ CÙNG 1 session
          HTTP (ít connection hơn); nếu không truyền (VD test/CLI gọi
          trực tiếp), tự construct instance riêng — giữ nguyên hành vi
          cũ, không breaking caller hiện có.
        - `RoundRobinDistributor`: cursor in-memory (int counter) — reset
          khi restart backend. Chấp nhận theo yêu cầu user (semantic
          "phân bổ đều theo thời gian dài" vẫn giữ, mất fairness ngắn
          hạn sau restart chấp nhận được).
        - `PushSuccessGate`: bộ đếm success + trạng thái paused; wiring
          2 chiều với JobManager qua callback `set_wake_scheduler` +
          `register_push_pause_checker` (xem `bootstrap.py`).

    Trả về notifier để test/CLI có thể tương tác trực tiếp (VD gọi
    `notifier.send_test(chat_id)` cho endpoint `/api/notifications/
    telegram/test`). Truy cập gate qua `notifier.push_gate` (public
    attribute) — endpoint routes_notifications gọi
    `notifier.push_gate.snapshot()` / `.resume()`.
    """
    client = client or TelegramBotClient()
    distributor = RoundRobinDistributor()
    push_gate = PushSuccessGate(settings=settings, sse=sse)
    live_qr_gate = LiveQrGate(settings=settings, sse=sse)
    notifier = TelegramNotifier(
        settings=settings,
        client=client,
        distributor=distributor,
        # Pass để `notify_qr_ready` ghi lại `telegram_notifications` vào
        # `_JobRecord` (persist + broadcast SSE `job_notified`).
        job_manager=job_manager,
        push_gate=push_gate,
        live_qr_gate=live_qr_gate,
    )
    job_manager.register_terminal_hook(notifier.notify_qr_ready)
    # Khi auto-check / manual Check Plus xác nhận plan=plus → tag QR
    # worker + tally Batch Plus (giống gpt_signup_hybrid).
    job_manager.register_plus_verified_hook(notifier.notify_plus_verified)
    # Auto-poll 5 phút hết mà chưa plus → tag ⌛ + tally expired.
    job_manager.register_plan_check_timeout_hook(
        notifier.notify_plan_check_timeout
    )
    # Live QR slots: free on job remove + stop/rerun/delete (poll cancel).
    job_manager.register_job_removed_hook(notifier.schedule_release_live_slots)
    job_manager.register_live_slot_release_hook(
        notifier.schedule_release_live_slots
    )
    return notifier


async def _get_bot_token(settings: SettingsRepository) -> str:
    """Đọc `telegram.bot_token` live từ Settings — dùng làm
    `bot_token_getter` cho `PullJobCoordinator`/`PullJobNotifier` (2 class
    này gọi lại hàm này MỖI LẦN cần token vì `bot_token` có thể đổi live
    qua Settings API trong lúc backend đang chạy).

    Fallback rỗng nếu chưa cấu hình (`None`) — để `TelegramBotClient` tự
    fail ở tầng gọi API (Telegram trả 401) thay vì raise sớm ở đây.
    """
    raw_token = await settings.get("telegram.bot_token")
    return raw_token if isinstance(raw_token, str) else ""


def register_pull_mode_infrastructure(
    job_manager: JobManager,
    settings: SettingsRepository,
    client: TelegramBotClient | None = None,
    reset_batch_tally_hook: Callable[[], Awaitable[dict]] | None = None,
) -> tuple[PullJobCoordinator, PullJobNotifier, PollingSupervisor]:
    """Khởi tạo toàn bộ hạ tầng Pull_Mode (task 32 — Migration Strategy
    điểm 5 design.md).

    Được `bootstrap.py` gọi 1 lần tại startup, SAU `register_telegram_namespace`
    (namespace `telegram.*` phải whitelist trước vì `PollingSupervisor`/
    `PullJobCoordinator` đọc `telegram.polling_enabled`/`telegram.bot_token`/
    `telegram.mode`/`telegram.pull_mode.*` live).

    Thứ tự dựng:
        1. `client = client or TelegramBotClient()` — dùng chung instance
           với `register_telegram_notifier` nếu caller truyền vào (xem
           docstring hàm đó).
        2. `bot_token_getter` — closure đọc `telegram.bot_token` live qua
           `_get_bot_token(settings)` mỗi lần được gọi (token có thể đổi
           live qua Settings API).
        3. `PullJobCoordinator(job_manager, client, settings, bot_token_getter,
           reset_batch_tally_hook=...)` — hook dùng cho confirm `/chotky`.
        4. `PullJobNotifier(job_manager, client, bot_token_getter)` — đăng
           ký `notify_pull_qr_ready` qua `register_terminal_hook`,
           `notify_pull_error` qua `register_pull_error_hook`, VÀ
           `notify_mode_switch_cancelled` qua
           `register_mode_switch_notify_hook` (R3.2 d/e — gỡ nút QR +
           báo worker job bị hủy do đổi Operating_Mode, best-effort R3.6).
        5. `CallbackDedupeCache()` — LRU 500 callback_query.id gần nhất
           (default maxsize, xem `polling.py`).
        6. `on_message`/`on_callback_query` — closure resolve `bot_token`
           live NGAY TRƯỚC MỖI LẦN gọi `commands.handle_message`/
           `callback_router.handle_callback_query` (2 hàm đó nhận
           `bot_token: str` tĩnh, không phải getter). `on_message` threads
           `reset_batch_tally_hook` into `commands.handle_message` so
           `/chotky` can detect a missing hook before showing confirm.
        7. `PollingSupervisor(settings, client, dedupe, on_message,
           on_callback_query)`.

    KHÔNG đăng ký hook dọn `_pull_job_locks` — hook đó đã được
    `JobManager.__init__` tự đăng ký nội bộ (task 12,
    `lambda job_id: self._pull_job_locks.pop(job_id, None)`), bootstrap
    wiring không cần lặp lại.

    Args:
        job_manager: `JobManager` singleton đã khởi tạo xong.
        settings: `SettingsRepository` đã đăng ký namespace `telegram.*`.
        client: `TelegramBotClient` dùng chung với Push_Mode (khuyến
            nghị — 1 session HTTP cho cả 2 mode); tự construct nếu không
            truyền.
        reset_batch_tally_hook: optional callable for `/chotky` confirm
            (normally `TelegramNotifier.reset_batch_tally`).

    Returns:
        `(pull_coordinator, pull_notifier, polling_supervisor)` — caller
        (`bootstrap.py`) giữ `polling_supervisor` để spawn
        `run_forever()` như background task và expose qua
        `BootstrappedServices` cho lifespan shutdown gọi `shutdown()`.
    """
    client = client or TelegramBotClient()

    async def bot_token_getter() -> str:
        return await _get_bot_token(settings)

    # UX refactor (task 2): single status card per worker session, share
    # giữa coordinator (khởi tạo/update pha claim + retry) và notifier
    # (finalize ✅ khi QR ready). In-memory, không persist — restart mất
    # phiên đang chạy, worker gõ `/start` lấy nút mới.
    status_card = WorkerStatusCardTracker(client=client)

    pull_coordinator = PullJobCoordinator(
        job_manager=job_manager,
        client=client,
        settings=settings,
        bot_token_getter=bot_token_getter,
        status_card=status_card,
        reset_batch_tally_hook=reset_batch_tally_hook,
    )
    pull_notifier = PullJobNotifier(
        job_manager=job_manager,
        client=client,
        bot_token_getter=bot_token_getter,
        status_card=status_card,
    )
    job_manager.register_terminal_hook(pull_notifier.notify_pull_qr_ready)
    # UX refactor (task 4): pull-error hook giờ đi qua coordinator để
    # đọc/ghi status card + tự claim job kế tiếp cho cùng worker (tối đa
    # `MAX_AUTO_RETRY_ATTEMPTS` lần) trước khi finalize. Thay
    # `pull_notifier.notify_pull_error` (đã xoá) — cùng signature
    # `PullErrorHook`, không ảnh hưởng JobManager boundary.
    job_manager.register_pull_error_hook(pull_coordinator.handle_pull_error)
    job_manager.register_mode_switch_notify_hook(
        pull_notifier.notify_mode_switch_cancelled
    )

    dedupe_cache = CallbackDedupeCache()

    async def on_message(message: dict) -> None:
        bot_token = await bot_token_getter()
        await commands.handle_message(
            message,
            client=client,
            bot_token=bot_token,
            job_manager=job_manager,
            settings=settings,
            reset_batch_tally_hook=reset_batch_tally_hook,
        )

    async def on_callback_query(callback_query: dict) -> None:
        bot_token = await bot_token_getter()
        await callback_router.handle_callback_query(
            callback_query,
            client=client,
            bot_token=bot_token,
            job_manager=job_manager,
            pull_coordinator=pull_coordinator,
        )

    polling_supervisor = PollingSupervisor(
        settings=settings,
        client=client,
        dedupe=dedupe_cache,
        on_message=on_message,
        on_callback_query=on_callback_query,
    )

    return pull_coordinator, pull_notifier, polling_supervisor
