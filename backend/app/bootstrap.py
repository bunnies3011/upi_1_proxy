"""Bootstrap wiring dùng chung cho `app.main` (HTTP) và `app.cli` (local CLI).

Đây là module core-level đứng CẠNH `app/main.py` — TÁCH BIỆT phần khởi tạo
singleton + đăng ký namespace/handler payment module khỏi lifespan FastAPI,
để CLI (Requirement 15) tái sử dụng KHÔNG copy code (Requirement 15.1, 15.3).

Payment_Module_Boundary (Requirement 13.7): module này là ĐIỂM TÍCH HỢP THỨ HAI
(ngoài `app/main.py`) được phép import cả `app.core.*` VÀ `app.payments.ideal`.
Việc tập trung 2 điểm chạm này ở đây (thay vì rải rác trong CLI + web layer)
giúp giới hạn "diện tiếp xúc" của payment module xuống đúng số điểm entry, và
duy trì được nguyên tắc `core/` KHÔNG biết về payment method cụ thể.

_Requirements: 13.7, 15.1, 15.3_
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.db import DbEngine
from app.core.job_manager import JobManager
from app.core.job_repo import JobRepository
from app.core.proxy_pool import ProxyPool
from app.core.session_cache import AccountSessionCache
from app.core.settings_store import SettingsRepository
from app.core.sse import SseBroadcaster, make_sse_logger_factory

# ---------------------------------------------------------------------------
# Import điểm tích hợp payment module (Requirement 13.7). Chỉ 2 điểm được
# phép import `app.payments.ideal`: `app/main.py` và module này. Xem docstring
# `app/payments/ideal/__init__.py` — import package KHÔNG kích hoạt side-effect
# toàn cục; các đăng ký thực sự xảy ra qua 2 hàm public `register_*` gọi từ
# `bootstrap_services()` bên dưới.
# ---------------------------------------------------------------------------
from app.payments.gcash_direct import (
    register_gcash_direct_handler,
    register_gcash_direct_namespace,
)
from app.payments.ideal import register_ideal_handler, register_ideal_namespace
from app.payments.kakao_direct import (
    register_kakao_direct_handler,
    register_kakao_direct_namespace,
)
from app.payments.momo_check import (
    register_momo_check_handler,
    register_momo_check_namespace,
)
from app.payments.upi import register_upi_handler, register_upi_namespace
from app.payments.upi.license_pool import UpiLicensePool
from app.payments.upi_direct import (
    register_upi_direct_handler,
    register_upi_direct_namespace,
)
from app.payments.upi_oaipay import register_oaipay_handler, register_oaipay_namespace

# ---------------------------------------------------------------------------
# Điểm tích hợp notifier module. Cùng pattern với payment module: chỉ
# `bootstrap.py` được phép import `app.notifiers.*`, giữ core/api không
# biết notifier tồn tại. Nếu tương lai có nhiều notifier (Discord, Slack),
# mỗi module phơi cặp `register_*_namespace` + `register_*_notifier` và
# thêm 2 dòng gọi vào `bootstrap_services` — không đụng core/api.
# ---------------------------------------------------------------------------
from app.notifiers.telegram import (
    PullJobCoordinator,
    PullJobNotifier,
    PollingSupervisor,
    TelegramBotClient,
    TelegramNotifier,
    register_pull_mode_infrastructure,
    register_telegram_namespace,
    register_telegram_notifier,
)


__all__ = ["BootstrappedServices", "bootstrap_services"]


# ---------------------------------------------------------------------------
# Hằng số nội bộ — namespace core generic (KHÔNG hardcode tên payment method
# ở tầng này). `proxy` + `session_cache` là namespace chung, dùng cho mọi
# payment method trong tương lai.
# ---------------------------------------------------------------------------

_PROXY_NAMESPACE = "proxy"
_SESSION_CACHE_NAMESPACE = "session_cache"
_UPI_NAMESPACE = "upi"


@dataclass(frozen=True)
class BootstrappedServices:
    """Gói toàn bộ singleton đã dựng xong sau khi `bootstrap_services` chạy.

    Immutable (frozen) để chủ thể gọi (main.py lifespan hoặc CLI wrapper)
    KHÔNG vô tình swap singleton giữa chừng — mọi thay đổi runtime đi qua
    method của từng service (ví dụ `apply_settings`), KHÔNG qua rebinding
    field. `qr_output_dir`, `bind_host`, `db_path` được giữ lại trong bundle
    để tầng gọi (test/main.py `app.state`, CLI logging) có thể echo lại
    context runtime mà KHÔNG cần re-resolve env/config.
    """

    db_engine: DbEngine
    settings: SettingsRepository
    sse: SseBroadcaster
    proxy_pool: ProxyPool
    session_cache: AccountSessionCache
    upi_license_pool: UpiLicensePool
    job_manager: JobManager
    telegram_notifier: TelegramNotifier
    polling_supervisor: PollingSupervisor
    qr_output_dir: Path
    bind_host: str
    db_path: Path


async def bootstrap_services(
    db_path: str | Path,
    bind_host: str,
    session_cache_dir: Path,
    qr_output_dir: Path,
) -> BootstrappedServices:
    """Khởi tạo toàn bộ singleton core + đăng ký payment module (Requirement 13.7, 15.1, 15.3).

    Trình tự CHÍNH XÁC (không được đảo — có phụ thuộc runtime):

        1. Ensure các thư mục runtime tồn tại: `db_path.parent`,
           `session_cache_dir`, `qr_output_dir` (mkdir parents=True,
           exist_ok=True).
        2. Khởi tạo singleton core theo thứ tự phụ thuộc constructor:
           `DbEngine` → `SettingsRepository(engine)` → `SseBroadcaster()`
           → `ProxyPool(settings)` → `AccountSessionCache(settings,
           session_cache_dir)` → `JobManager(settings, proxy_pool, sse)`.
        3. `await engine.init_schema()` — tạo bảng `settings` + index nếu
           chưa có (idempotent, Requirement 11.6).
        4. `await register_ideal_namespace(settings)` — đăng ký namespace
           `ideal.*` (Payment_Module_Boundary — Requirement 13.7). PHẢI
           gọi trước `apply_settings` hoặc `register_ideal_handler` vì cả
           hai đều đọc key `ideal.*`.
        6. Hydrate 2 service có state runtime từ Settings_Store:
           `proxy_pool.apply_settings(...)`, `session_cache.apply_settings(...)`
           (Requirement 9.1, 10.1). PHẢI gọi SAU `init_schema` (cần bảng
           settings) và có thể gọi trước hoặc sau `register_ideal_handler`
           (không phụ thuộc lẫn nhau).
        7. `register_ideal_handler(job_manager, settings, session_cache,
           qr_output_dir)` — đăng ký `IdealFlowHandler` vào `JobManager` để
           dispatch job `payment_method="ideal"` (Requirement 13.4, 13.7).

    KHÔNG bao gồm bước `deps.configure_services(...)` — đây là bước riêng
    của TẦNG HTTP (bơm singleton vào dependency getter của FastAPI router),
    được `app/main.py` tự thực hiện SAU khi gọi `bootstrap_services`. CLI
    KHÔNG cần bước này vì không đi qua router.

    KHÔNG bao gồm bước `engine.close()` — cleanup là trách nhiệm của caller
    (main.py lifespan shutdown, CLI finally block).

    Args:
        db_path: Đường dẫn SQLite file (str hoặc Path — normalize về Path
            trước khi mkdir). Caller (main.py resolve từ env; CLI resolve
            từ env + `--db-path` override) chịu trách nhiệm quyết định
            location.
        bind_host: Host mà Backend_Service sẽ bind. Chỉ mang tính thông tin
            (informational) — được giữ lại trong `BootstrappedServices` để
            tầng gọi echo/log (`app.state.bind_host`); KHÔNG còn dùng cho
            bất kỳ kiểm tra bảo mật khởi động nào (auth đã bỏ hoàn toàn).
            CLI có thể truyền string rỗng hoặc placeholder.
        session_cache_dir: Thư mục lưu file cache session per-account
            (Requirement 10.1). PHẢI tồn tại/mkdir được (bước 1).
        qr_output_dir: Thư mục lưu file QR PNG per-job (Requirement 7.3,
            7.4). PHẢI tồn tại/mkdir được (bước 1).

    Returns:
        `BootstrappedServices` immutable chứa toàn bộ singleton đã hydrate
        + register xong, sẵn sàng nhận job/HTTP request.

    Raises:
        NamespaceAlreadyRegisteredError: `register_ideal_namespace` đã được
            gọi từ trước với `settings` này (bug setup, Fail_Fast).
        HandlerAlreadyRegisteredError: `register_ideal_handler` đã được
            gọi từ trước với `job_manager` này (bug setup, Fail_Fast).
        SettingsValidationError: Giá trị hydrate từ Settings_Store không
            khớp constraint (bug data trong DB, Fail_Fast).
    """
    # 1) Ensure runtime dirs tồn tại — làm sớm để mọi service phía dưới
    #    (DbEngine, AccountSessionCache, QrRenderer) không phải tự lo và
    #    tránh race khi chạy song song test parallel.
    db_path_p = Path(db_path)
    db_path_p.parent.mkdir(parents=True, exist_ok=True)
    session_cache_dir.mkdir(parents=True, exist_ok=True)
    qr_output_dir.mkdir(parents=True, exist_ok=True)

    # 2) Khởi tạo singleton core theo thứ tự phụ thuộc constructor.
    engine = DbEngine(db_path_p)
    settings = SettingsRepository(engine)
    sse = SseBroadcaster()
    proxy_pool = ProxyPool(settings)
    session_cache = AccountSessionCache(settings, session_cache_dir)
    # `JobRepository` inject vào `JobManager` để persist job state qua
    # restart. Không có repo → JobManager vẫn hoạt động (test unit tạo
    # không có repo), chỉ mất persistence.
    job_repo = JobRepository(engine)
    job_manager = JobManager(settings, proxy_pool, sse, job_repo=job_repo)

    # 3) Init schema — idempotent, an toàn gọi mỗi lần startup.
    await engine.init_schema()

    # 4) Đăng ký namespace payment module (Payment_Module_Boundary —
    #    Requirement 13.7). PHẢI trước apply_settings + register_ideal_handler.
    await register_ideal_namespace(settings)
    # 4a-upi) Đăng ký namespace payment module UPI (Payment_Module_Boundary).
    #         PHẢI trước register_upi_handler + apply_max_concurrent (handler
    #         đọc `upi.*` live; concurrency key `upi.max_concurrent` phải seed).
    await register_upi_namespace(settings)
    # 4a-oaipay) UPI no-CDK (OaiPay) namespace — seed before handler + concurrency.
    await register_oaipay_namespace(settings)
    # 4a-upi_direct) In-house ChatGPT+Stripe UPI QR — seed before handler + concurrency.
    await register_upi_direct_namespace(settings)
    await register_kakao_direct_namespace(settings)
    await register_gcash_direct_namespace(settings)
    await register_momo_check_namespace(settings)
    # 4b) Đăng ký namespace notifier — cùng thứ tự với payment module,
    #     trước khi apply_settings để settings validate không lỡ nhịp
    #     nếu user đã có `telegram.*` trong DB từ đợt trước. `engine` được
    #     truyền thêm để `register_telegram_namespace` có thể migrate 2 key
    #     cũ (`telegram.enabled`/`telegram.chat_targets`) bằng raw SQL
    #     TRƯỚC khi namespace mới (whitelist đổi) được đăng ký.
    await register_telegram_namespace(settings, engine)

    # 6) Hydrate service có state runtime từ Settings_Store (Requirement 9.1, 10.1).
    proxy_pool.apply_settings(await settings.list(_PROXY_NAMESPACE))
    session_cache.apply_settings(await settings.list(_SESSION_CACHE_NAMESPACE))

    # 6b) Startup preflight probe — chạy 1 lần sau khi pool đã hydrate để
    #     mark_dead sớm các proxy không kết nối được. KHÔNG BLOCK startup
    #     quá `total_timeout_seconds` (30s default) — nếu pool 20 proxy
    #     toàn timeout, cắt sớm để service vẫn boot; proxy chưa probe xong
    #     sẽ được probe lại per-job.
    #     Chạy trong background task để không delay UVicorn `startup`
    #     event — user vẫn có thể submit job ngay, các job đầu sẽ chờ
    #     preflight complete qua semaphore (probe_concurrency).
    from app.core.proxy_health import probe_pool_batch
    import asyncio as _asyncio
    import logging as _logging_bootstrap
    _preflight_logger = _logging_bootstrap.getLogger("app.bootstrap.preflight")
    _preflight_task = _asyncio.create_task(
        probe_pool_batch(
            proxy_pool,
            proxy_pool.probe_config,
            logger=_preflight_logger,
        ),
        name="proxy-preflight-startup",
    )
    # KHÔNG await — để startup tiếp tục. Task tự log kết quả qua logger
    # khi xong. Nếu task raise ngoại lệ, `_preflight_logger` không bắt
    # → asyncio in warning `Task exception was never retrieved`. Attach
    # done-callback để log gracefully.
    def _preflight_done(task: "_asyncio.Task") -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _preflight_logger.exception(
                "preflight batch failed", exc_info=exc
            )

    _preflight_task.add_done_callback(_preflight_done)

    # 7) Đăng ký handler payment module vào JobManager (Requirement 13.4, 13.7).
    #    Inject `logger_factory` có `SseLogHandler` → mọi `logger.info(...)`
    #    trong 12-step flow tự broadcast SSE `job_log` realtime + lưu vào
    #    buffer log của job (JobManager.record_log_line).
    logger_factory = make_sse_logger_factory(
        sse=sse,
        loop=_asyncio.get_event_loop(),
        record_log_line=job_manager.record_log_line,
    )
    ideal_handler = register_ideal_handler(
        job_manager=job_manager,
        settings=settings,
        session_cache=session_cache,
        qr_output_dir=qr_output_dir,
        logger_factory=logger_factory,
    )

    # 7a) Đăng ký job-removed hook để `DeviceProfileAllocator._cache`
    #     (job_id -> DeviceProfile, R5.1) được dọn đồng bộ mỗi khi job bị
    #     xoá khỏi `JobManager` (delete_job / cleanup TTL 24h / trần cứng
    #     10k job). Thiếu bước này, cache phình vô hạn theo tổng số job
    #     đã TỪNG chạy trong đời process — memory leak khi chạy hàng chục
    #     nghìn account nhiều ngày liên tục (fix cùng đợt với
    #     `_JobRecord.logs` bounded deque).
    job_manager.register_job_removed_hook(
        ideal_handler.device_profile_allocator.forget
    )

    # 7c) Đăng ký handler UPI (payment method "upi") — cùng logger_factory SSE
    #     với iDEAL. `register_upi_handler` tự dựng UpiLicensePool + vendor
    #     client factory bên trong; gọi SAU register_upi_namespace (bước 4a-upi)
    #     và TRƯỚC apply_max_concurrent_from_settings (bước 9).
    upi_handler = register_upi_handler(
        job_manager=job_manager,
        settings=settings,
        session_cache=session_cache,
        qr_output_dir=qr_output_dir,
        logger_factory=logger_factory,
    )
    # Hydrate UpiLicensePool từ Settings_Store — cùng pattern proxy_pool ở
    # bước 6. `register_upi_handler` dựng pool RỖNG bên trong; nếu bỏ bước
    # này thì mọi job UPI fail `upi_no_license_credit` cho tới khi restart
    # (pool không bao giờ thấy `upi.license_codes` operator đã nhập). Gọi SAU
    # register (cần pool đã tồn tại) và SAU register_upi_namespace (bước
    # 4a-upi, cần key `upi.*` đã seed). apply_settings KHÔNG query DB/verify
    # egress — chỉ rebuild `_codes` từ snapshot, an toàn ở startup.
    upi_handler.license_pool.apply_settings(await settings.list(_UPI_NAMESPACE))

    # 7d) UPI no-CDK (OaiPay) handler — no license pool; live settings per run.
    register_oaipay_handler(
        job_manager=job_manager,
        settings=settings,
        session_cache=session_cache,
        qr_output_dir=qr_output_dir,
        logger_factory=logger_factory,
    )

    # 7e) UPI Direct (in-house ChatGPT+Stripe) — no vendor; live settings per run.
    register_upi_direct_handler(
        job_manager=job_manager,
        settings=settings,
        session_cache=session_cache,
        qr_output_dir=qr_output_dir,
        logger_factory=logger_factory,
    )

    register_kakao_direct_handler(
        job_manager=job_manager,
        settings=settings,
        session_cache=session_cache,
        qr_output_dir=qr_output_dir,
        logger_factory=logger_factory,
    )

    register_gcash_direct_handler(
        job_manager=job_manager,
        settings=settings,
        session_cache=session_cache,
        qr_output_dir=qr_output_dir,
        logger_factory=logger_factory,
    )

    register_momo_check_handler(
        job_manager=job_manager,
        settings=settings,
        session_cache=session_cache,
        qr_output_dir=qr_output_dir,
        logger_factory=logger_factory,
    )

    # 7b) Đăng ký Telegram notifier — hook vào JobManager qua
    #     `register_terminal_hook`. Gọi SAU khi `register_ideal_handler`
    #     để đảm bảo namespace `telegram.*` đã đăng ký (bước 4b) VÀ
    #     JobManager sẵn sàng nhận hook. Notifier là singleton, expose
    #     qua `BootstrappedServices.telegram_notifier` để main.py inject
    #     vào `routes_notifications` (endpoint test + reset cursor).
    #
    #     `telegram_bot_client` được tạo TRƯỚC và dùng CHUNG cho cả
    #     Push_Mode (`register_telegram_notifier`) VÀ Pull_Mode
    #     (`register_pull_mode_infrastructure`, bước 9 dưới) — 1 session
    #     HTTP curl_cffi cho toàn bộ giao tiếp Telegram API, thay vì mỗi
    #     mode tự mở session riêng.
    telegram_bot_client = TelegramBotClient()
    telegram_notifier = register_telegram_notifier(
        job_manager=job_manager,
        settings=settings,
        sse=sse,
        client=telegram_bot_client,
    )
    # Push_Success_Gate wiring (feature "success wait" Push_Mode) —
    # 2 chiều giữa notifier và job_manager qua callback do bootstrap
    # inject (Payment_Module_Boundary: notifier không import
    # `core/job_manager.py`, JobManager không import `notifiers/`):
    #   - JobManager scheduler đọc gate qua `_push_pause_checker` để
    #     quyết định có bỏ qua job Push_Mode trong queue không.
    #   - Gate đánh thức JobManager scheduler qua `wake_scheduler` sync
    #     callback khi user bấm "Tiếp tục" (reset counter → paused=False),
    #     để queue có job Push_Mode đang chờ được xử lý ngay.
    if telegram_notifier.push_gate is not None:
        telegram_notifier.push_gate.set_wake_scheduler(
            job_manager.wake_scheduler
        )
        # Khi gate hit threshold → gọi callback này để set cờ pause trên
        # cancellation_token của mọi job Push_Mode RUNNING. Handler check
        # is_paused() ở checkpoint sau login xong → trả pause_requested=True
        # → wrapper chuyển record về PENDING. Đảm bảo counter Telegram
        # KHÔNG overshoot (không có "12/10 đã gửi") và cache session được
        # bảo toàn (login xong mới cho phép dừng).
        telegram_notifier.push_gate.set_pause_running_push_jobs_callback(
            job_manager.signal_pause_running_push_jobs
        )
    if telegram_notifier.live_qr_gate is not None:
        telegram_notifier.live_qr_gate.set_wake_scheduler(
            job_manager.wake_scheduler
        )
        telegram_notifier.live_qr_gate.set_pause_running_push_jobs_callback(
            job_manager.signal_pause_running_push_jobs
        )
    # Composite pause checker: Success Wait OR Live QR full (both cached
    # sync bools, no I/O). Mutex means one mode active; OR is fail-closed
    # for flood while each gate no-ops when its mode is disabled.
    push_gate = telegram_notifier.push_gate
    live_gate = telegram_notifier.live_qr_gate

    def _composite_push_paused() -> bool:
        paused = push_gate is not None and push_gate.is_paused()
        blocked = live_gate is not None and live_gate.is_blocked()
        return paused or blocked

    if push_gate is not None or live_gate is not None:
        job_manager.register_push_pause_checker(_composite_push_paused)

    # 8) Load job đã persist từ DB (Requirement: nhớ qua restart).
    #    Gọi SAU `register_ideal_handler` để mọi `payment_method` có
    #    dispatcher — nếu load ra job với method chưa có handler,
    #    scheduler sẽ dispatch fail (UnknownPaymentMethodError trong
    #    `_run_handler`) → job ERROR luôn thay vì kẹt PENDING.
    #    Job đang RUNNING lúc shutdown được `load_from_db` chuyển về
    #    PENDING → scheduler tự kick lại flow từ đầu.
    await job_manager.load_from_db()

    # 8b) Khởi tạo hạ tầng Pull_Mode (Migration Strategy điểm 5 design.md
    #     — task 32): `PullJobCoordinator` + `PullJobNotifier` +
    #     `PollingSupervisor`, dùng CHUNG `telegram_bot_client` với
    #     Push_Mode (bước 7b). `register_pull_mode_infrastructure` tự
    #     đăng ký `pull_notifier.notify_pull_qr_ready` qua
    #     `job_manager.register_terminal_hook` VÀ
    #     `pull_notifier.notify_pull_error` qua
    #     `job_manager.register_pull_error_hook` bên trong nó — bootstrap
    #     KHÔNG cần tự đăng ký lại. Gọi SAU `load_from_db` để mọi job
    #     Pull_Mode resume dở từ trước (`pull_assignment_state=assigned`)
    #     đã nằm trong `_jobs`/`_pending_order` TRƯỚC khi polling loop có
    #     thể nhận claim mới — tránh race giữa việc resume state cũ và
    #     việc claim account mới từ Telegram_Worker.
    #
    #     KHÔNG cần tự đăng ký hook dọn `_pull_job_locks` — hook đó đã
    #     được `JobManager.__init__` tự đăng ký nội bộ (task 12,
    #     `lambda job_id: self._pull_job_locks.pop(job_id, None)`).
    _pull_coordinator, _pull_notifier, polling_supervisor = (
        register_pull_mode_infrastructure(
            job_manager=job_manager,
            settings=settings,
            client=telegram_bot_client,
            reset_batch_tally_hook=telegram_notifier.reset_batch_tally,
        )
    )

    # Spawn polling loop như background task — cùng pattern với
    # `_preflight_task` ở bước 6b: KHÔNG await để không delay UVicorn
    # `startup` event, tự log exception qua done-callback để tránh
    # asyncio warning "Task exception was never retrieved" nếu
    # `run_forever()` raise ngoài dự kiến (bản thân nó đã tự bọc
    # try/except cho từng vòng lặp — done-callback này chỉ bắt lỗi
    # setup ngoài dự kiến).
    _pull_polling_logger = _logging_bootstrap.getLogger(
        "app.bootstrap.pull_polling"
    )
    _pull_polling_task = _asyncio.create_task(
        polling_supervisor.run_forever(),
        name="telegram-pull-mode-polling",
    )

    def _pull_polling_done(task: "_asyncio.Task") -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _pull_polling_logger.exception(
                "pull-mode polling supervisor failed", exc_info=exc
            )

    _pull_polling_task.add_done_callback(_pull_polling_done)

    # 9) Đồng bộ concurrency runtime từ Settings_Store (Requirement 8.4).
    #    `JobManager.__init__` khởi tạo Semaphore(1) mặc định để scheduler
    #    có thể chạy trước khi settings load; nếu bỏ bước này, user cấu
    #    hình `ideal.max_concurrent=20` trong DB nhưng runtime vẫn chỉ
    #    chạy 1 job song song → UI hiển thị "Multi 20" mà thực tế chỉ
    #    có 1 slot. Gọi SAU `register_ideal_handler` vì cần
    #    `_handlers` đã có ít nhất 1 handler để đọc concurrency key.
    await job_manager.apply_max_concurrent_from_settings()

    return BootstrappedServices(
        db_engine=engine,
        settings=settings,
        sse=sse,
        proxy_pool=proxy_pool,
        session_cache=session_cache,
        upi_license_pool=upi_handler.license_pool,
        job_manager=job_manager,
        telegram_notifier=telegram_notifier,
        polling_supervisor=polling_supervisor,
        qr_output_dir=qr_output_dir,
        bind_host=bind_host,
        db_path=db_path_p,
    )
