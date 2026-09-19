"""Settings admin routes (Requirement 6.5, 6.6, 6.7, 11.2, 11.3, 11.4).

Cung cấp 3 endpoint quản trị Settings_Store — nguồn cấu hình runtime duy
nhất của Backend_Service:

- `GET  /api/settings`           — liệt kê toàn bộ (hoặc theo `?prefix=`)
                                    key-value hiện có (Requirement 11).
- `PUT  /api/settings/{key}`     — ghi 1 key (Requirement 11.3, 11.4).
- `POST /api/settings/bulk`      — ghi nhiều key trong 1 transaction, atomic
                                    (Requirement 11.2 — Fail_Fast rollback
                                    toàn bộ khi 1 item vi phạm).
- `POST /api/settings/telegram/mode` — đường DUY NHẤT hợp lệ để đổi
                                    `telegram.mode`, đi qua Mode_Switch_Guard
                                    (`JobManager.set_operating_mode()`) thay
                                    vì `settings_repo.set()` trực tiếp
                                    (Requirement 3.1, 3.2, 3.3). `PUT
                                    /{key}` và `POST /bulk` từ chối key
                                    `telegram.mode` để không cho bypass.

Payment_Module_Boundary (Requirement 13.5, 13.6): module chỉ import từ
`app.core.*` (gián tiếp qua `deps`) và `app.api.schemas`; KHÔNG import bất
kỳ gì từ `app.payments.*`. Do đó tầng này KHÔNG biết cụ thể key
`ideal.default_issuer` cần đối chiếu với `ideal.known_issuers` — mọi
whitelist/enum/type constraint đều nằm ở `SettingsRepository` qua
constraint registry (đăng ký lúc startup). Route chỉ:

1. Chuyển request xuống repository.
2. Bắt `SettingsValidationError` (whitelist miss / enum miss / type sai /
   range sai) và ánh xạ sang HTTP 400 với body ổn định
   `{"error": "settings_validation_error", "key": ..., "reason": ...}` để
   frontend/CI hiển thị lý do rõ ràng cho user (ví dụ Requirement 6.5-6.7:
   `ideal.default_issuer` không thuộc whitelist `ideal.known_issuers`).

Auth: đã gỡ bỏ hoàn toàn — tool chạy trên mạng nội bộ tin cậy, không còn
kiểm tra token ở tầng route.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from fastapi import Header

from app.api import deps
from app.notifiers.telegram.live_qr_gate import (
    KEY_CHAT_TARGETS,
    KEY_LIVE_ENABLED,
    KEY_LIVE_MAX_PER_CHAT,
    KEY_SUCCESS_WAIT_ENABLED,
    normalize_push_mode,
)

_LIVE_QR_REFRESH_KEYS = frozenset(
    {
        KEY_LIVE_ENABLED,
        KEY_LIVE_MAX_PER_CHAT,
        KEY_CHAT_TARGETS,
        "telegram.push_mode.enabled",
    }
)


async def _refresh_live_qr_gate_config(written_keys: list[str]) -> None:
    """Reload LiveQrGate cache after settings that affect capacity/blocked.

    Capacity shrink (lower max / disable chat) can flip blocked False→True
    without an acquire — refresh fires the pause edge.
    """
    if not any(k in _LIVE_QR_REFRESH_KEYS for k in written_keys):
        return
    try:
        from app.api import routes_notifications

        notifier = getattr(routes_notifications, "_telegram_notifier", None)
        gate = getattr(notifier, "live_qr_gate", None) if notifier else None
        if gate is None:
            return
        await gate.refresh_config()
    except Exception:  # noqa: BLE001
        pass
# `SettingsApplicable` là Protocol structural định nghĩa trong `deps` (tầng
# api/) — dùng để type UPI license pool mà KHÔNG import `app.payments.*`
# (Payment_Module_Boundary, R13.5/R13.6, enforced qua architecture test).
from app.api.deps import SettingsApplicable
from app.api.schemas import (
    SetTelegramModeRequest,
    SetTelegramModeResponse,
    SettingUpdateRequest,
    SettingUpdateResponse,
    SettingsBulkRequest,
    SettingsResponse,
)
from app.core.errors import ModeSwitchBlockedError, SettingsValidationError
from app.core.job_manager import JobManager
from app.core.proxy_pool import ProxyPool
from app.core.session_cache import AccountSessionCache
from app.core.settings_store import SettingsRepository
from app.core.sse import SseBroadcaster

# Mã lỗi ổn định — frontend/CI dựa vào để nhận diện lỗi validate Settings
# (whitelist miss / type sai / enum miss / range sai). KHÔNG đổi giá trị
# chuỗi này mà không cập nhật cả phía consumer.
_SETTINGS_VALIDATION_ERROR_CODE = "settings_validation_error"

# `telegram.mode` KHÔNG được phép ghi qua `PUT /{key}`/`POST /bulk` — key
# này PHẢI đi qua `POST /telegram/mode` để chạy Mode_Switch_Guard
# (`JobManager.set_operating_mode()`), nếu không sẽ bypass hoàn toàn việc
# kiểm tra job Pull_Mode đang dở dang (Requirement 3.1, 3.2, 3.3).
_TELEGRAM_MODE_KEY = "telegram.mode"
_TELEGRAM_MODE_BYPASS_REASON = (
    "key 'telegram.mode' không được ghi trực tiếp qua endpoint này — dùng "
    "POST /api/settings/telegram/mode để đổi mode an toàn qua "
    "Mode_Switch_Guard."
)

router = APIRouter(prefix="/api/settings", tags=["settings"])


# Namespace của các key runtime cần "write-through" reload vào singleton
# service tương ứng ngay sau khi persist vào DB. Nếu bỏ hook này, khi user
# save proxy qua UI thì DB có nhưng `ProxyPool._proxies` runtime vẫn empty
# → mọi job chạy Direct_Mode → fail vì ChatGPT/Stripe từ chối IP thẳng.
# CLI không gặp bug này vì bootstrap_services chạy `apply_settings` mỗi
# lần khởi động; server web startup 1 lần rồi phục vụ nhiều request.
_PROXY_NAMESPACE = "proxy"
_SESSION_CACHE_NAMESPACE = "session_cache"
_UPI_NAMESPACE = "upi"


async def _write_through_runtime_state(
    keys: list[str],
    settings_repo: SettingsRepository,
    proxy_pool: ProxyPool,
    session_cache: AccountSessionCache,
    job_manager: JobManager,
    upi_license_pool: SettingsApplicable,
) -> None:
    """Reload runtime service từ DB nếu 1 trong `keys` thuộc namespace quản lý.

    Hook idempotent — gọi thừa (không có key runtime) chỉ tốn 1 DB read
    empty, không side-effect. Fail-safe: nếu 1 service raise trong
    `apply_settings`, log-and-continue để không rollback write DB đã
    thành công (semantic: DB là source of truth, runtime chỉ là cache).
    """

    touched_prefixes = {key.split(".", 1)[0] for key in keys if "." in key}

    if _PROXY_NAMESPACE in touched_prefixes:
        snapshot = await settings_repo.list(_PROXY_NAMESPACE)
        try:
            proxy_pool.apply_settings(snapshot)
        except Exception:
            import logging as _logging
            _logging.getLogger(__name__).exception(
                "proxy_pool.apply_settings failed after settings update"
            )
        else:
            # Sau khi apply_settings thành công + user thực sự đổi `proxy.list`,
            # kick preflight batch để mark_dead sớm các proxy chết mới nhập.
            # Chạy background task để không block HTTP response — endpoint
            # `POST /api/settings` phải trả 2xx ngay. Preflight tự log kết
            # quả qua logger; UI sẽ thấy proxy dead qua `GET /api/proxy/pool`
            # sau khi batch xong (~30s worst case).
            if any(k == "proxy.list" for k in keys):
                import asyncio as _asyncio
                import logging as _logging_pfl
                from app.core.proxy_health import probe_pool_batch

                _preflight_logger = _logging_pfl.getLogger(
                    "app.settings.preflight"
                )
                _task = _asyncio.create_task(
                    probe_pool_batch(
                        proxy_pool,
                        proxy_pool.probe_config,
                        logger=_preflight_logger,
                    ),
                    name="proxy-preflight-on-save",
                )

                def _pfl_done(task: "_asyncio.Task") -> None:
                    if task.cancelled():
                        return
                    exc = task.exception()
                    if exc is not None:
                        _preflight_logger.exception(
                            "preflight batch on save failed", exc_info=exc
                        )

                _task.add_done_callback(_pfl_done)

    if _SESSION_CACHE_NAMESPACE in touched_prefixes:
        snapshot = await settings_repo.list(_SESSION_CACHE_NAMESPACE)
        try:
            session_cache.apply_settings(snapshot)
        except Exception:
            import logging as _logging
            _logging.getLogger(__name__).exception(
                "session_cache.apply_settings failed after settings update"
            )

    # UPI license pool — cùng semantic proxy_pool: khi user đổi `upi.license_codes`
    # (hoặc key `upi.*` khác) qua Settings API, re-apply vào pool runtime ngay.
    # Không có branch này thì code license mới nhập chỉ vào DB, pool vẫn rỗng →
    # mọi job UPI fail `upi_no_license_credit` cho tới khi restart. Fail-safe:
    # log-and-continue, KHÔNG rollback write DB đã thành công (DB là source of
    # truth, pool chỉ là cache RAM). apply_settings đồng bộ, không verify egress.
    if _UPI_NAMESPACE in touched_prefixes:
        snapshot = await settings_repo.list(_UPI_NAMESPACE)
        try:
            upi_license_pool.apply_settings(snapshot)
        except Exception:
            import logging as _logging
            _logging.getLogger(__name__).exception(
                "upi_license_pool.apply_settings failed after settings update"
            )

    # Concurrency runtime — nếu 1 trong `keys` là concurrency key của
    # handler đã đăng ký (VD `ideal.max_concurrent`), gọi
    # `update_max_concurrent()` để Semaphore trong `JobManager` phản ánh
    # ngay giá trị mới. Nếu bỏ hook này thì:
    #   - Multi 1 → 2: DB đổi, Semaphore vẫn 1 → job pending thứ 2 không
    #     bao giờ được kick vào running dù có slot logic.
    #   - Multi 3 → 2: DB đổi, Semaphore vẫn 3 → user vẫn thấy 3 job chạy.
    # `maybe_reload_concurrency` tự no-op nếu `keys` không chạm concurrency
    # key nào — không cần điều kiện thêm ở đây.
    try:
        await job_manager.maybe_reload_concurrency(keys)
    except Exception:
        import logging as _logging
        _logging.getLogger(__name__).exception(
            "job_manager.maybe_reload_concurrency failed after settings update"
        )


# Mã lỗi ổn định cho 409 khi Mode_Switch_Guard chặn đổi `telegram.mode` vì
# còn job Pull_Mode đang dở dang và request không có `force=true`.
_MODE_SWITCH_BLOCKED_ERROR_CODE = "mode_switch_blocked"


def _mode_switch_blocked_response(err: ModeSwitchBlockedError) -> JSONResponse:
    """Chuẩn hoá body 409 khi `JobManager.set_operating_mode()` bị chặn.

    Format ổn định:

        {
            "error": "mode_switch_blocked",
            "job_ids": ["<job_id>", ...]
        }

    409 Conflict là mã ngữ nghĩa đúng cho "hành động bị chặn do trạng thái
    hiện tại xung đột" — khác 400 (request tự nó sai) dùng cho
    `SettingsValidationError`. Frontend dựa vào `job_ids` để hiển thị danh
    sách job đang dở dang và hỏi user có muốn `force=true` không.
    """

    return JSONResponse(
        status_code=409,
        content={
            "error": _MODE_SWITCH_BLOCKED_ERROR_CODE,
            "job_ids": err.job_ids,
        },
    )


def _settings_validation_response(err: SettingsValidationError) -> JSONResponse:
    """Chuẩn hoá body 400 khi `SettingsRepository` từ chối key/value.

    Format ổn định:

        {
            "error": "settings_validation_error",
            "key":   "<key vi phạm>",
            "reason": "<mô tả cụ thể theo Fail_Fast_Policy>"
        }

    Dùng chung cho cả `PUT /{key}` và `POST /bulk` để frontend chỉ cần 1
    handler lỗi duy nhất (Requirement 11.4).
    """

    return JSONResponse(
        status_code=400,
        content={
            "error": _SETTINGS_VALIDATION_ERROR_CODE,
            "key": err.key,
            "reason": err.reason,
        },
    )


@router.get("", response_model=SettingsResponse)
async def list_settings(
    prefix: str | None = None,
    settings_repo: SettingsRepository = Depends(deps.get_settings_repo),
) -> SettingsResponse:
    """Liệt kê Settings hiện có (Requirement 11).

    Args:
        prefix: Nếu có, lọc theo namespace (ví dụ `prefix=ideal` chỉ trả
            key thuộc `ideal.*`). `None`/không truyền → trả toàn bộ.
        settings_repo: Singleton `SettingsRepository` inject qua `deps`.

    Returns:
        `SettingsResponse` với `settings` là dict `{key: value}` đã decode
        JSON — kiểu của mỗi value phụ thuộc `TypeConstraint` của key đó.
    """

    settings_map = await settings_repo.list(prefix=prefix)
    return SettingsResponse(settings=settings_map)


@router.post("/telegram/mode", response_model=SetTelegramModeResponse)
async def set_telegram_mode(
    payload: SetTelegramModeRequest,
    x_client_id: str | None = Header(default=None, alias="X-Client-Id"),
    job_manager: JobManager = Depends(deps.get_job_manager),
    settings_repo: SettingsRepository = Depends(deps.get_settings_repo),
    sse: SseBroadcaster = Depends(deps.get_sse),
) -> Any:
    """Đổi `telegram.mode` an toàn qua Mode_Switch_Guard (Requirement 3).

    Đây là con đường DUY NHẤT được phép đổi `telegram.mode` qua API — route
    KHÔNG bao giờ gọi `settings_repo.set("telegram.mode", ...)` trực tiếp,
    luôn đi qua `JobManager.set_operating_mode()` để Guard kiểm tra job
    Pull_Mode đang dở dang trước khi cho phép đổi (R3.1, R3.2, R3.3).

    Args:
        payload: Body `{"mode": "push"|"pull", "force": bool}`.
        job_manager: Singleton `JobManager` inject qua `deps` — nắm
            `set_operating_mode()` (Mode_Switch_Guard).
        settings_repo: Singleton `SettingsRepository` — dùng đọc lại giá
            trị đã persist sau khi Guard hoàn tất, cùng convention với
            `update_setting`.

    Returns:
        - 200 với `SetTelegramModeResponse(mode=<giá trị đã persist>)` khi
          đổi mode thành công ngay (không có job dở dang) hoặc thành công
          sau khi `force=true` đã force-fail hết job dở dang (R3.2, R3.3).
        - 400 với body `{"error": "settings_validation_error", "key":
          "telegram.mode", "reason": ...}` khi `mode` không thuộc enum
          `{"push", "pull"}`.
        - 409 với body `{"error": "mode_switch_blocked", "job_ids": [...]}`
          khi còn job Pull_Mode đang `assigned`/`pending` và `force=False`
          (R3.1) — frontend hiển thị danh sách `job_ids` và hỏi user có
          muốn `force=true` không.

    Note:
        `resolve_pull_outcome`/data-integrity error (`JobNotFoundError`,
        `JobAlreadyResolvedError`) khi `force=true` KHÔNG được catch ở đây
        — propagate thành 500 để Fail_Fast (R3.5, mode KHÔNG được đổi
        trong trường hợp này), khác `ModeSwitchBlockedError`/
        `SettingsValidationError` là lỗi "mong đợi" của endpoint này.
    """

    try:
        await job_manager.set_operating_mode(payload.mode, force=payload.force)
    except ModeSwitchBlockedError as err:
        return _mode_switch_blocked_response(err)
    except SettingsValidationError as err:
        return _settings_validation_response(err)

    persisted_value = await settings_repo.get("telegram.mode")

    # Broadcast SSE `setting_updated` — các client khác đang xem UI (VD tab
    # web khác, CLI đang theo dõi) sync ngay trạng thái mode mới, cùng
    # convention với `update_setting` (Requirement 12.7).
    await sse.broadcast_setting_updated(
        key="telegram.mode",
        value=persisted_value,
        source_client_id=x_client_id,
    )

    return SetTelegramModeResponse(mode=persisted_value)


@router.put("/{key}")
async def update_setting(
    key: str,
    payload: SettingUpdateRequest,
    x_client_id: str | None = Header(default=None, alias="X-Client-Id"),
    settings_repo: SettingsRepository = Depends(deps.get_settings_repo),
    proxy_pool: ProxyPool = Depends(deps.get_proxy_pool),
    session_cache: AccountSessionCache = Depends(deps.get_session_cache),
    sse: SseBroadcaster = Depends(deps.get_sse),
    job_manager: JobManager = Depends(deps.get_job_manager),
    upi_license_pool: SettingsApplicable = Depends(deps.get_upi_license_pool),
) -> Any:
    """Ghi 1 key vào Settings_Store (Requirement 11.3, 11.4).

    Route KHÔNG tự validate — mọi kiểm tra whitelist/type/enum/range đều do
    `SettingsRepository.set()` đảm nhiệm. Khi vi phạm, repository raise
    `SettingsValidationError` với `key` + `reason` cụ thể và KHÔNG ghi đè
    giá trị cũ; route ánh xạ sang HTTP 400 theo shape ổn định.

    Args:
        key: Path param, ví dụ `ideal.default_issuer`.
        payload: Body `{"value": ...}`. `value` cố ý dùng `Any`, việc kiểm
            tra type tuỳ theo `TypeConstraint` đã đăng ký cho key đó.
        settings_repo: Singleton `SettingsRepository` inject qua `deps`.

    Returns:
        - 200 với `SettingUpdateResponse(key=..., value=<giá trị đã persist>)`
          khi ghi thành công. Value trả về đọc lại từ store để phản ánh
          đúng dạng đã lưu (`SettingsRepository.get`) — tránh trả lại giá
          trị request thô nếu repository decode/normalise khác đi trong
          tương lai.
        - 400 với body `{"error": "settings_validation_error", "key": ...,
          "reason": ...}` khi vi phạm whitelist/type constraint.
    """

    if key == _TELEGRAM_MODE_KEY:
        return _settings_validation_response(
            SettingsValidationError(_TELEGRAM_MODE_KEY, _TELEGRAM_MODE_BYPASS_REASON)
        )

    # Live QR Gate ↔ Success Wait mutex: enabling one auto-clears the other.
    keys_to_write: dict[str, Any] = {key: payload.value}
    if key in (KEY_LIVE_ENABLED, KEY_SUCCESS_WAIT_ENABLED):
        current = {
            KEY_LIVE_ENABLED: await settings_repo.get(KEY_LIVE_ENABLED),
            KEY_SUCCESS_WAIT_ENABLED: await settings_repo.get(
                KEY_SUCCESS_WAIT_ENABLED
            ),
        }
        keys_to_write = normalize_push_mode({key: payload.value}, current)

    try:
        if len(keys_to_write) == 1 and key in keys_to_write:
            await settings_repo.set(key, keys_to_write[key])
        else:
            await settings_repo.bulk_set(keys_to_write)
    except SettingsValidationError as err:
        return _settings_validation_response(err)

    # Write-through: reload ProxyPool/SessionCache/JobManager runtime nếu
    # key thuộc namespace của service tương ứng. Không reload → DB có
    # proxy mới nhưng runtime pool vẫn empty (job fail Direct_Mode); DB
    # có `ideal.max_concurrent=2` nhưng Semaphore vẫn 1 (job pending kế
    # tiếp không được kick).
    written_keys = list(keys_to_write.keys())
    await _write_through_runtime_state(
        written_keys,
        settings_repo,
        proxy_pool,
        session_cache,
        job_manager,
        upi_license_pool,
    )

    persisted_value = await settings_repo.get(key)

    # Broadcast SSE `setting_updated` — nhiều client cùng xem tool sẽ sync
    # state real-time (VD 2 tab web + 1 CLI cùng backend). `source_client_id`
    # cho phép client tự lọc echo của chính mình để tránh feedback loop
    # (client A edit → server broadcast → A nhận SSE → skip nếu client_id
    # trùng để không mất caret/cursor position trong textarea).
    for k in written_keys:
        v = await settings_repo.get(k)
        await sse.broadcast_setting_updated(
            key=k, value=v, source_client_id=x_client_id
        )

    await _refresh_live_qr_gate_config(written_keys)

    return SettingUpdateResponse(key=key, value=persisted_value)


@router.post("/bulk")
async def bulk_update_settings(
    payload: SettingsBulkRequest,
    x_client_id: str | None = Header(default=None, alias="X-Client-Id"),
    settings_repo: SettingsRepository = Depends(deps.get_settings_repo),
    proxy_pool: ProxyPool = Depends(deps.get_proxy_pool),
    session_cache: AccountSessionCache = Depends(deps.get_session_cache),
    sse: SseBroadcaster = Depends(deps.get_sse),
    job_manager: JobManager = Depends(deps.get_job_manager),
    upi_license_pool: SettingsApplicable = Depends(deps.get_upi_license_pool),
) -> Any:
    """Ghi nhiều key trong 1 transaction (Requirement 11.2).

    Atomic: `SettingsRepository.bulk_set()` validate TOÀN BỘ `items` trước
    khi ghi bất kỳ item nào. Nếu 1 item vi phạm whitelist/type constraint,
    raise `SettingsValidationError` ngay và KHÔNG ghi item nào (Fail_Fast,
    không partial-write). Nếu lỗi I/O giữa chừng, connection rollback.

    Args:
        payload: Body `{"items": {key1: val1, key2: val2, ...}}`.
        settings_repo: Singleton `SettingsRepository` inject qua `deps`.

    Returns:
        - 200 với `{"applied": {key: value}}` khi toàn bộ items được ghi
          thành công. `applied` chứa giá trị đã persist đọc lại từ store
          (`bulk_get`), để frontend cập nhật store optimistic đúng dạng
          canonical thay vì payload thô.
        - 400 với body `{"error": "settings_validation_error", "key": ...,
          "reason": ...}` khi 1 item vi phạm — trong trường hợp này KHÔNG
          có item nào được ghi.
    """

    if _TELEGRAM_MODE_KEY in payload.items:
        return _settings_validation_response(
            SettingsValidationError(_TELEGRAM_MODE_KEY, _TELEGRAM_MODE_BYPASS_REASON)
        )

    # Live QR Gate ↔ Success Wait mutex on bulk path (auto-clear).
    items = dict(payload.items)
    if KEY_LIVE_ENABLED in items or KEY_SUCCESS_WAIT_ENABLED in items:
        current = {
            KEY_LIVE_ENABLED: await settings_repo.get(KEY_LIVE_ENABLED),
            KEY_SUCCESS_WAIT_ENABLED: await settings_repo.get(
                KEY_SUCCESS_WAIT_ENABLED
            ),
        }
        items = {**items, **normalize_push_mode(items, current)}

    try:
        await settings_repo.bulk_set(items)
    except SettingsValidationError as err:
        return _settings_validation_response(err)

    # Write-through: reload ProxyPool/SessionCache/JobManager runtime nếu
    # batch chứa key thuộc namespace của service tương ứng. Fix bug
    # "web save proxy xong job vẫn fail" — DB có proxy nhưng
    # ProxyPool._proxies runtime rỗng nếu không reload — và bug
    # "chọn Multi 20 nhưng chỉ chạy 1" — DB có `ideal.max_concurrent=20`
    # nhưng Semaphore vẫn 1 nếu không reload.
    await _write_through_runtime_state(
        list(items.keys()),
        settings_repo,
        proxy_pool,
        session_cache,
        job_manager,
        upi_license_pool,
    )

    applied = await settings_repo.bulk_get(list(items.keys()))

    # Broadcast SSE cho mỗi key được ghi — client khác đồng bộ state runtime
    # ngay (VD proxy modal edit trên tab A → tab B refresh badge số proxy).
    for k, v in applied.items():
        await sse.broadcast_setting_updated(
            key=k, value=v, source_client_id=x_client_id
        )

    await _refresh_live_qr_gate_config(list(items.keys()))

    return {"applied": applied}
