"""Dependency wiring cho tầng `api/` (Requirement 14.7).

Module này đóng vai trò "service locator" duy nhất cho các FastAPI router:
mọi router import dependency function ở đây (`get_settings_repo`,
`get_job_manager`, …) thay vì tự khởi tạo hay đọc biến toàn cục ở nơi khác.
Các singleton core (`SettingsRepository`, `JobManager`, `ProxyPool`,
`AccountSessionCache`, `SseBroadcaster`) được `main.py` (task 26.1) khởi tạo
1 lần ở startup và bơm vào đây qua `configure_services(...)`.

Payment_Module_Boundary (Requirement 13.5, 13.6): module chỉ import từ
`app.core.*`, tuyệt đối KHÔNG import bất kỳ gì từ `app.payments.*` — tầng
`api/` không được biết đến chi tiết payment method cụ thể.

Fail_Fast_Policy (Requirement 14.2): các dependency getter raise
`RuntimeError` ngay khi bị gọi trước lúc `configure_services` chạy — thay
vì trả `None` hoặc tự khởi tạo lười biếng có thể che giấu lỗi wiring ở
startup.
"""

from __future__ import annotations

from typing import Protocol

from app.core.job_manager import JobManager
from app.core.proxy_pool import ProxyPool
from app.core.session_cache import AccountSessionCache
from app.core.settings_store import SettingsRepository
from app.core.sse import SseBroadcaster


class SettingsApplicable(Protocol):
    """Structural type cho 1 service runtime hydrate từ settings snapshot.

    Payment_Module_Boundary (Requirement 13.5, 13.6): tầng `api/` KHÔNG được
    import `app.payments.*` (enforced qua `test_architecture_core_boundary`).
    `UpiLicensePool` sống trong `app.payments.upi.*` — KHÁC `ProxyPool` (ở
    `app.core`) nên KHÔNG thể mirror getter proxy bằng import trực tiếp. api/
    tham chiếu pool qua Protocol structural payment-agnostic này: chỉ biết
    "có `apply_settings(snapshot)`", KHÔNG biết payment method cụ thể. Object
    thật (`UpiLicensePool`) do `bootstrap` bơm vào qua `configure_services`.
    """

    def apply_settings(self, snapshot: dict) -> None: ...

_NOT_CONFIGURED_MESSAGE = (
    "Dependency chưa được cấu hình — gọi `configure_services(...)` "
    "tại startup của FastAPI app trước khi phục vụ request."
)

# Module-level singleton state. Được set 1 lần duy nhất tại
# `configure_services(...)` chạy trong startup hook của `main.py`; các
# dependency getter đọc từ đây (không tự khởi tạo lười biếng để tránh che
# giấu lỗi wiring).
_settings_repo: SettingsRepository | None = None
_job_manager: JobManager | None = None
_proxy_pool: ProxyPool | None = None
_session_cache: AccountSessionCache | None = None
_sse: SseBroadcaster | None = None
_upi_license_pool: SettingsApplicable | None = None


def configure_services(
    settings: SettingsRepository,
    job_manager: JobManager,
    proxy_pool: ProxyPool,
    session_cache: AccountSessionCache,
    sse: SseBroadcaster,
    upi_license_pool: SettingsApplicable,
) -> None:
    """Inject các singleton core vào module-level state.

    Được gọi bởi `main.py` (task 26.1) ở startup FastAPI app, sau khi các
    core service đã khởi tạo và hydrate `apply_settings` xong.

    Cho phép gọi lại (test/reload) — thay thế state hiện có.

    Args:
        settings: Instance duy nhất của `SettingsRepository`.
        job_manager: Instance duy nhất của `JobManager`.
        proxy_pool: Instance duy nhất của `ProxyPool`.
        session_cache: Instance duy nhất của `AccountSessionCache`.
        sse: Instance duy nhất của `SseBroadcaster`.
        upi_license_pool: Instance duy nhất của `UpiLicensePool` (dựng trong
            `register_upi_handler`, hydrate ở `bootstrap_services`) — typed qua
            `SettingsApplicable` để giữ boundary api/. Bơm vào đây để
            write-through settings API có thể re-apply `upi.*`.
    """

    global _settings_repo, _job_manager, _proxy_pool, _session_cache, _sse
    global _upi_license_pool

    _settings_repo = settings
    _job_manager = job_manager
    _proxy_pool = proxy_pool
    _session_cache = session_cache
    _sse = sse
    _upi_license_pool = upi_license_pool


def get_settings_repo() -> SettingsRepository:
    """FastAPI dependency trả về `SettingsRepository` singleton."""

    if _settings_repo is None:
        raise RuntimeError(_NOT_CONFIGURED_MESSAGE)
    return _settings_repo


def get_job_manager() -> JobManager:
    """FastAPI dependency trả về `JobManager` singleton."""

    if _job_manager is None:
        raise RuntimeError(_NOT_CONFIGURED_MESSAGE)
    return _job_manager


def get_proxy_pool() -> ProxyPool:
    """FastAPI dependency trả về `ProxyPool` singleton."""

    if _proxy_pool is None:
        raise RuntimeError(_NOT_CONFIGURED_MESSAGE)
    return _proxy_pool


def get_session_cache() -> AccountSessionCache:
    """FastAPI dependency trả về `AccountSessionCache` singleton."""

    if _session_cache is None:
        raise RuntimeError(_NOT_CONFIGURED_MESSAGE)
    return _session_cache


def get_sse() -> SseBroadcaster:
    """FastAPI dependency trả về `SseBroadcaster` singleton."""

    if _sse is None:
        raise RuntimeError(_NOT_CONFIGURED_MESSAGE)
    return _sse


def get_upi_license_pool() -> SettingsApplicable:
    """FastAPI dependency trả về UPI license pool singleton (structural type).

    Dùng bởi settings routes để write-through `upi.*` vào pool runtime (cùng
    pattern `get_proxy_pool`). Trả về `SettingsApplicable` thay vì
    `UpiLicensePool` để api/ KHÔNG import `app.payments.*` (Payment_Module_
    Boundary). Fail_Fast_Policy: raise `RuntimeError` nếu gọi trước
    `configure_services`.
    """

    if _upi_license_pool is None:
        raise RuntimeError(_NOT_CONFIGURED_MESSAGE)
    return _upi_license_pool
