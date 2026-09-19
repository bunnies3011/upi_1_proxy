"""Jobs API routes (Requirement 7.4, 7.5, 8.1, 8.2, 8.6, 8.7, 12.3).

Cung cấp 5 endpoint để Frontend_App tạo/liệt kê/xem chi tiết/dừng job và
tải file QR PNG:

- `POST /api/jobs` — submit batch account lines (R8.1, R8.2).
- `GET /api/jobs` — danh sách job compact cho JobList (R12.3, R12.8).
- `GET /api/jobs/{job_id}` — chi tiết 1 job cho JobDetailPanel (R12.3).
- `DELETE /api/jobs/{job_id}` — stop 1 job (R8.6).
- `GET /api/jobs/{job_id}/qr.png` — trả binary `image/png` (R7.4, R7.5).

Payment_Module_Boundary (Requirement 13.5, 13.6): module chỉ import từ
`app.core.*` (gián tiếp qua `deps`) và `app.api.schemas`; KHÔNG import bất
kỳ gì từ `app.payments.*`. Toàn bộ tương tác với payment logic đi qua
interface generic `JobManager` — router không biết "ideal" hay bất kỳ
payment method nào cụ thể ngoài default trong schema.

Auth: đã gỡ bỏ hoàn toàn — tool chạy trên mạng nội bộ tin cậy, không còn
kiểm tra token ở tầng route.

Sensitive_Data_Redaction (Requirement 14.8, R12.3): dòng account thô
(`Job.account_line`) chứa password/token/totp; API layer LUÔN mask qua
`mask_account_line()` trước khi phát ra `JobViewCompact`/`JobViewDetail`.
Log entries đã được redact ở tầng core (`JobManager._append_log` +
`SseBroadcaster.broadcast`), API layer chuyển tiếp nguyên vẹn.

Error shape (Requirement 14.2, tương thích `ErrorResponse` schema): mọi
lỗi (404 `job_not_found`, 409 `qr_not_ready`, 400 `unknown_payment_method`)
đều trả body JSON theo cấu trúc `{"error_code", "message", "details"}`
để frontend/CI phân loại lỗi bằng `error_code` ổn định (không dựa vào
`message` — có thể i18n).
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response

from app.api import deps
from app.api.schemas import (
    JobLogEntry,
    JobViewCompact,
    JobViewDetail,
    SkippedLineItem,
    StopJobResponse,
    SubmitJobsRequest,
    SubmitJobsResponse,
    mask_account_line,
)
from app.core.job_manager import JobManager, UnknownPaymentMethodError
from app.core.payment_flow import JobStatus

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _job_not_found_error(job_id: str) -> dict[str, Any]:
    """Build body cho lỗi 404 khi job_id không tồn tại (R14.2).

    Trả `dict` (không phải `ErrorResponse` instance) vì HTTPException.detail
    yêu cầu JSON-serializable value; FastAPI tự bọc thành `{"detail": ...}`
    khi raise HTTPException, còn khi trả qua `JSONResponse` (qr.png) thì
    dùng nguyên vẹn dict này làm body.
    """

    return {
        "error_code": "job_not_found",
        "message": f"Job '{job_id}' does not exist",
        "details": {"job_id": job_id},
    }


def _log_entry_from_dict(entry: dict[str, Any]) -> JobLogEntry:
    """Chuyển log entry dạng dict (từ `_JobRecord.logs`) sang `JobLogEntry`.

    `_JobRecord.logs` lưu mỗi entry dạng `{"ts": float, "message": str, **extra}`
    (theo `JobManager._append_log`); tách 2 field cố định `ts`/`message` ra,
    phần còn lại đưa vào `extra` để giữ nguyên metadata đã redact ở tầng
    core. Fallback `ts=0.0`/`message=""` nếu key thiếu (không raise) — API
    layer phải fail-safe để không phá JobDetailPanel khi buffer log dị
    dạng vì lý do nào đó.
    """

    ts_raw = entry.get("ts", 0.0)
    try:
        ts = float(ts_raw)
    except (TypeError, ValueError):
        ts = 0.0
    message_raw = entry.get("message", "")
    message = message_raw if isinstance(message_raw, str) else str(message_raw)
    extra = {k: v for k, v in entry.items() if k not in ("ts", "message")}
    return JobLogEntry(ts=ts, message=message, extra=extra)


def _render_dynamic_qr_png(data: str) -> bytes:
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_L
    except ImportError as exc:  # pragma: no cover - dependency is pinned.
        raise RuntimeError("qrcode_missing") from exc

    try:
        qr = qrcode.QRCode(error_correction=ERROR_CORRECT_L, box_size=10, border=4)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:
        raise RuntimeError(f"qr_encode_failed:{exc.__class__.__name__}") from exc


@router.post("/stop-all", response_model=None)
async def stop_all_jobs(
    job_manager: JobManager = Depends(deps.get_job_manager),
) -> JSONResponse:
    """Dừng tất cả job đang pending/running (bulk).

    Iterate qua danh sách job hiện tại, gọi `stop()` cho các job có status
    ∈ {`pending`, `running`}. Job đã terminal (qr_ready/error/stopped) →
    skip (idempotent).
    """
    from app.core.payment_flow import JobStatus as _JobStatus

    stopped: list[str] = []
    for record in job_manager.list_jobs():
        if record.status in (_JobStatus.PENDING, _JobStatus.RUNNING):
            await job_manager.stop(record.job.job_id)
            stopped.append(record.job.job_id)
    return JSONResponse(
        status_code=200,
        content={"stopped_count": len(stopped), "stopped_ids": stopped},
    )


@router.post("/start-all", response_model=None)
async def start_all_held_jobs(
    job_manager: JobManager = Depends(deps.get_job_manager),
) -> JSONResponse:
    """Bulk start mọi job đang `pending + held=True` (feature Add / Run
    tách bạch).

    Iterate qua danh sách job hiện tại, gọi `start_job` cho job pending
    + held=True. Job không thuộc diện đó → skip (idempotent). Trả về
    danh sách `job_id` vừa được start theo thứ tự `order` tăng dần.

    Không cần body — mọi held job sẽ được start. Nếu muốn start có
    filter, dùng `POST /api/jobs/{id}/start` cho từng job.
    """
    started_ids = await job_manager.start_all_held()
    return JSONResponse(
        status_code=200,
        content={"started_count": len(started_ids), "started_ids": started_ids},
    )


@router.post("/{job_id}/start", response_model=None)
async def start_held_job(
    job_id: str,
    job_manager: JobManager = Depends(deps.get_job_manager),
) -> JSONResponse:
    """Start 1 job đang `pending + held=True` (feature Add / Run tách bạch).

    - Job không tồn tại → 404 `job_not_found`.
    - Job không ở `pending` (đã RUNNING / terminal) → 409 `job_not_held`.
    - Job pending + held=False (đã start) → 409 `job_not_held` (idempotent
      không phân biệt "đã start" và "chưa từng held" — cả 2 đều là "không
      cần start nữa").
    """
    if job_manager.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail=_job_not_found_error(job_id))

    started = await job_manager.start_job(job_id)
    if not started:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "job_not_held",
                "message": (
                    f"Job '{job_id}' is not in a held state and cannot be started."
                ),
                "details": {"job_id": job_id},
            },
        )
    return JSONResponse(
        status_code=200,
        content={"started": True, "job_id": job_id},
    )


@router.post("/rerun-failed", response_model=SubmitJobsResponse)
async def rerun_failed_jobs(
    job_manager: JobManager = Depends(deps.get_job_manager),
) -> SubmitJobsResponse:
    """Chạy lại tất cả job đã `error` hoặc `stopped` (bulk retry).

    Group theo `payment_method` (mỗi group 1 lần `submit_batch`) để giữ
    ordering + đúng registry handler. Nếu account_line ban đầu không parse
    được (edge case) → thêm vào `skipped`.
    """
    from app.core.payment_flow import JobStatus as _JobStatus

    # Gom theo payment_method
    by_method: dict[str, list[str]] = {}
    for record in job_manager.list_jobs():
        if record.status in (_JobStatus.ERROR, _JobStatus.STOPPED):
            by_method.setdefault(record.job.payment_method, []).append(
                record.job.account_line
            )

    all_created: list[str] = []
    all_skipped: list[dict[str, str]] = []
    for method, lines in by_method.items():
        try:
            # `force_rerun=True`: bulk retry là user chủ động chạy lại →
            # reset record cũ về pending thay vì skip `job_already_exists`.
            result = await job_manager.submit_batch(
                method, lines, force_rerun=True
            )
        except UnknownPaymentMethodError as ex:
            raise HTTPException(
                status_code=400,
                detail={
                    "error_code": "unknown_payment_method",
                    "message": str(ex),
                    "details": {"payment_method": ex.payment_method},
                },
            )
        all_created.extend(result.created_job_ids)
        all_skipped.extend(result.skipped)

    return SubmitJobsResponse(
        created_job_ids=all_created,
        skipped=[
            SkippedLineItem(line=item["line"], reason=item["reason"])
            for item in all_skipped
        ],
    )


@router.delete("/clear", response_model=None)
async def clear_jobs(
    filter: str = "all",
    job_manager: JobManager = Depends(deps.get_job_manager),
) -> JSONResponse:
    """Xóa hàng loạt job khỏi list (hard delete).

    Query param `filter`:
    - `all` (mặc định) — xóa tất cả job (bao gồm pending/running: cancel
      trước rồi xóa).
    - `failed` — chỉ xóa `error` + `stopped`.
    - `completed` — chỉ xóa `qr_ready`.
    - `finished` — xóa mọi trạng thái terminal (`qr_ready`, `error`, `stopped`).

    Fail-fast: filter không hợp lệ → 400 `invalid_filter`.
    """
    from app.core.payment_flow import JobStatus as _JobStatus

    filter_key = (filter or "all").lower()
    allowed = {"all", "failed", "completed", "finished"}
    if filter_key not in allowed:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "invalid_filter",
                "message": f"filter must be one of {sorted(allowed)}",
                "details": {"filter": filter_key},
            },
        )

    predicates: dict[str, set[_JobStatus]] = {
        "failed": {_JobStatus.ERROR, _JobStatus.STOPPED},
        "completed": {_JobStatus.QR_READY},
        "finished": {_JobStatus.QR_READY, _JobStatus.ERROR, _JobStatus.STOPPED},
    }

    target_ids: list[str] = []
    for record in job_manager.list_jobs():
        if filter_key == "all":
            target_ids.append(record.job.job_id)
        elif record.status in predicates[filter_key]:
            target_ids.append(record.job.job_id)

    deleted: list[str] = []
    for job_id in target_ids:
        if await job_manager.delete_job(job_id):
            deleted.append(job_id)

    return JSONResponse(
        status_code=200,
        content={
            "deleted_count": len(deleted),
            "deleted_ids": deleted,
            "filter": filter_key,
        },
    )


@router.post("", response_model=SubmitJobsResponse)
async def submit_jobs(
    body: SubmitJobsRequest,
    job_manager: JobManager = Depends(deps.get_job_manager),
) -> SubmitJobsResponse:
    """Tạo batch job từ danh sách dòng account (Requirement 8.1, 8.2).

    Job_Manager parse mỗi dòng qua `handler.parse_account_line`; dòng hợp
    lệ → tạo Job (giữ đúng thứ tự tạo theo R8.1), dòng lỗi → thêm vào
    `skipped` kèm reason cụ thể (R8.2). Batch rỗng hoặc toàn dòng lỗi vẫn
    là request hợp lệ — response trả `created_job_ids=[]` + danh sách
    `skipped` đầy đủ (R8.2 "kể cả khi số dòng hợp lệ bằng 0").

    Semantic add-only (giống UPI `web/manager.py::UpiJobManager.add_jobs`):
    dòng có email đã tồn tại trong list job hiện tại → skip với reason
    `job_already_active` (job đang chạy) hoặc `job_already_exists` (job
    đã terminal). KHÔNG tự động rerun job cũ — user muốn chạy lại phải
    dùng nút Rerun trên từng row, "Retry failed" bulk, hoặc xóa job cũ.

    Payment method chưa đăng ký handler → `UnknownPaymentMethodError` bị
    catch tại đây và trả 400 với `error_code="unknown_payment_method"` —
    Fail_Fast, không suy diễn ngầm sang handler khác (R13.4).
    """

    try:
        # `force_rerun=False`: submit từ UI textarea = "Tạo job mới cho
        # email chưa tồn tại". Email đã có job (bất kể status) → skip,
        # KHÔNG reset job cũ. Đây là fix cho bug "ấn Tạo job chạy lại
        # cả job đã success".
        # `start`: mặc định True (nút Run trên UI — chạy ngay). Client
        # cũ không kèm field → BaseModel default True → hành vi cũ 100%.
        # Nút "+ Add" gửi start=False → job tạo ở `pending + held=True`.
        result = await job_manager.submit_batch(
            body.payment_method,
            body.lines,
            force_rerun=False,
            start=body.start,
        )
    except UnknownPaymentMethodError as ex:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "unknown_payment_method",
                "message": str(ex),
                "details": {"payment_method": ex.payment_method},
            },
        )

    return SubmitJobsResponse(
        created_job_ids=list(result.created_job_ids),
        skipped=[
            SkippedLineItem(line=item["line"], reason=item["reason"])
            for item in result.skipped
        ],
    )


@router.get("", response_model=list[JobViewCompact])
async def list_jobs(
    job_manager: JobManager = Depends(deps.get_job_manager),
) -> list[JobViewCompact]:
    """Danh sách job compact cho JobList (Requirement 12.3, 12.8).

    Trả về theo đúng thứ tự tạo (`JobManager.list_jobs()` giữ ordering theo
    dict insertion). Chỉ trả field tối thiểu để tránh payload lớn khi có
    nhiều job — log đầy đủ + artifact_path chỉ tải khi user mở
    `JobDetailPanel` qua `GET /api/jobs/{id}`.
    """

    return [
        JobViewCompact(
            job_id=record.job.job_id,
            payment_method=record.job.payment_method,
            account_masked=mask_account_line(record.job.account_line),
            account_line=record.job.account_line,
            status=record.status.value,
            updated_at=record.updated_at,
            payment_link=record.payment_link,
            order=record.order,
            retry_count=record.retry_count,
            started_at=record.started_at,
            finished_at=record.finished_at,
            telegram_notifications=list(record.telegram_notifications),
            held=record.held,
            plan=record.plan,
        )
        for record in job_manager.list_jobs()
    ]


@router.get("/{job_id}", response_model=JobViewDetail)
async def get_job_detail(
    job_id: str,
    job_manager: JobManager = Depends(deps.get_job_manager),
) -> JobViewDetail:
    """Chi tiết 1 job cho JobDetailPanel (Requirement 12.3).

    Trả toàn bộ log (đã redact ở tầng core) + `artifact_path`/`error_code`/
    `error_message` khi có. Frontend LUÔN tải QR qua endpoint riêng
    `GET /api/jobs/{id}/qr.png` (R7.4) — `artifact_path` chỉ dùng cho
    debug/log server-side, KHÔNG dùng để build URL trên UI.

    Job không tồn tại → 404 `job_not_found` (Fail_Fast, không trả detail
    rỗng để phân biệt "chưa có data" với "job không tồn tại" theo R14.2).
    """

    record = job_manager.get_job(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail=_job_not_found_error(job_id))

    return JobViewDetail(
        job_id=record.job.job_id,
        payment_method=record.job.payment_method,
        account_masked=mask_account_line(record.job.account_line),
        account_line=record.job.account_line,
        status=record.status.value,
        updated_at=record.updated_at,
        logs=[_log_entry_from_dict(entry) for entry in record.logs],
        artifact_path=record.artifact_path,
        error_code=record.error_code,
        error_message=record.error_message,
        payment_link=record.payment_link,
        order=record.order,
        retry_count=record.retry_count,
        started_at=record.started_at,
        finished_at=record.finished_at,
        telegram_notifications=list(record.telegram_notifications),
        held=record.held,
        plan=record.plan,
    )


@router.post("/{job_id}/rerun", response_model=SubmitJobsResponse)
async def rerun_job(
    job_id: str,
    job_manager: JobManager = Depends(deps.get_job_manager),
) -> SubmitJobsResponse:
    """Chạy lại 1 job — resubmit account_line + payment_method vào batch mới.

    KHÔNG mutate job cũ — tạo Job MỚI (job_id mới, PENDING) từ cùng
    `account_line` + `payment_method`. Cho phép so sánh 2 lần chạy nếu cần.

    Trả về response giống `submit_batch` — `created_job_ids` chứa 1 phần tử
    (job mới), `skipped` rỗng trừ khi account_line ban đầu không parse được
    (edge case do config parser đổi giữa 2 lần).
    """
    record = job_manager.get_job(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail=_job_not_found_error(job_id))

    try:
        # `force_rerun=True`: nút Rerun trên từng row = user chủ động chạy
        # lại → reset record hiện tại về pending (giữ `job_id` cũ), không
        # skip vì `job_already_exists`.
        result = await job_manager.submit_batch(
            record.job.payment_method,
            [record.job.account_line],
            force_rerun=True,
        )
    except UnknownPaymentMethodError as ex:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "unknown_payment_method",
                "message": str(ex),
                "details": {"payment_method": ex.payment_method},
            },
        )

    return SubmitJobsResponse(
        created_job_ids=list(result.created_job_ids),
        skipped=[
            SkippedLineItem(line=item["line"], reason=item["reason"])
            for item in result.skipped
        ],
    )


@router.post("/{job_id}/check-plan", response_model=None)
async def check_plan(
    job_id: str,
    job_manager: JobManager = Depends(deps.get_job_manager),
) -> JSONResponse:
    """Kiểm tra tài khoản của job đã lên Plus chưa qua session cache.

    Delegate xuống `JobManager.check_plan_status()` — handler tương ứng
    (`IdealFlowHandler.check_plan_status`) dùng session cache để gọi
    ChatGPT `/api/auth/session` và phân loại plan.

    Response body: `{"plan": "plus"|"free"|"unknown", "email"?: str,
    "raw_plan"?: str, "expires_at"?: any, "error"?: str}`.

    HTTP status LUÔN 200 trừ khi job không tồn tại (404). Trường hợp
    không check được (no session cached, transport error) → status 200
    với `plan: "unknown"` + `error` cụ thể — UI hiển thị badge "?" thay vì
    error dialog.
    """
    result = await job_manager.check_plan_status(job_id)
    if result is None:
        return JSONResponse(status_code=404, content=_job_not_found_error(job_id))
    return JSONResponse(status_code=200, content=result)


@router.delete("/{job_id}/remove", response_model=None)
async def delete_job(
    job_id: str,
    job_manager: JobManager = Depends(deps.get_job_manager),
) -> JSONResponse:
    """Xóa hoàn toàn job khỏi danh sách (hard delete, không phục hồi).

    Khác với `DELETE /api/jobs/{id}` (stop-only):
    - Nếu job đang running → signal cancel + xóa khỏi state ngay (UI
      không đợi handler exit). Handler task còn tồn tại trong bg cho tới
      khi tự cancel (best-effort, không leak vì proxy_lease và slot vẫn
      được release trong finally của `_run_handler`).
    - Nếu job pending/terminal → xóa ngay.

    Job không tồn tại → 404 `job_not_found`.
    """
    deleted = await job_manager.delete_job(job_id)
    if not deleted:
        return JSONResponse(status_code=404, content=_job_not_found_error(job_id))
    return JSONResponse(status_code=200, content={"job_id": job_id, "deleted": True})


@router.delete("/{job_id}", response_model=StopJobResponse)
async def stop_job(
    job_id: str,
    job_manager: JobManager = Depends(deps.get_job_manager),
) -> StopJobResponse:
    """Yêu cầu dừng 1 job (Requirement 8.6).

    Semantic của `JobManager.stop` (đọc từ core):
    - `pending` → set `stopped` ngay, xóa khỏi hàng đợi.
    - `running` → set cancellation_token; handler tự thoát trong lần
      check kế tiếp và `_run_handler` release lease/slot + phát SSE khi
      thực sự exit. Response trả về CÓ THỂ vẫn `running` — frontend theo
      dõi tiếp qua SSE để nhận final state trong ≤ 2 giây (R8.8).
    - terminal (qr_ready/error/stopped) → no-op, giữ nguyên trạng thái.

    Job không tồn tại → 404 `job_not_found`. Đây là quyết định fail-fast:
    KHÔNG suy diễn "đã xoá xong" — nếu job_id sai chính tả frontend cần
    biết ngay thay vì báo success ảo (R14.2).
    """

    record = job_manager.get_job(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail=_job_not_found_error(job_id))

    await job_manager.stop(job_id)
    # `record` là cùng object với entry trong `JobManager._jobs` (dataclass
    # mutable), nên `record.status` đã phản ánh state sau khi `stop()` xử
    # lý xong (pending → stopped ngay). Với running job, status vẫn có thể
    # là RUNNING vì handler chưa kịp exit — hợp đồng R8.6.
    return StopJobResponse(job_id=job_id, status=record.status.value)


@router.get("/{job_id}/qr.png", response_model=None)
async def get_job_qr(
    job_id: str,
    job_manager: JobManager = Depends(deps.get_job_manager),
) -> FileResponse | JSONResponse | Response:
    """Trả binary PNG của QR khi job đạt `qr_ready` (Requirement 7.4, 7.5).

    Hợp đồng chặt:
    - Job không tồn tại → 404 JSON `job_not_found` (R7.5 phân biệt rõ với
      trường hợp thành công).
    - Job tồn tại nhưng `status != qr_ready` → 409 JSON `qr_not_ready`
      kèm `status` hiện tại để frontend hiển thị thông báo (R7.5).
    - Job `qr_ready` nhưng `artifact_path` rỗng hoặc file đĩa không tồn
      tại → 409/404 JSON `qr_artifact_missing` (Fail_Fast — trạng thái
      không nhất quán, KHÔNG trả file rỗng hoặc placeholder ngầm định
      theo R7.5).
    - Job `qr_ready` + file tồn tại → `FileResponse` với
      `media_type="image/png"`; KHÔNG base64 trong JSON (R7.4).

    `FileResponse` stream file trực tiếp từ đĩa (không đọc toàn bộ vào bộ
    nhớ), phù hợp cho QR PNG kích thước 10-30KB đến vài trăm KB.
    """

    record = job_manager.get_job(job_id)
    if record is None:
        return JSONResponse(status_code=404, content=_job_not_found_error(job_id))

    if record.status != JobStatus.QR_READY:
        return JSONResponse(
            status_code=409,
            content={
                "error_code": "qr_not_ready",
                "message": (
                    f"Job '{job_id}' has not reached qr_ready "
                    f"(current status: {record.status.value})"
                ),
                "details": {"job_id": job_id, "status": record.status.value},
            },
        )

    artifact_path_str = record.artifact_path
    if not artifact_path_str:
        if record.payment_link:
            if record.job.payment_method == "gcash_direct":
                return JSONResponse(
                    status_code=409,
                    content={
                        "error_code": "gcash_qr_requires_browser_session",
                        "message": (
                            "GCash QR must be generated inside the authenticated "
                            "checkout browser session; use payment_link instead."
                        ),
                        "details": {"job_id": job_id, "status": record.status.value},
                    },
                )
            try:
                png_bytes = _render_dynamic_qr_png(record.payment_link)
            except RuntimeError as exc:
                return JSONResponse(
                    status_code=409,
                    content={
                        "error_code": "qr_render_failed",
                        "message": f"Could not render payment link QR for job '{job_id}'.",
                        "details": {"job_id": job_id, "detail": str(exc)},
                    },
                )
            return Response(
                content=png_bytes,
                media_type="image/png",
                headers={"Cache-Control": "no-store"},
            )

        # Trạng thái không nhất quán: QR_READY nhưng không có artifact_path.
        # Không được xảy ra nếu `IdealFlowHandler` tuân thủ R7.3, nhưng
        # phải fail-safe ở tầng route.
        return JSONResponse(
            status_code=409,
            content={
                "error_code": "qr_artifact_missing",
                "message": (
                    f"Job '{job_id}' is in qr_ready status but has no "
                    "artifact_path — inconsistent state."
                ),
                "details": {"job_id": job_id, "status": record.status.value},
            },
        )

    artifact_path = Path(artifact_path_str)
    if not artifact_path.is_file():
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "qr_artifact_missing",
                "message": (
                    f"QR PNG file for job '{job_id}' does not exist on disk."
                ),
                "details": {
                    "job_id": job_id,
                    "status": record.status.value,
                },
            },
        )

    return FileResponse(
        path=str(artifact_path),
        media_type="image/png",
        filename=f"{job_id}.png",
        # `Cache-Control: no-store` — file QR gắn với 1 job cụ thể, không
        # nên cache ở browser/proxy trung gian (trong trường hợp QR bị
        # regenerate cho job cùng ID — hiếm nhưng không loại trừ).
        headers={"Cache-Control": "no-store"},
    )
