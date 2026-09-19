"""CLI bootstrap wrapper (Requirement 15.1, 15.3, 15.4).

Wrapper MỎNG bọc `app.bootstrap.bootstrap_services(...)` để CLI tái sử dụng
100% wiring của Backend_Service (singleton core + đăng ký payment module)
mà KHÔNG copy code (Requirement 15.3). CLI chạy local, không bind socket
(auth đã bỏ hoàn toàn khỏi toàn bộ backend).

Priority resolve `db_path` (Requirement 15.4):

    1. `db_path_override` (từ `--db-path` argparse) — non-empty → dùng nguyên.
       * Absolute → giữ nguyên.
       * Relative → resolve theo CWD của user (KHÁC main.py resolve theo
         `backend/`). Design decision: CLI thường được chạy từ nhiều CWD
         khác nhau khi debug/thao tác data, người vận hành expect
         `--db-path ./local.db` ám chỉ file cạnh CWD hiện tại, không phải
         cạnh `backend/`.
    2. Env `IDEAL_QR_TOOL_DB_PATH` — giống `_resolve_db_path` của main.py:
       relative resolve theo `backend/` để CLI trỏ đúng file với Backend_Service
       khi cùng env.
    3. Default `<backend_root>/runtime/ideal_qr_tool.db` — cùng file mặc định
       với main.py để CLI test đúng data mà server đang phục vụ.

Runtime dirs (`session_cache`, `qr`) DÙNG CHUNG với Backend_Service
(`backend/runtime/session_cache`, `backend/runtime/qr`). Design decision:
CLI thao tác trên DATA THẬT của server (session cache đã hydrate, QR file
đã render) để debug hiệu quả — nếu tách runtime riêng, CLI sẽ mở SQLite
đúng file nhưng thao tác cache/qr trên thư mục khác, gây lệch state.

`bind_host` truyền `"cli-local"` — placeholder có ý nghĩa cho debug output
(`BootstrappedServices.bind_host` echo lại), KHÔNG dùng cho socket bind
(CLI không bind) và chỉ mang tính thông tin.

_Requirements: 15.1, 15.3_
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from app.bootstrap import BootstrappedServices, bootstrap_services


__all__ = ["bootstrap_cli"]


def _ensure_stderr_logging() -> None:
    """Cấu hình logging root một lần → stderr, level WARNING+.

    Cho phép ``logging.exception(...)`` bên trong core (`JobManager`,
    HTTP client...) hiển thị traceback ra stderr khi CLI chạy — hữu ích
    để debug lỗi ngoài `IdealFlowError` hierarchy mà không cần đọc DB log
    riêng. Chỉ cấu hình 1 lần: nếu root logger đã có handler (test
    framework, pytest logging, ...) thì skip để không nhân đôi.
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        root.addHandler(handler)
        # INFO để debug/dev thấy tiến trình flow. Production dùng WARNING —
        # có thể toggle qua env `IDEAL_QR_TOOL_LOG_LEVEL` (nếu cần sau).
        root.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Hằng số nội bộ — GIỮ ĐỒNG BỘ với `app/main.py` (không import chéo để
# tránh lifespan/lazy-init side-effect của main.py rơi vào CLI process).
# Nếu main.py đổi convention, sửa cả 2 nơi (grep `_RUNTIME_SUBDIR`).
# ---------------------------------------------------------------------------

_ENV_DB_PATH = "IDEAL_QR_TOOL_DB_PATH"

_DEFAULT_DB_FILENAME = "ideal_qr_tool.db"
_RUNTIME_SUBDIR = "runtime"
_SESSION_CACHE_SUBDIR = "session_cache"
_QR_OUTPUT_SUBDIR = "qr"

# `bind_host` placeholder cho CLI — chỉ echo lại trong
# `BootstrappedServices.bind_host` để log/debug (auth đã bỏ hoàn toàn,
# không còn kiểm tra bảo mật khởi động nào đọc giá trị này).
_CLI_BIND_HOST_PLACEHOLDER = "cli-local"


def _backend_root() -> Path:
    """Trả về `backend/` — parent của package `app/`.

    File hiện tại nằm ở `backend/app/cli/bootstrap.py` nên
    `Path(__file__).resolve().parent` là `backend/app/cli/`, `.parent` là
    `backend/app/`, `.parent` nữa là `backend/`.
    """
    return Path(__file__).resolve().parent.parent.parent


def _resolve_db_path(db_path_override: str | None) -> Path:
    """Áp dụng priority resolve `db_path`: override → env → default.

    Xem docstring module cho chi tiết từng cấp priority.
    """
    # (1) Override từ `--db-path` — resolve theo CWD của user (không anchor
    # vào `backend/` như env case). `.resolve()` normalize về absolute để
    # `BootstrappedServices.db_path` echo path meaningful và tránh lệ thuộc
    # `os.getcwd()` sau này nếu code hạ tầng đổi CWD.
    if db_path_override is not None and db_path_override.strip():
        return Path(db_path_override.strip()).resolve()

    # (2) Env — logic KHỚP `app/main.py::_resolve_db_path` để CLI và
    # Backend_Service trỏ cùng file khi cùng env.
    raw = os.environ.get(_ENV_DB_PATH, "").strip()
    if raw:
        candidate = Path(raw)
        if candidate.is_absolute():
            return candidate
        return _backend_root() / candidate

    # (3) Default — cùng path với `app/main.py`.
    return _backend_root() / _RUNTIME_SUBDIR / _DEFAULT_DB_FILENAME


def _resolve_session_cache_dir() -> Path:
    """Dùng CHUNG dir với Backend_Service (Requirement 10.1)."""
    return _backend_root() / _RUNTIME_SUBDIR / _SESSION_CACHE_SUBDIR


def _resolve_qr_output_dir() -> Path:
    """Dùng CHUNG dir với Backend_Service (Requirement 7.3, 7.4)."""
    return _backend_root() / _RUNTIME_SUBDIR / _QR_OUTPUT_SUBDIR


async def bootstrap_cli(db_path_override: str | None = None) -> BootstrappedServices:
    """Bootstrap toàn bộ singleton core cho CLI (Requirement 15.1, 15.3).

    Wrapper mỏng — CHỈ resolve 4 path/host theo priority CLI-specific rồi
    delegate 100% vào `bootstrap_services(...)`. KHÔNG copy logic khởi tạo
    singleton hay đăng ký payment module (Requirement 15.3).

    Args:
        db_path_override: Giá trị của flag `--db-path` (argparse). `None`
            hoặc chuỗi rỗng → dùng env `IDEAL_QR_TOOL_DB_PATH` hoặc default
            (xem docstring module cho priority chi tiết).

    Returns:
        `BootstrappedServices` immutable đã hydrate + register xong, sẵn
        sàng cho sub-command CLI (probe, purge, list-jobs, ...).

    Raises:
        Xem `bootstrap_services` — các lỗi setup (namespace/handler
        already-registered, settings validation) vẫn có thể raise nếu DB
        hoặc setup có bug — Fail_Fast_Policy.
    """
    _ensure_stderr_logging()
    return await bootstrap_services(
        db_path=_resolve_db_path(db_path_override),
        bind_host=_CLI_BIND_HOST_PLACEHOLDER,
        session_cache_dir=_resolve_session_cache_dir(),
        qr_output_dir=_resolve_qr_output_dir(),
    )
