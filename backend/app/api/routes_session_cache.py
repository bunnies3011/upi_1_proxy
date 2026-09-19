"""Session cache admin routes (Requirement 10.8, 10.9).

Cung cấp 2 endpoint xoá cache session accounts được `AccountSessionCache`
lưu trên đĩa. Đây là 2 thao tác quản trị (không phải flow chạy job) — dùng
để user hoặc CI reset trạng thái phiên trước khi rerun đăng ký:

- `DELETE /api/session-cache/{account_key}` — clear 1 account (R10.8).
- `DELETE /api/session-cache` — clear toàn bộ (R10.9).

Cả 2 endpoint đều **idempotent**: `AccountSessionCache.clear()` /
`clear_all()` không raise khi file không tồn tại và chỉ log warning cho lỗi
I/O khác (Requirement 10.7). Vì vậy endpoint HTTP luôn trả 200 khi auth
hợp lệ — không có "not found" case cho session cache admin.

Auth: đã gỡ bỏ hoàn toàn — tool chạy trên mạng nội bộ tin cậy, không còn
kiểm tra token ở tầng route.

Payment_Module_Boundary (Requirement 13.5, 13.6): module chỉ import từ
`app.core.*` (gián tiếp qua `deps`) và `app.api.schemas`; KHÔNG import bất
kỳ gì từ `app.payments.*`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api import deps
from app.api.schemas import ClearAllSessionCacheResponse, ClearSessionCacheResponse

router = APIRouter(prefix="/api/session-cache", tags=["session-cache"])


@router.delete("/{account_key}", response_model=ClearSessionCacheResponse)
async def clear_session_cache(
    account_key: str,
    session_cache=Depends(deps.get_session_cache),
) -> ClearSessionCacheResponse:
    """Xoá bản ghi cache của 1 account (Requirement 10.8).

    Idempotent — `AccountSessionCache.clear()` không raise khi key không
    tồn tại và chỉ log warning cho lỗi I/O khác, nên endpoint luôn trả về
    `cleared=True` khi auth hợp lệ.

    Args:
        account_key: Định danh account (email hoặc key hash) — được truyền
            nguyên vẹn xuống `AccountSessionCache` để định vị file cache.
        session_cache: Singleton `AccountSessionCache` inject qua `deps`.

    Returns:
        `ClearSessionCacheResponse` với `account_key` echo lại và
        `cleared=True`.
    """

    await session_cache.clear(account_key)
    return ClearSessionCacheResponse(account_key=account_key, cleared=True)


@router.delete("", response_model=ClearAllSessionCacheResponse)
async def clear_all_session_cache(
    session_cache=Depends(deps.get_session_cache),
) -> ClearAllSessionCacheResponse:
    """Xoá toàn bộ cache session của mọi account (Requirement 10.9).

    Best-effort: `AccountSessionCache.clear_all()` duyệt qua mọi file cache
    và cố gắng xoá; file nào lỗi I/O chỉ log warning và tiếp tục — endpoint
    KHÔNG raise. Nếu thư mục cache chưa tồn tại thì no-op (idempotent).

    Args:
        session_cache: Singleton `AccountSessionCache` inject qua `deps`.

    Returns:
        `ClearAllSessionCacheResponse` với `cleared_all=True`.
    """

    await session_cache.clear_all()
    return ClearAllSessionCacheResponse(cleared_all=True)
