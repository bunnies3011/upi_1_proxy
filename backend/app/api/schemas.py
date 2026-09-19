"""Pydantic request/response models cho tầng API (Requirement 8.1, 11.3).

Định nghĩa toàn bộ shape của request/response cho các route thuộc bảng API
trong design.md → "API layer (api/)":

- Jobs (`/api/jobs*`)
- Settings (`/api/settings*`)
- Session cache (`/api/session-cache*`)
- Error trả về khi validate/domain fail

Dùng Pydantic v2 (`pydantic==2.10.5` — đã có trong pyproject của backend).
Model được thiết kế "dumb" — chỉ định nghĩa shape, KHÔNG validate nghiệp vụ
(whitelist key, type constraint của Settings, v.v. do `SettingsRepository`
đảm nhận theo Requirement 11.3, 11.4). Điều này giữ separation of concerns:

- API layer: parse/serialize (Pydantic)
- Domain layer: validate business rule (SettingsRepository, JobManager, ...)

Helper `mask_account_line(line)` mask dòng account thô để hiển thị an toàn
trên UI (Requirement 12.3 — JobList/JobDetail chỉ hiển thị account đã mask,
KHÔNG lộ password/token/totp).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Jobs — request/response models cho routes_jobs.py
# ---------------------------------------------------------------------------


class SubmitJobsRequest(BaseModel):
    """Body của `POST /api/jobs` (Requirement 8.1).

    - `payment_method` mặc định `"ideal"` (hệ thống hiện chỉ có 1 payment
      method đã đăng ký, giữ field để mở rộng theo Payment_Module_Boundary).
    - `lines` là danh sách dòng account thô (chưa parse) — mỗi phần tử được
      `PaymentFlowHandler.parse_account_line` xử lý và tạo Job cho dòng hợp
      lệ, bỏ qua dòng lỗi (kèm reason trong response).
    """

    payment_method: str = "ideal"
    lines: list[str] = Field(default_factory=list)
    # `start`: feature Add / Run tách bạch — mặc định True (nút Run
    # trong UI, giữ hành vi cũ 100% cho client cũ không kèm field).
    # False khi user bấm "+ Add" — job tạo ở trạng thái `pending +
    # held=True`, chờ user chủ động bấm Start (per-row hoặc bulk).
    start: bool = True


class SkippedLineItem(BaseModel):
    """1 dòng account bị bỏ qua khi submit_batch (Requirement 8.2).

    `line` giữ nguyên chuỗi thô người dùng nhập (đã đi qua redaction ở tầng
    core nếu chứa dữ liệu nhạy cảm) để user đối chiếu; `reason` mô tả lý do
    parse fail (ví dụ `"malformed_account_line"`, `"empty_email"`).
    """

    line: str
    reason: str


class SubmitJobsResponse(BaseModel):
    """Response của `POST /api/jobs` (Requirement 8.1, 8.2).

    `created_job_ids` giữ ĐÚNG thứ tự dòng input hợp lệ (Requirement 8.1) —
    frontend hiển thị theo thứ tự tạo.
    """

    created_job_ids: list[str] = Field(default_factory=list)
    skipped: list[SkippedLineItem] = Field(default_factory=list)


class JobLogEntry(BaseModel):
    """1 log entry của 1 job (Requirement 8.8, 14.2).

    `ts` là epoch seconds (float) — frontend format sang ISO/locale khi hiển
    thị. `extra` chứa metadata bổ sung đã được redact ở tầng core
    (`redact_dict`) trước khi lưu, KHÔNG có giá trị nhạy cảm.
    """

    ts: float
    message: str
    extra: dict[str, Any] = Field(default_factory=dict)


class TelegramNotificationEntry(BaseModel):
    """1 bản ghi khi TelegramNotifier gửi QR PNG tới 1 chat.

    Server append 1 entry sau mỗi lần `send_photo` (thành công hoặc fail
    với `TelegramApiError`). Frontend hiển thị dạng badge (số lần đã gửi)
    + tooltip liệt kê `chat_label` / `chat_id` cho từng lần.

    Fields:
        - `chat_id`: Telegram chat_id (string vì có thể là số âm supergroup
          vượt Number.MAX_SAFE_INTEGER của JS).
        - `chat_label`: Label do user đặt trong `telegram.chat_targets`
          setting (chuỗi rỗng nếu chưa đặt — FE tự fallback hiển thị
          `chat_id`).
        - `sent_at`: Epoch seconds (float) — mốc thời gian gửi.
        - `success`: True khi Telegram API trả OK; False khi có `error`.
        - `error`: `null` khi success; dict `{status_code, error_code,
          description}` khi fail (từ `TelegramApiError` hoặc transport
          error).
    """

    chat_id: str
    chat_label: str = ""
    sent_at: float
    success: bool
    error: dict[str, Any] | None = None


class JobViewCompact(BaseModel):
    """View compact 1 job cho `GET /api/jobs` (list, Requirement 12.3, 12.8).

    Chỉ chứa field tối thiểu để hiển thị 1 hàng trong JobList — log đầy đủ
    và error detail chỉ tải khi user mở JobDetailPanel (`GET /api/jobs/{id}`)
    → giảm payload cho danh sách dài.

    `payment_link` là ngoại lệ: nhỏ (1 URL), có mặt khi và chỉ khi
    `status == "qr_ready"`; expose ngay ở compact view để FE render nút
    "Copy link" trên từng row mà không cần fetch detail.

    `order` là counter tăng đơn điệu, cố định vĩnh viễn cho 1 job kể từ
    lúc tạo. FE sort theo `order ASC` để đảm bảo thứ tự UI không đổi khi
    status thay đổi (running/success/error) — bất kể Map/dict insertion
    order có bị Vue reactivity xáo trộn hay không.
    """

    job_id: str
    payment_method: str
    account_masked: str
    # `account_line` là chuỗi RAW user dán vào textarea input, thường có
    # format `email|password|totp_secret`. Expose ra API để SuccessOutputPanel
    # ở FE có thể copy đủ credential (email + password + 2fa) khi job thành
    # công — không cần lưu Map in-memory ở FE (Map mất khi reload trang).
    #
    # Đây là dữ liệu NHẠY CẢM — hệ thống hoạt động ở local network trust
    # (auth token đã bị disable theo yêu cầu user), nếu tương lai bind
    # public port PHẢI thêm auth layer trước khi expose field này.
    # `account_masked` (email masked) giữ nguyên để backward compat với
    # UI hiển thị trong JobList (không expose password ở list chính, chỉ
    # ở SuccessOutputPanel khi user chủ động copy).
    account_line: str
    status: str
    updated_at: float
    payment_link: str | None = None
    order: int
    # `retry_count`: số lần auto-retry đã dùng (Settings
    # `ideal.auto_retry_blocked_*`). Default 0 để backward compat với
    # test payload cũ không kèm field này.
    retry_count: int = 0
    # Mốc thời gian chạy job THẬT — khác `updated_at` (broadcast SSE gần
    # nhất, có thể tăng liên tục theo log realtime):
    #   - `pending` → chưa từng chạy: cả 2 None. FE hiển thị `—`.
    #   - `running` → started_at set, finished_at None: FE tick 1s tính
    #     `now - started_at`.
    #   - terminal (`qr_ready`/`error`/`stopped`) → cả 2 set: FE đóng
    #     băng `finished_at - started_at`, không đếm nữa.
    started_at: float | None = None
    finished_at: float | None = None
    # `telegram_notifications`: history đã gửi Telegram cho job này. Rỗng
    # khi Telegram disabled hoặc job chưa QR_READY. FE hiển thị badge
    # count + tooltip label trong JobList item.
    telegram_notifications: list[TelegramNotificationEntry] = Field(
        default_factory=list
    )
    # `held`: cờ orthogonal với `status="pending"` — feature Add / Run
    # tách bạch. True = job đã tạo qua "+ Add" nhưng chưa Start (scheduler
    # bỏ qua). Default False cho backward compat với payload cũ không
    # kèm field. FE render badge "Held" + nút Start ▶ khi held=True.
    held: bool = False
    # `plan`: kết quả check-plan cuối cùng đã persist (yêu cầu 2026-07 —
    # Successful accounts luôn hiện qua reload). Giá trị:
    #   - `None` → chưa từng check hoặc job vừa rerun (state machine
    #     reset). FE hiển thị badge "?" và cho phép check.
    #   - `"plus"` → tài khoản Plus. SuccessOutputPanel lọc theo giá trị
    #     này (thay vì in-memory `planStates` cũ). "Check Plus All"
    #     SKIP row có `plan="plus"` để không call ChatGPT lại.
    #   - `"free"` → tài khoản Free. FE hiển thị badge FREE; "Check
    #     Plus All" VẪN re-check (free có thể lên plus).
    # Default `None` cho backward compat với BE cũ trước migration
    # không kèm field.
    plan: str | None = None


class JobViewDetail(BaseModel):
    """View chi tiết 1 job cho `GET /api/jobs/{job_id}` (Requirement 12.3).

    Bao gồm toàn bộ log entries và trường lỗi/artifact nếu có — dùng cho
    JobDetailPanel. `artifact_path` là đường dẫn file QR PNG server-side (chỉ
    log/debug); frontend LUÔN tải QR qua `GET /api/jobs/{id}/qr.png`, KHÔNG
    dùng path trực tiếp (Requirement 7.4).
    """

    job_id: str
    payment_method: str
    account_masked: str
    # Raw credential — xem docstring `JobViewCompact.account_line`.
    account_line: str
    status: str
    updated_at: float
    logs: list[JobLogEntry] = Field(default_factory=list)
    artifact_path: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    payment_link: str | None = None
    order: int
    retry_count: int = 0
    # Xem docstring của `JobViewCompact.started_at` cho semantics đầy đủ.
    started_at: float | None = None
    finished_at: float | None = None
    # `telegram_notifications`: xem `JobViewCompact.telegram_notifications`.
    # Expose ở cả 2 view (compact + detail) để tránh trường hợp FE list
    # có badge nhưng khi mở detail lại rỗng (race SSE `job_notified` giữa
    # 2 view).
    telegram_notifications: list[TelegramNotificationEntry] = Field(
        default_factory=list
    )
    # `held`: xem docstring `JobViewCompact.held`.
    held: bool = False
    # `plan`: xem docstring `JobViewCompact.plan`.
    plan: str | None = None


class StopJobResponse(BaseModel):
    """Response của `DELETE /api/jobs/{job_id}` (Requirement 8.6).

    `status` là `Job_Status` cuối cùng SAU khi request stop đã được xử lý:
    - Job pending → `"stopped"` ngay.
    - Job running → `"stopped"` (nếu handler thoát ngay) hoặc trạng thái
      hiện tại nếu handler chưa kịp thoát; frontend theo dõi tiếp qua SSE.
    - Job đã terminal → giữ nguyên trạng thái đó (no-op).
    """

    job_id: str
    status: str


# ---------------------------------------------------------------------------
# Settings — request/response models cho routes_settings.py
# ---------------------------------------------------------------------------


class SettingUpdateRequest(BaseModel):
    """Body của `PUT /api/settings/{key}` (Requirement 11.3, 11.4).

    `value` cố ý dùng `Any` — API layer KHÔNG validate whitelist/type
    constraint tại đây; trách nhiệm này thuộc `SettingsRepository.set()` +
    `_validate_type_constraint()` (Fail_Fast raise `SettingsValidationError`
    khi vi phạm, tầng route bắt và trả `400` với `ErrorResponse`).
    """

    value: Any


class SettingsBulkRequest(BaseModel):
    """Body của `POST /api/settings/bulk` (Requirement 11.2).

    Ghi nhiều key trong 1 transaction — atomic (SettingsRepository.bulk_set).
    Nếu bất kỳ key nào vi phạm whitelist/type constraint, TOÀN BỘ batch bị
    rollback (Fail_Fast, không partial-write).
    """

    items: dict[str, Any] = Field(default_factory=dict)


class SettingsResponse(BaseModel):
    """Response của `GET /api/settings` (Requirement 11).

    `settings` chứa mọi key hiện có trong Settings Store (hoặc bị lọc theo
    query param `prefix=` ở tầng route). Giá trị đã được decode JSON — dùng
    `Any` vì mỗi key có type khác nhau (int, str, bool, list, dict).
    """

    settings: dict[str, Any] = Field(default_factory=dict)


class SettingUpdateResponse(BaseModel):
    """Response của `PUT /api/settings/{key}` (Requirement 11.3).

    Trả lại giá trị ĐÃ ĐƯỢC PERSIST (không phải giá trị request thô) — cho
    phép frontend cập nhật store optimistic sau khi API xác nhận thành công
    (Requirement 12.7).
    """

    key: str
    value: Any


class SetTelegramModeRequest(BaseModel):
    """Body của `POST /api/settings/telegram/mode` (Requirement 3.1, 3.2).

    Đường DUY NHẤT hợp lệ qua API để đổi `telegram.mode` — route gọi
    `JobManager.set_operating_mode(mode, force=force)` (Mode_Switch_Guard)
    THAY CHO `settings_repo.set("telegram.mode", ...)` trực tiếp, vì key
    này cần kiểm tra job Pull_Mode đang dở dang trước khi cho phép đổi.

    Fields:
        mode: Giá trị mode mới, mong đợi `"push"` hoặc `"pull"` — validate
            thực tế do `JobManager.set_operating_mode()` đảm nhiệm (raise
            `SettingsValidationError` nếu sai), route chỉ ánh xạ lỗi.
        force: `True` để tự động kết luận THẤT BẠI cho các job Pull_Mode
            đang `assigned`/`pending` rồi mới đổi mode (Requirement 3.2).
            `False` (default) và còn job dở dang -> bị chặn với 409.
    """

    mode: str
    force: bool = False


class SetTelegramModeResponse(BaseModel):
    """Response của `POST /api/settings/telegram/mode` (Requirement 3.3).

    `mode` là giá trị ĐÃ ĐƯỢC PERSIST, đọc lại từ `SettingsRepository` sau
    khi `set_operating_mode()` hoàn tất — cùng convention với
    `SettingUpdateResponse` (Requirement 12.7).
    """

    mode: str


# ---------------------------------------------------------------------------
# Proxy — request/response models cho routes_proxy.py
# ---------------------------------------------------------------------------


class ProxyProbeBatchRequest(BaseModel):
    """Body của `POST /api/proxy/probe-batch`.

    Probe danh sách proxy do FE cung cấp (thường là draft đang gõ trong
    modal cấu hình) — KHÔNG đụng `ProxyPool` state (không mark_dead / không
    mark_alive), chỉ trả kết quả read-only cho user quyết định lưu hay
    không.

    Fields:
        proxies: Danh sách RAW line (dạng `host:port[:user[:pass]]`,
            `scheme://user:pass@host:port`, hoặc có template `{SID}`).
            Được deduplicate + strip whitespace trước khi probe. Rỗng →
            trả `results=[]`.
        endpoint: URL target probe (default `https://api64.ipify.org` nếu
            `None`). Match `ProbeConfig.endpoint` để FE có thể override
            khi test cùng endpoint đang draft.
        timeout_seconds: Timeout per-probe (giây). `None` → dùng default
            `ProbeConfig.timeout_seconds` (6s). Clamp 3..30.
        concurrency: Số probe song song. `None` → dùng default 5. Clamp
            1..20 để bảo vệ backend (probe song song quá cao có thể DoS
            provider probe endpoint).
    """

    proxies: list[str] = Field(default_factory=list)
    endpoint: str | None = None
    timeout_seconds: int | None = None
    concurrency: int | None = None


class ProxyProbeItemResult(BaseModel):
    """Kết quả probe 1 proxy — 1 phần tử trong response batch.

    Fields:
        proxy: RAW line user đã submit (giữ nguyên để FE map ngược về row
            trong UI). Có thể chứa credential + `{SID}` — FE tự quyết định
            hiển thị `proxy_masked` cho UI.
        proxy_masked: `mask_proxy(materialized_url)` — an toàn cho log/UI.
            Ví dụ: `http://user:pass@1.2.3.4:8080` → `http://***@1.2.3.4:8080`.
            Nếu format rác (không materialize được), trả string `"***"`.
        ok: True khi probe thành công (HTTP 2xx từ endpoint).
        reason: Enum `"ok"` / `"auth"` / `"ip"` / `"format"`.
            - `ok`: probe live.
            - `auth`: 407 proxy-auth hoặc DNS fail (line hỏng, không rotate SID cứu được).
            - `ip`: timeout / connection reset / tunnel fail.
            - `format`: line không parse được (Value_Error từ `materialize_proxy`).
        latency_ms: Thời gian probe (ms) — đo cả cho fail case để user
            phân biệt "timeout" vs "reject nhanh".
        error: Message lỗi ngắn (sanitize credential) khi `ok=False`.
            `None` khi `ok=True`.
    """

    proxy: str
    proxy_masked: str
    ok: bool
    reason: str
    latency_ms: int
    error: str | None = None


class ProxyProbeBatchResponse(BaseModel):
    """Response của `POST /api/proxy/probe-batch`.

    Fields:
        results: Kết quả từng proxy, theo THỨ TỰ input (không sort theo
            latency) để FE map ngược về row index dễ.
        total: Số proxy đã probe (sau dedupe + strip empty).
        live: Số proxy `ok=True`.
        elapsed_ms: Tổng thời gian batch (ms) — không phải sum latency mà
            là wall-clock end-to-end (probe chạy song song).
    """

    results: list[ProxyProbeItemResult] = Field(default_factory=list)
    total: int
    live: int
    elapsed_ms: int


# ---------------------------------------------------------------------------
# Session cache — response models cho routes_session_cache.py
# ---------------------------------------------------------------------------


class ClearSessionCacheResponse(BaseModel):
    """Response của `DELETE /api/session-cache/{account_key}` (Requirement 10.8).

    `cleared` luôn `True` khi endpoint trả 200 — bao gồm cả trường hợp
    account_key không có bản ghi (idempotent). Lỗi I/O tại tầng
    AccountSessionCache đã được log warning, không raise ra ngoài (ngoại lệ
    duy nhất của Fail_Fast, R10.7).
    """

    account_key: str
    cleared: bool


class ClearAllSessionCacheResponse(BaseModel):
    """Response của `DELETE /api/session-cache` (Requirement 10.9)."""

    cleared_all: bool


# ---------------------------------------------------------------------------
# Error response — dùng chung cho mọi route khi Fail_Fast
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    """Shape chuẩn cho response lỗi (Requirement 11.4, 14.2).

    `error_code` là mã ổn định (ví dụ `"settings_key_not_whitelisted"`,
    `"proxy_exhausted"`, `"job_not_qr_ready"`) để frontend/CI dựa vào; tách
    khỏi `message` là chuỗi mô tả dành cho người vận hành, có thể i18n.
    `details` chứa metadata bổ sung tuỳ endpoint, đã redact dữ liệu nhạy
    cảm.
    """

    model_config = ConfigDict(extra="forbid")

    error_code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def mask_account_line(line: str) -> str:
    """Mask dòng account thô để hiển thị an toàn cho UI (Requirement 12.3).

    Định dạng dòng account thô (do `payments/ideal/chatgpt_client.parse_account_line`
    quy định):

    - `email|password|totp_secret` (totp_secret có thể rỗng)
    - `email|access_token`

    Nhiệm vụ của hàm này là:

    1. Lấy phần đầu (email) — bỏ toàn bộ password/token/totp phía sau dấu `|`.
    2. Mask username của email để không lộ toàn bộ định danh (giữ ký tự đầu
       + `***` + domain).

    Ví dụ:

    - `"user@example.com|hunter2"` → `"u***@example.com"`
    - `"jsmith@corp.io|token_xxx"` → `"j***@corp.io"`
    - `"a@b.com|p|t"` → `"a***@b.com"`

    Edge cases (không lộ dữ liệu, không raise):

    - Dòng rỗng/whitespace → `"***"`
    - Không chứa `@` → `"***"` (không thể xác định định dạng email → mask
      toàn bộ để an toàn)
    - Username rỗng (`"@domain.com|..."`) → `"***@domain.com"`
    - Username 1 ký tự (`"a@domain.com|..."`) → `"a***@domain.com"` (vẫn giữ
      pattern nhất quán; chỉ 1 ký tự đầu vốn đã ít định danh)

    Hàm KHÔNG raise trong bất kỳ trường hợp nào — API layer / SSE broadcaster
    có thể gọi mà không cần try/except (Requirement 14.8 —
    Sensitive_Data_Redaction luôn thành công, không phá luồng log).
    """
    stripped = line.strip()
    if not stripped:
        return "***"

    # Bước 1: cắt bỏ mọi thứ sau dấu `|` đầu tiên (password/token/totp).
    email_part = stripped.split("|", 1)[0].strip()
    if not email_part:
        return "***"

    # Bước 2: tách username/domain qua `@`. Nếu không có `@` → mask toàn bộ.
    if "@" not in email_part:
        return "***"

    username, _, domain = email_part.partition("@")
    if not username:
        return f"***@{domain}"
    return f"{username[0]}***@{domain}"
