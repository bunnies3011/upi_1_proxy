"""FastAPI app factory + startup wiring cho Backend_Service (Requirement 9.8, 11.1, 13.7).

Đây là điểm entry duy nhất khởi tạo toàn bộ Backend_Service:

1. Đọc 2 bootstrap config từ env (`IDEAL_QR_TOOL_DB_PATH`,
   `IDEAL_QR_TOOL_BIND_HOST`) — chỉ 2 giá trị này là "chicken-and-egg" (cần
   để mở SQLite/quyết định bind, KHÔNG thể lưu trong Settings_Store vì
   Settings_Store chưa mở). Mọi runtime config nghiệp vụ khác đọc từ
   Settings_Store (Requirement 11.1 — không có file JSON/YAML nào khác).

2. Ủy quyền TOÀN BỘ khởi tạo singleton + đăng ký payment module cho
   `app.bootstrap.bootstrap_services(...)` (Requirement 13.7, 15.1, 15.3).
   `main.py` KHÔNG còn trực tiếp import `app.core.*` hay `app.payments.ideal`
   — 2 điểm chạm payment module giờ nằm gọn trong `bootstrap.py`, main.py
   chỉ điều phối tầng HTTP (env → bootstrap → deps.configure_services →
   app.state → include_router).

3. Lifespan startup:
   - `await bootstrap_services(...)` — chạy toàn bộ init schema, đăng ký
     namespace `ideal.*`, hydrate `ProxyPool` + `AccountSessionCache`, đăng
     ký `IdealFlowHandler` vào `JobManager`. Fail_Fast_Policy: exception
     bên trong sẽ ngăn app bind socket, uvicorn exit non-zero.
   - `deps.configure_services(...)` — bơm singleton vào tầng `api/` để
     dependency getter cấp cho router (Requirement 14.7). Bước này thuộc
     TẦNG HTTP nên `bootstrap_services` KHÔNG bao gồm; CLI (Requirement 15)
     tái sử dụng `bootstrap_services` mà không cần bước này.
   - Attach singleton vào `fastapi_app.state.*` để test (task 26.2) inspect
     trạng thái sau startup mà không phải re-import module.

4. Include 4 router: `routes_jobs`, `routes_settings`, `routes_session_cache`,
   `routes_events` — mỗi router đã tự khai `prefix` riêng theo file (`/api/*`).

5. Lifespan shutdown, THEO THỨ TỰ:
   a) `await services.job_manager.shutdown()` — cancel + AWAIT toàn bộ task
      nền (delayed-retry, scheduler, cleanup, HANDLER TASK in-flight) trước
      khi trả về. Bắt buộc phải chờ handler task exit ở đây, nếu không
      chúng sẽ tiếp tục gọi `_persist_record` → hit
      `sqlite3.ProgrammingError: Cannot operate on a closed database` khi
      bước (c) đóng connection.
   b) `await services.telegram_notifier.aclose()` — đóng HTTP session.
   c) `await services.db_engine.close()` — đóng SQLite connection.

Payment_Module_Boundary (Requirement 13.7): SAU refactor task 38.2,
`main.py` KHÔNG còn import `app.payments.ideal` — điểm chạm được rút về
`app/bootstrap.py`. `main.py` giờ hoàn toàn agnostic với payment method
cụ thể, chỉ biết đến `bootstrap_services` như 1 hàm khởi tạo generic.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# ---------------------------------------------------------------------------
# Windows-only: force `WindowsSelectorEventLoopPolicy` TRƯỚC khi bất kỳ
# event loop nào được tạo (uvicorn tạo loop khi import module này).
#
# Lý do: backend dùng `curl_cffi.requests.AsyncSession` (xem
# `app/core/http_client.py`) làm HTTP client duy nhất. `curl_cffi` đăng ký
# socket vào event loop qua `loop.add_reader` / `add_writer` — 2 method này
# KHÔNG tồn tại trên `WindowsProactorEventLoopPolicy` (default Python 3.8+
# trên Windows).
#
# Khi chạy trên Proactor loop, `curl_cffi` fallback qua một selector-thread
# bridge (`curl_cffi/aio.py :: AddThreadSelectorEventLoop`). Bridge này khi
# có nhiều request async concurrent (batch 20+ job qua proxy) hay drop socket
# event → request bị stall cho tới khi timeout 30s hit (curl error 28
# "Operation timed out after 30000 milliseconds with 0 bytes received").
# Upstream `curl_cffi` chính thức khuyến cáo caller set Selector policy để
# né bridge này (xem source `curl_cffi/aio.py` `PROACTOR_WARNING`).
#
# macOS/Linux không bị vì default là `SelectorEventLoop` (kqueue/epoll) hỗ
# trợ `add_reader` native — libcurl multi-socket API chạy trực tiếp không
# qua bridge nào.
#
# `uvicorn` không tự set policy này; setup.bat launcher cũng không set. Nếu
# không có block dưới đây, mọi cài đặt Windows sẽ dính timeout 30s ở step
# `checkout` / `stripe_init` / `pay_ideal_get_page` khi batch chạy nhiều job.
#
# GHI CHÚ: Selector policy trên Windows KHÔNG hỗ trợ subprocess (Proactor
# mới hỗ trợ). Backend không spawn subprocess async ở runtime (Playwright
# đã bỏ, không dùng `asyncio.create_subprocess_*`) nên trade-off này an
# toàn. Nếu tương lai cần subprocess async trên Windows, phải cân nhắc lại.
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import deps
from app.api import (
    routes_events,
    routes_jobs,
    routes_notifications,
    routes_proxy,
    routes_session_cache,
    routes_settings,
)
from app.bootstrap import BootstrappedServices, bootstrap_services

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bootstrap config từ env — chỉ 2 giá trị bắt buộc phải nằm ngoài
# Settings_Store (chicken-and-egg). Mọi giá trị khác đọc từ Settings_Store.
# ---------------------------------------------------------------------------

_ENV_DB_PATH = "IDEAL_QR_TOOL_DB_PATH"
_ENV_BIND_HOST = "IDEAL_QR_TOOL_BIND_HOST"

_DEFAULT_DB_FILENAME = "ideal_qr_tool.db"
_DEFAULT_BIND_HOST = "127.0.0.1"

# Convention project (design.md → cấu trúc thư mục): mọi dữ liệu runtime
# nằm trong `backend/runtime/` (gitignored). Tên subdir cố định cho từng
# loại artifact để test/integration định vị dễ.
_RUNTIME_SUBDIR = "runtime"
_SESSION_CACHE_SUBDIR = "session_cache"
_QR_OUTPUT_SUBDIR = "qr"


def _backend_root() -> Path:
    """Trả về `backend/` — parent của package `app/`, dùng làm anchor cho
    các đường dẫn relative (`runtime/...`) khi env var không set/tương đối.

    `main.py` nằm ở `backend/app/main.py` nên `Path(__file__).resolve().parent`
    là `backend/app/`, `.parent` nữa là `backend/`.
    """
    return Path(__file__).resolve().parent.parent


def _resolve_db_path() -> Path:
    """Xác định đường dẫn file SQLite từ env `IDEAL_QR_TOOL_DB_PATH`.

    - Không set → `backend/runtime/ideal_qr_tool.db`.
    - Set giá trị tuyệt đối → dùng nguyên.
    - Set giá trị tương đối → resolve theo `backend/` (KHÔNG theo CWD của
      process — CWD phụ thuộc cách người vận hành start uvicorn, dễ gây
      confusion; anchor vào `backend/` cho ổn định).
    """
    raw = os.environ.get(_ENV_DB_PATH, "").strip()
    if not raw:
        return _backend_root() / _RUNTIME_SUBDIR / _DEFAULT_DB_FILENAME
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    return _backend_root() / candidate


def _resolve_bind_host() -> str:
    """Đọc bind host từ env `IDEAL_QR_TOOL_BIND_HOST` (default `127.0.0.1`).

    Giá trị này chỉ mang tính thông tin (informational) — được echo lại qua
    `app.state.bind_host` cho log/debug. KHÔNG dùng để bind socket thực sự
    (uvicorn/process manager tự bind theo cli/env riêng của uvicorn) và
    KHÔNG còn dùng cho bất kỳ kiểm tra bảo mật khởi động nào (auth đã bỏ
    hoàn toàn).
    """
    raw = os.environ.get(_ENV_BIND_HOST, "").strip()
    return raw or _DEFAULT_BIND_HOST


def _resolve_session_cache_dir() -> Path:
    """Thư mục lưu file cache session per-account (Requirement 10.1)."""
    return _backend_root() / _RUNTIME_SUBDIR / _SESSION_CACHE_SUBDIR


def _resolve_qr_output_dir() -> Path:
    """Thư mục lưu file QR PNG per-job (Requirement 7.3, 7.4)."""
    return _backend_root() / _RUNTIME_SUBDIR / _QR_OUTPUT_SUBDIR


def create_app() -> FastAPI:
    """FastAPI app factory (Requirement 9.8, 11.1, 13.7).

    Trả về instance `FastAPI` đã được cấu hình đầy đủ:

    - Resolve 4 bootstrap config path/host từ env NGAY tại factory (đồng
      bộ, không I/O). Việc khởi tạo singleton core được ủy quyền hoàn toàn
      cho `bootstrap_services` chạy trong lifespan startup (async).
    - Lifespan context manager phụ trách toàn bộ I/O startup/shutdown
      (bootstrap_services + deps.configure_services + attach app.state +
      close DB) — Fail_Fast_Policy: exception raise trong lifespan startup
      sẽ ngăn app bind socket, uvicorn exit non-zero.

    Returns:
        `FastAPI` instance đã include đủ 4 router (`routes_jobs`,
        `routes_settings`, `routes_session_cache`, `routes_events`) và đã
        cài lifespan hook. Có thể trực tiếp truyền cho `uvicorn.run(...)`.
    """
    db_path = _resolve_db_path()
    bind_host = _resolve_bind_host()
    session_cache_dir = _resolve_session_cache_dir()
    qr_output_dir = _resolve_qr_output_dir()

    @asynccontextmanager
    async def lifespan(fastapi_app: FastAPI):
        # ---- STARTUP -----------------------------------------------------
        # 1) Ủy quyền toàn bộ bootstrap singleton core + đăng ký payment
        #    module cho `bootstrap_services` (Requirement 13.7, 15.1, 15.3).
        #    Auth ĐÃ BỎ HOÀN TOÀN — không còn kiểm tra bảo mật khởi động;
        #    tool chạy trên local network tin cậy.
        services: BootstrappedServices = await bootstrap_services(
            db_path=db_path,
            bind_host=bind_host,
            session_cache_dir=session_cache_dir,
            qr_output_dir=qr_output_dir,
        )

        # 2) Bơm singleton vào tầng `api/` (Requirement 14.7). Router
        #    dependency (`deps.get_*`) sẽ lấy từ state module-level này.
        #    Bước này thuộc TẦNG HTTP, KHÔNG nằm trong `bootstrap_services`
        #    để CLI (Requirement 15) tái sử dụng bootstrap mà không cần
        #    dependency getter của FastAPI.
        deps.configure_services(
            settings=services.settings,
            job_manager=services.job_manager,
            proxy_pool=services.proxy_pool,
            session_cache=services.session_cache,
            sse=services.sse,
            upi_license_pool=services.upi_license_pool,
        )
        # Inject notifier singleton vào tầng route `notifications/*`. Cùng
        # pattern với `deps.configure_services` nhưng tách module riêng vì
        # notifier không thuộc core (tránh mở rộng deps.py với field không
        # phải core-level).
        routes_notifications.configure_telegram_notifier(services.telegram_notifier)

        # 3) Expose singleton qua `app.state` cho phép test (task 26.2)
        #    inspect trạng thái sau startup. Đây KHÔNG phải cơ chế DI
        #    chính — DI đi qua `app.api.deps` (đã configure ở bước 2).
        #    Giữ nguyên tên field public như trước refactor (task 26.1)
        #    để không break test/consumer đang đọc `app.state.*`.
        fastapi_app.state.db_engine = services.db_engine
        fastapi_app.state.settings_repo = services.settings
        fastapi_app.state.sse_broadcaster = services.sse
        fastapi_app.state.proxy_pool = services.proxy_pool
        fastapi_app.state.session_cache = services.session_cache
        fastapi_app.state.job_manager = services.job_manager
        fastapi_app.state.telegram_notifier = services.telegram_notifier
        fastapi_app.state.db_path = services.db_path
        fastapi_app.state.bind_host = services.bind_host

        logger.info(
            "Backend_Service started: db_path=%s bind_host=%s",
            services.db_path,
            services.bind_host,
        )

        try:
            yield
        finally:
            # ---- SHUTDOWN -------------------------------------------------
            # Trước tiên cancel + await toàn bộ task nền của JobManager
            # (delayed auto-retry, scheduler, cleanup, VÀ handler task
            # in-flight). `JobManager.shutdown()` không return cho tới khi
            # tất cả task đã exit → đảm bảo không còn task nào gọi
            # `_persist_record` khi `db_engine.close()` bên dưới đóng
            # connection. Best-effort: exception (bao gồm timeout 5s) được
            # log nhưng không phá tiếp bước đóng DB.
            try:
                await services.job_manager.shutdown()
            except Exception:  # noqa: BLE001
                logger.exception("job_manager.shutdown() failed")
            # Đóng HTTP session của Telegram notifier — best-effort.
            try:
                await services.telegram_notifier.aclose()
            except Exception:  # noqa: BLE001
                logger.exception("telegram_notifier.aclose() failed")
            # Dừng polling supervisor TRƯỚC khi đóng DB — nếu không, vòng
            # lặp polling có thể còn ghi `telegram.polling_offset` vào
            # SQLite trong lúc teardown. Best-effort, guard None phòng khi
            # config không dựng supervisor.
            polling_supervisor = getattr(services, "polling_supervisor", None)
            if polling_supervisor is not None:
                try:
                    await polling_supervisor.shutdown()
                except Exception:  # noqa: BLE001
                    logger.exception("polling_supervisor.shutdown() failed")
            # Đóng connection SQLite dùng chung (idempotent).
            await services.db_engine.close()

    fastapi_app = FastAPI(
        title="iDEAL QR Tool — Backend",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Include 4 router — mỗi router tự khai `prefix` (`/api/jobs`,
    # `/api/settings`, `/api/session-cache`, `/api/events`) trong file
    # riêng, main.py KHÔNG duplicate prefix ở đây.
    fastapi_app.include_router(routes_jobs.router)
    fastapi_app.include_router(routes_settings.router)
    fastapi_app.include_router(routes_session_cache.router)
    fastapi_app.include_router(routes_events.router)
    fastapi_app.include_router(routes_notifications.router)
    fastapi_app.include_router(routes_proxy.router)

    # -----------------------------------------------------------------
    # Mount static frontend (Vue build) — cùng origin backend để chỉ chạy
    # 1 process uvicorn cho cả API + UI (không cần Vite dev server song
    # song). Vite build ra `frontend/dist/` với `index.html` + `assets/`.
    #
    # Layout: `backend/app/main.py` → parent 2 lần = `backend/`, sibling
    # `frontend/dist/`. Nếu thư mục KHÔNG tồn tại (dev chưa build) → log
    # warning + skip mount; user có thể chạy Vite dev server proxy song
    # song (theo `vite.config.ts`). API vẫn hoạt động độc lập.
    # -----------------------------------------------------------------
    frontend_dist = _backend_root().parent / "frontend" / "dist"
    if frontend_dist.is_dir() and (frontend_dist / "index.html").exists():
        # Mount assets (chunks JS/CSS + favicon...) tại `/assets` để khớp
        # base URL Vite build default. `html=False` để 404 asset trả JSON
        # error thay vì html fallback (dễ debug).
        assets_dir = frontend_dist / "assets"
        if assets_dir.is_dir():
            fastapi_app.mount(
                "/assets",
                StaticFiles(directory=str(assets_dir), html=False),
                name="frontend-assets",
            )

        # Serve các file khác trong dist root (favicon.svg, vite.svg,
        # robots.txt...) khi client request đúng tên. Dùng route bắt
        # regex thay vì StaticFiles(directory=root) để tránh conflict
        # với `/api/*` (StaticFiles không có wildcard filter).
        @fastapi_app.get("/", include_in_schema=False)
        async def _index() -> FileResponse:
            return FileResponse(str(frontend_dist / "index.html"))

        # SPA fallback: mọi route KHÔNG match `/api/*` và không có file
        # tĩnh khớp → trả `index.html` (Vue Router hash mode vẫn OK vì
        # backend chỉ serve entry HTML; JS client-side handle route).
        # Bắt path `{full_path:path}` cuối để KHÔNG shadow các route
        # `/api/*` đã include ở trên (FastAPI ưu tiên route đăng ký
        # trước).
        @fastapi_app.get("/{full_path:path}", include_in_schema=False)
        async def _spa_fallback(full_path: str) -> FileResponse:
            # Không tự serve `/api/*` — nếu chưa match router API ở trên,
            # trả 404 rõ ràng thay vì trả HTML.
            if full_path.startswith("api/") or full_path == "api":
                raise HTTPException(
                    status_code=404, detail={"error_code": "not_found"}
                )
            # Ưu tiên file thật trong dist (VD favicon, robots, image).
            candidate = frontend_dist / full_path
            if candidate.is_file():
                return FileResponse(str(candidate))
            # Fallback tất cả path khác về `index.html` để SPA client-side
            # route hoạt động (Vue Router history mode).
            return FileResponse(str(frontend_dist / "index.html"))

        logger.info(
            "frontend static mounted at '/' from %s", frontend_dist,
        )
    else:
        logger.warning(
            "frontend dist not found at %s — chạy `npm run build` "
            "trong frontend/ để serve UI cùng origin backend. "
            "Trong dev, dùng Vite dev server (`npm run dev`) song song.",
            frontend_dist,
        )

    return fastapi_app


# Module-level `app` cho `uvicorn app.main:app` chạy trực tiếp không cần
# factory flag (`--factory`). Test dùng `create_app()` mới mỗi lần để cách
# ly state (mỗi test 1 DB tạm — task 26.2).
app = create_app()
