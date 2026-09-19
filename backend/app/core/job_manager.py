"""Job_Manager — hàng đợi Job generic + concurrency control (Requirement 8, 13).

Thuộc `core/` — KHÔNG import bất kỳ gì từ `app.payments.*`
(Payment_Module_Boundary R13.2, R13.5). JobManager chỉ tương tác với payment
logic qua interface `PaymentFlowHandler` (Protocol) và tra registry theo
`Job.payment_method`.

Concurrency: **per payment method** — mỗi method có limiter riêng
(`configured_max` / `in_use` / `over_budget_slots`). Settings key của
method A chỉ rebuild limiter A; method B không bị last-write-wins.
Scheduler chọn job pending FIFO sớm nhất mà method còn slot (saturated
method không head-of-line block method khác). Job đang `running` KHÔNG
bị giết khi limit giảm — `over_budget_slots` per-method nhớ slot vượt
ngân sách; khi job vượt ngân sách kết thúc, giảm bộ đếm thay vì free
capacity mới.

Fail_Fast (R14.2): mọi exception cho 1 job cụ thể được catch tại boundary
duy nhất `_run_handler` → set job → ERROR + log rõ nguyên nhân, KHÔNG raise
ra ngoài scheduler loop. Các job khác tiếp tục chạy độc lập.

Sensitive_Data_Redaction (R14.8): mọi `extra` truyền vào log entry / SSE
event đều đi qua `redact_dict()` trước khi lưu vào buffer log và phát qua
SSE (SseBroadcaster cũng tự redact 1 lần nữa — defensive double-check,
không thay thế trách nhiệm của caller).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

#: Số dòng log tối đa giữ trong `_JobRecord.logs` cho MỖI job.
#:
#: Trước đây `logs` là list không giới hạn — 1 job chạy flow dài (nhiều
#: retry Stripe / poll refresh_state / login attempt) tích luỹ hàng trăm
#: entry; nhân với số job không dọn còn lại trong `_jobs` dict → RAM tăng
#: đều theo thời gian sử dụng. Cap ở 500 giữ đủ chi tiết để debug 1 flow
#: iDEAL đầy đủ (12 bước + retry) mà không phình vô hạn. Log cũ nhất bị
#: pop tự động khi vượt cap (`collections.deque(maxlen=...)`).
_MAX_LOG_ENTRIES_PER_JOB: int = 500

#: TTL cho job ở trạng thái terminal (qr_ready/error/stopped).
#:
#: Job terminal cũ hơn ngưỡng này bị `_cleanup_loop` xoá khỏi `_jobs`
#: dict + DB. Job pending/running KHÔNG BAO GIỜ bị dọn tự động (chỉ user
#: mới quyết định stop/delete). Chọn 24 giờ vì:
#: - Đủ dài để 1 phiên làm việc thường (submit sáng, review chiều) không
#:   mất job đã hoàn tất trên UI.
#: - Đủ ngắn để `GET /api/jobs` không phình lên hàng nghìn row sau vài
#:   ngày chạy liên tục.
_JOB_RETENTION_SECONDS: float = 24 * 3600

#: Trần cứng số job terminal giữ đồng thời trong `_jobs` dict.
#:
#: Nếu số job terminal vượt trần, xoá các job cũ nhất (theo `updated_at`)
#: tới khi bằng trần — bất kể tuổi. Bảo vệ trước edge case "1 ngày submit
#: 20k account thành công" khiến TTL 24h không đủ nhanh để giải phóng RAM.
_JOB_RETENTION_MAX_COUNT: int = 10_000

#: Chu kỳ chạy vòng dọn dẹp job terminal cũ (giây).
#:
#: Không cần chạy quá thường xuyên: cleanup chỉ scan `_jobs.values()`
#: một lần và làm việc bulk. 5 phút là compromise giữa "kịp giải phóng
#: RAM" và "không tốn CPU vô cớ khi rảnh".
_JOB_CLEANUP_INTERVAL_SECONDS: float = 300.0


def _new_logs_deque() -> "deque[dict[str, Any]]":
    """Factory tạo deque có maxlen cho `_JobRecord.logs`.

    Dataclass `field(default_factory=...)` cần callable trả instance mới —
    không thể inline `deque(maxlen=N)` vì sẽ share cùng 1 object giữa các
    record (bug shared-mutable-default). Factory riêng đảm bảo mỗi record
    có deque độc lập với maxlen = `_MAX_LOG_ENTRIES_PER_JOB`.
    """
    return deque(maxlen=_MAX_LOG_ENTRIES_PER_JOB)

from typing import Awaitable, Callable

from app.core.errors import (
    CoreError,
    HandlerAlreadyRegisteredError,
    JobAlreadyResolvedError,
    JobNotFoundError,
    ModeSwitchBlockedError,
    ProxyExhaustedError,
    SettingsValidationError,
)
from app.core.proxy_health import acquire_live_proxy, is_network_error
from app.core.payment_flow import (
    AccountLineError,
    Job,
    JobResult,
    JobStatus,
    ParsedAccount,
    PaymentFlowHandler,
    ProxyLeaseHealth,
    SimpleCancellationToken,
)
from app.core.redaction import redact_dict

if TYPE_CHECKING:
    from app.core.job_repo import BatchTallyRow, JobRepository, JobRow
    from app.core.proxy_pool import ProxyLease, ProxyPool
    from app.core.settings_store import SettingsRepository
    from app.core.sse import SseBroadcaster


# Signature callback đăng ký qua `register_terminal_hook`. Nhận `_JobRecord`
# đã được set state QR_READY + artifact_path/payment_link đầy đủ. Trả
# coroutine để JobManager `await` — hook được phép làm I/O async (VD gọi
# Telegram Bot API), tuy nhiên nên tự spawn task nền nếu tác vụ có thể lâu
# để không block scheduler nhận job tiếp theo.
JobTerminalHook = Callable[["_JobRecord"], Awaitable[None]]

# Signature callback đăng ký qua `register_job_removed_hook`. Nhận
# `job_id` (str) của job VỪA bị xoá vĩnh viễn khỏi `_jobs` (qua
# `delete_job()` hoặc `_cleanup_old_terminal_jobs()`). Sync (KHÔNG
# Awaitable) — hook chỉ nên làm dọn dẹp in-memory nhẹ (VD
# `dict.pop(job_id, None)` của 1 cache external), KHÔNG I/O. Dùng để các
# module external giữ cache keyed theo `job_id` (VD
# `DeviceProfileAllocator._cache`) có cơ hội giải phóng entry đồng bộ với
# vòng đời job ở `JobManager`, tránh memory leak khi cache đó sống suốt
# đời process mà không tự bounded.
JobRemovedHook = Callable[[str], None]

# Signature callback đăng ký qua `register_pull_error_hook`. Nhận
# `_JobRecord` VỪA được `resolve_pull_error` chốt `pull_outcome = FAIL`
# (job Pull_Mode có Job_Pull_Assignment chuyển ERROR) cùng `error_code`
# (có thể `None`). Dùng để `PullJobNotifier`
# (notifiers/telegram/pull_mode.py) gửi message báo lỗi kỹ thuật kèm nút
# "Nhận job tiếp" vào `pull_origin_chat_id` (R13.3) mà KHÔNG cần
# `JobManager` import `notifiers.telegram.*` (Payment_Module_Boundary R20).
# Best-effort: exception trong hook được swallow tại boundary
# `_invoke_pull_error_hooks` (log rồi tiếp tục), KHÔNG rollback
# `pull_outcome` đã ghi (R13.6).
PullErrorHook = Callable[["_JobRecord", "str | None"], Awaitable[None]]

_NON_RERUNNABLE_ERROR_CODES = frozenset({"no_free_offer"})
_DEACTIVATED_LOGIN_ERROR_MARKERS = (
    "account_deleted_or_deactivated",
    "deleted or deactivated",
    "has been deleted",
    "deactivated",
    "do not have an account",
)


def _non_rerunnable_error_reason(
    error_code: str | None,
    error_message: str | None,
) -> str | None:
    """Return skip reason for terminal errors that should never be retried."""

    if error_code in _NON_RERUNNABLE_ERROR_CODES:
        return error_code
    if error_code == "login_failed":
        lowered = (error_message or "").lower()
        if any(marker in lowered for marker in _DEACTIVATED_LOGIN_ERROR_MARKERS):
            return "account_deactivated"
    return None

# Signature callback đăng ký qua `register_mode_switch_notify_hook`. Nhận
# `_JobRecord` VỪA bị `set_operating_mode` force-fail (`pull_outcome`
# chuyển FAIL do đổi Operating_Mode giữa lúc job đang assigned/pending,
# R3.2). Dùng để `pull_mode.py` (PullJobNotifier) gửi
# `editMessageReplyMarkup(reply_markup=None)` (gỡ nút QR nếu còn) + 1
# `sendMessage` báo `pull_origin_chat_id` job đã bị hủy do đổi mode vận
# hành (R3.2 bước d, e) — giữ `JobManager` KHÔNG import
# `notifiers.telegram.*` (Payment_Module_Boundary R20). Best-effort:
# exception trong hook được swallow tại boundary
# `_invoke_mode_switch_notify_hooks` (log rồi tiếp tục), KHÔNG rollback
# `pull_outcome` đã chốt VÀ KHÔNG chặn việc set mode mới ở bước cuối
# `set_operating_mode` (R3.6).
ModeSwitchNotifyHook = Callable[["_JobRecord"], Awaitable[None]]


class UnknownPaymentMethodError(CoreError):
    """Raise khi `JobManager.submit_batch` gặp `payment_method` chưa được đăng ký
    handler tương ứng (Requirement 13.4 — payment module tự đăng ký, không có
    fallback ngầm)."""

    def __init__(self, payment_method: str) -> None:
        self.payment_method = payment_method
        message = (
            f"Payment method '{payment_method}' chưa có handler đăng ký. "
            "Payment module PHẢI gọi JobManager.register_handler() trước khi "
            "submit_batch."
        )
        super().__init__(message)


class HandlerContractError(CoreError):
    """Raise khi handler truyền vào `register_handler()` thiếu 1 trong các
    method bắt buộc của `PaymentFlowHandler` (`run`, `parse_account_line`,
    `get_max_concurrent_key`).

    Do `PaymentFlowHandler` là `typing.Protocol` (structural, không
    `runtime_checkable` mặc định), duck-type check tại thời điểm register là
    lớp bảo vệ Fail_Fast — phát hiện ngay tại register thay vì lỗi runtime
    sâu bên trong scheduler loop.
    """

    def __init__(self, payment_method: str, missing_attr: str) -> None:
        self.payment_method = payment_method
        self.missing_attr = missing_attr
        message = (
            f"Handler cho payment method '{payment_method}' thiếu method bắt "
            f"buộc '{missing_attr}' của interface PaymentFlowHandler."
        )
        super().__init__(message)


class PullOutcome(str, Enum):
    """Kết quả tính công của 1 job Pull_Mode (design.md §3.1, R20.5, R20.6,
    R22.4, R22.7). `PENDING` là trạng thái ban đầu khi job vừa được
    `claim_pull_account`; `SUCCESS`/`FAIL` là terminal (absorbing) — không
    có transition nào đổi ngược `SUCCESS`/`FAIL` về giá trị khác, chỉ được
    ghi đúng 1 lần duy nhất qua `resolve_pull_outcome`."""

    PENDING = "pending"
    SUCCESS = "success"
    FAIL = "fail"


class PullAssignmentState(str, Enum):
    """Trạng thái gán account cho worker của 1 job Pull_Mode (design.md
    §3.1). `_JobRecord.pull_assignment_state == None` nghĩa là job thuộc
    Push_Mode (không có Job_Pull_Assignment) — khác `UNASSIGNED` (job Pull_Mode
    còn nằm trong Pull_Account_Pool, chưa được worker nào nhận)."""

    UNASSIGNED = "unassigned"
    ASSIGNED = "assigned"


class PlusCheckState(str, Enum):
    """State machine verify 2 lần cho QR Pull_Mode (design.md §3.1 — tái
    dùng tên từ spec `telegram-callback-verify-plus`, implement lần đầu ở
    module này vì Pull_Verify_State cần state machine này hoạt động đúng)."""

    ARMED = "armed"
    CHECKING = "checking"
    VERIFIED = "verified"
    EXHAUSTED_2 = "exhausted_2"
    FAILED_MARKED = "failed_marked"


@dataclass(frozen=True)
class BatchSubmitResult:
    """Kết quả của `JobManager.submit_batch` (Requirement 8.1, 8.2).

    Attributes:
        created_job_ids: `job_id` (theo đúng thứ tự tạo = thứ tự dòng trong
            input) của các dòng account hợp lệ đã tạo Job thành công.
        skipped: Danh sách dòng bị bỏ qua kèm lý do; mỗi phần tử là dict
            `{"line": <raw_line>, "reason": <reason>}` — API layer trả về
            nguyên vẹn cho Frontend_App (Requirement 8.2).
    """

    created_job_ids: list[str]
    skipped: list[dict[str, str]]


@dataclass
class _JobRecord:
    """Trạng thái nội bộ 1 Job trong JobManager (KHÔNG public API).

    Task 23.1 (route layer `api/routes_jobs.py`) sẽ đọc trực tiếp qua
    `get_job()` / `list_jobs()` — giữ dataclass mutable để cập nhật
    `status`/`updated_at` in-place mà không phải tạo record mới.

    `artifact_path`/`error_code`/`error_message` được `_run_handler` copy từ
    `JobResult` khi handler thoát (thành công hoặc lỗi) hoặc từ error path
    tại boundary duy nhất (proxy_exhausted / internal_error). Đây là nguồn
    duy nhất cho API layer trả:
    - `GET /api/jobs/{id}/qr.png` (task 23.1) đọc `artifact_path` để trả
      binary PNG (R7.4).
    - `GET /api/jobs/{id}` trả `error_code`/`error_message` khi job terminal
      ở trạng thái ERROR (R12.3).
    KHÔNG có source-of-truth thay thế: `JobResult` chỉ tồn tại trong stack
    frame của `handler.run()`; log entries KHÔNG chứa artifact_path (broadcast
    SSE chỉ đẩy đi client, không lưu buffer trên record).

    `dedup_key` (optional): định danh duy nhất cho account trong 1 payment
    method (VD email lowercase với iDEAL). Handler tuỳ chọn implement
    `get_account_dedup_key(parsed)` — nếu có, JobManager dùng key này để
    thực thi "1 account = 1 job" (rerun trên job đã có thay vì tạo job mới).
    """

    job: Job
    status: JobStatus
    # `logs`: bounded deque (maxlen = `_MAX_LOG_ENTRIES_PER_JOB`) thay vì
    # list vô giới hạn — log cũ nhất bị pop tự động khi vượt cap, tránh
    # RAM tăng đều theo thời gian sống của job. API layer iterate deque
    # bình thường (`for entry in record.logs`) — deque là iterable.
    logs: "deque[dict[str, Any]]" = field(default_factory=_new_logs_deque)
    lease: "ProxyLease | None" = None
    settings_snapshot: dict[str, Any] = field(default_factory=dict)
    updated_at: float = 0.0
    handler_task: asyncio.Task | None = None
    artifact_path: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    # `payment_link` copy từ `JobResult.payment_link` khi handler đạt QR_READY —
    # URL user mở khi quét QR (VD `https://pay.ideal.nl/transactions/...`).
    # Expose qua API `/api/jobs` và `/api/jobs/{id}` để UI cung cấp nút
    # "Copy link" cho từng row job không cần download PNG rồi decode QR.
    # KHÔNG chứa dữ liệu nhạy cảm (URL công khai iDEAL), an toàn broadcast SSE.
    payment_link: str | None = None
    qr_expires_at: float | None = None
    dedup_key: str | None = None
    # `retry_count`: số lần auto-retry đã dùng cho job này (Requirement
    # auto-retry blocked). Reset về 0 khi user submit thủ công lại (dedup
    # reuse do batch input). Tăng 1 mỗi lần `_maybe_auto_retry` schedule.
    # Nhớ qua restart qua cột `jobs.retry_count`. Xem UPI
    # `web/manager.py::_maybe_auto_retry` để tham chiếu ngữ nghĩa.
    retry_count: int = 0
    # `order`: counter tăng đơn điệu duy nhất, cố định VĨNH VIỄN cho 1 job
    # kể từ khi tạo lần đầu. Dedup reuse (retry_failed / rerun / submit
    # trùng account) KHÔNG đổi order. delete_job → job biến mất, order
    # cũ không được tái sử dụng.
    #
    # Mục đích: cung cấp key sort cứng cho frontend để UI hiển thị job
    # theo đúng thứ tự tạo, KHÔNG phụ thuộc dict/Map insertion order (dù
    # spec ECMAScript/Python 3.7+ preserve insertion, Vue reactive wrap
    # trên `ref(new Map())` có thể có edge case khi có nhiều mutation
    # song song từ SSE — dẫn tới user thấy "job nhảy vị trí trong lúc
    # chạy"). Sort by `order` là guarantee tuyệt đối.
    order: int = 0
    # Mốc thời gian chạy job THẬT — khác `updated_at` (broadcast SSE gần
    # nhất, có thể tăng liên tục theo log realtime).
    #
    #   - `started_at`: set khi job chuyển RUNNING lần đầu trong
    #     `_run_handler`. Reset về None khi dedup reuse (job cũ terminal,
    #     submit lại → chạy từ đầu). None khi job chưa từng RUNNING.
    #   - `finished_at`: set khi job chuyển terminal (qr_ready / error /
    #     stopped). Reset về None khi dedup reuse.
    #
    # Frontend dùng để hiển thị elapsed đúng:
    #   - pending → chưa chạy: `—`.
    #   - running → tick 1s `now - started_at`.
    #   - terminal → `finished_at - started_at` ĐÓNG BĂNG, không đếm nữa.
    started_at: float | None = None
    finished_at: float | None = None
    # `telegram_notifications`: bản ghi mỗi lần `TelegramNotifier` đã gửi
    # QR PNG tới 1 chat. Mỗi entry là dict shape:
    #   {"chat_id": str, "chat_label": str, "sent_at": float,
    #    "success": bool, "error": dict | None}
    # Notifier gọi `JobManager.record_telegram_notification(job_id, entry)`
    # sau khi API Telegram trả kết quả — method này append + persist +
    # broadcast SSE event `job_notified` (không phải `job_status` để FE
    # merge patch mà không trigger status transition side-effect).
    #
    # KHÔNG dedup entry — nếu notifier retry cùng chat, sẽ có nhiều entry.
    # Cho phép user thấy history đầy đủ (bao gồm lần fail nếu có).
    telegram_notifications: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Pull_Mode — Job_Pull_Assignment (design.md §3.2, R21.2, R21 Glossary)
    # ------------------------------------------------------------------
    # Toàn bộ 10 field dưới đây default `None`/`PlusCheckState.ARMED`/`0` để
    # job cũ (nạp từ `JobRow` trước migration, KHÔNG có 6 cột pull_* trong
    # DB) tự động coi là job Push_Mode — backward-compatible (R21.2), KHÔNG
    # cần migrate dữ liệu cũ nào ngoài thêm cột NULL.
    #
    # `pull_assignment_state`: `None` = job Push_Mode (không có
    # Job_Pull_Assignment). `UNASSIGNED` = job Pull_Mode còn trong
    # Pull_Account_Pool, chưa worker nào nhận. `ASSIGNED` = đã gán cho 1
    # worker qua `claim_pull_account`. CHỈ được gán qua method public của
    # JobManager (R20.2) — module Pull_Mode bên ngoài KHÔNG được set trực tiếp.
    pull_assignment_state: "PullAssignmentState | None" = None
    # `pull_assigned_telegram_user_id`: telegram_user_id của worker đã nhận
    # job này qua `claim_pull_account`. `None` nếu Push_Mode hoặc còn unassigned.
    pull_assigned_telegram_user_id: str | None = None
    # `pull_origin_chat_id`: chat_id nơi worker đã bấm Nhận_Job_Button — đích
    # gửi các thông báo tiếp theo (QR, lỗi kỹ thuật, hủy do đổi mode).
    pull_origin_chat_id: str | None = None
    # `pull_assigned_username`/`pull_assigned_first_name`: SNAPSHOT định danh
    # worker lấy từ Telegram TẠI THỜI ĐIỂM Nhận_Job_Button được bấm — KHÔNG
    # truy vấn lại Telegram API sau đó (dùng hiển thị caption QR + ranking).
    pull_assigned_username: str | None = None
    pull_assigned_first_name: str | None = None
    # `pull_outcome`: kết quả tính công — `None` = job Push_Mode. `PENDING`
    # = job Pull_Mode chưa resolve. `SUCCESS`/`FAIL` = terminal (absorbing),
    # CHỈ được ghi đúng 1 lần qua `resolve_pull_outcome` (R20.5, R20.6, R22.4, R22.7).
    pull_outcome: "PullOutcome | None" = None
    # `plus_check_state`/`plus_check_attempts`/`telegram_message_chat_id`/
    # `telegram_message_id`: ĐÃ thuộc scope spec `telegram-callback-verify-plus`,
    # implement CÙNG lúc ở đây vì Pull_Verify_State tái dùng chính xác state
    # machine này. Default `ARMED`/`0` (chưa từng check) áp dụng cho MỌI job
    # (cả Push_Mode và Pull_Mode) — KHÔNG có nghĩa "job đang Pull_Mode".
    plus_check_state: "PlusCheckState" = PlusCheckState.ARMED
    plus_check_attempts: int = 0
    # `telegram_message_chat_id`/`telegram_message_id`: định danh message QR
    # đã gửi (chat_id + message_id) — dùng để `editMessageReplyMarkup`/
    # `editMessageCaption` khi verify 2 lần chuyển state hoặc khi Mode_Switch_Guard
    # gỡ nút do hủy job.
    telegram_message_chat_id: str | None = None
    telegram_message_id: int | None = None
    # `held`: cờ orthogonal với `status`. Feature Add / Run tách bạch —
    # user bấm nút "+ Add" (thay vì "Run") tạo job ở trạng thái pending
    # + held=True → CHƯA vào `_pending_order`, scheduler bỏ qua đến khi
    # user chủ động bấm Start (per-row hoặc bulk "Start N held"). Chỉ
    # có ý nghĩa khi `status=PENDING`; scheduler chuyển sang RUNNING sẽ
    # tự clear `held=False` để restore sau restart không kẹt lại. Default
    # False → hành vi cũ 100% (submit → chạy ngay).
    held: bool = False
    # `plan`: kết quả check-plan cuối cùng của tài khoản này — persist
    # qua restart để reload trang giữ nguyên panel Successful accounts
    # (yêu cầu 2026-07 "Successful accounts luôn hiện, chỉ Clear all mới
    # mất"). Giá trị chỉ nhận:
    #   - `None`  → chưa từng check hoặc job vừa rerun (state machine
    #     reset). Nút "Check Plus All" sẽ check.
    #   - `"plus"` → tài khoản Plus. SuccessOutputPanel lọc theo giá
    #     trị này. "Check Plus All" bỏ qua để không call ChatGPT lại.
    #   - `"free"` → tài khoản Free. FE hiển thị badge FREE ngay sau
    #     reload; "Check Plus All" VẪN re-check (free có thể lên plus).
    # Không persist `"unknown"` (transport error / no_session_cached) —
    # coi như chưa check để lần sau có cơ hội thử lại.
    # `check_plan_status` chịu trách nhiệm ghi giá trị này + persist.
    # `_maybe_reset_plan_on_rerun` (gọi trong `submit_batch` force_rerun
    # + `_run_handler` khi RUNNING lại) reset về None.
    plan: str | None = None


_REQUIRED_HANDLER_ATTRS = ("run", "parse_account_line", "get_max_concurrent_key")


@dataclass
class _MethodLimiter:
    """Limiter concurrency scoped theo 1 payment_method.

    `configured_max`: limit hiện hành (từ settings / update).
    `in_use`: số slot đang giữ (job đã acquire, chưa release) — bao gồm
        cả job "vượt ngân sách" khi limit bị hạ lúc đang chạy.
    `over_budget_slots`: số slot in_use vượt configured_max; khi job kết
        thúc, ưu tiên giảm counter này thay vì free capacity mới.
    """

    configured_max: int = 1
    in_use: int = 0
    over_budget_slots: int = 0

    def has_capacity(self) -> bool:
        budgeted = self.in_use - self.over_budget_slots
        return budgeted < self.configured_max

    def acquire(self) -> None:
        self.in_use += 1

    def release(self) -> None:
        if self.over_budget_slots > 0:
            self.over_budget_slots -= 1
        self.in_use = max(0, self.in_use - 1)


class JobManager:
    """Quản lý hàng đợi Job generic theo PaymentFlowHandler đã đăng ký.

    KHÔNG biết chi tiết payment method — chỉ tra registry theo
    `Job.payment_method`.
    """

    def __init__(
        self,
        settings: "SettingsRepository",
        proxy_pool: "ProxyPool",
        sse: "SseBroadcaster",
        job_repo: "JobRepository | None" = None,
    ) -> None:
        self._settings = settings
        self._proxy_pool = proxy_pool
        self._sse = sse
        # `job_repo` optional để test unit hiện có tạo `JobManager` với 3
        # arg cũ không phải sửa (persist là behavior phụ, không critical
        # cho logic scheduler/dedup/concurrency). Production luôn inject
        # qua `bootstrap_services()` để nhớ job qua restart.
        self._job_repo: "JobRepository | None" = job_repo
        self._handlers: dict[str, PaymentFlowHandler] = {}
        self._jobs: dict[str, _JobRecord] = {}
        # `pending` job_ids theo thứ tự tạo (FIFO). Danh sách chứ không phải
        # `asyncio.Queue` vì cần khả năng remove nhanh khi `stop(job_id)` với
        # job đang pending.
        self._pending_order: list[str] = []
        # Counter tăng đơn điệu cho `_JobRecord.order` — mỗi job mới tạo
        # nhận giá trị hiện tại + 1. Dedup reuse KHÔNG tăng counter.
        # Không reset khi delete_job (tránh trùng order với job đang tồn tại).
        # int Python không giới hạn kích thước → không cần lo tràn số.
        self._order_counter: int = 0

        # Concurrency control — PER payment method (không còn global
        # semaphore last-write-wins). Mỗi entry: configured_max / in_use /
        # over_budget_slots. Default 1 slot khi register_handler; startup
        # `apply_max_concurrent_from_settings` ghi đè theo settings key.
        self._method_limiters: dict[str, _MethodLimiter] = {}
        # Index dedup_key → job_id để lookup O(1) trong submit_batch.
        # Chỉ populate khi handler cung cấp `get_account_dedup_key(parsed)`
        # (duck-typed optional method). Key format `<payment_method>:<key>`
        # để cùng email trong 2 payment method khác nhau vẫn tạo 2 job
        # riêng biệt (Payment_Module_Boundary — mỗi method có namespace riêng).
        self._dedup_index: dict[str, str] = {}

        # Scheduler task + event để đánh thức khi có job mới hoặc slot đổi.
        self._new_job_event: asyncio.Event = asyncio.Event()
        self._scheduler_task: asyncio.Task | None = None

        # Cleanup task chạy nền dọn job terminal cũ theo TTL/trần cứng —
        # xem `_cleanup_loop`. Lazy spawn cùng scheduler qua
        # `_ensure_scheduler_running` để test unit không cần start task
        # nền khi chỉ gọi API sync như `list_jobs`.
        self._cleanup_task: asyncio.Task | None = None

        # Auto-retry: map job_id → asyncio.Task đang sleep chờ requeue lại
        # job. Track để có thể cancel khi user `stop`/`delete_job`/backend
        # shutdown, tránh requeue "zombie" sau khi user chủ động dừng job.
        # Tham chiếu gpt_signup_hybrid/web/manager.py::_delayed_requeue_tasks.
        self._delayed_requeue_tasks: dict[str, asyncio.Task] = {}

        # Terminal hooks — callback được gọi SAU khi 1 job chuyển sang
        # trạng thái QR_READY (job success). Dùng cho các module external
        # ngoài `core/` (VD Telegram notifier) cần react khi có job thành
        # công mà KHÔNG được import ngược vào core (giữ boundary generic —
        # core không biết Telegram tồn tại). Signature:
        #   `async def hook(record: _JobRecord) -> None`
        # Best-effort: exception trong hook được swallow ở boundary (log
        # rồi tiếp tục), KHÔNG được raise ra `_run_handler` và làm hỏng
        # flow chính. Hook nào chậm/timeout sẽ ảnh hưởng tới thời điểm
        # scheduler nhận job tiếp — nên hook phải tự schedule task nền
        # nếu I/O có thể lâu.
        self._terminal_hooks: list["JobTerminalHook"] = []
        # Job-removed hooks — callback SYNC được gọi mỗi khi 1 job bị xoá
        # vĩnh viễn khỏi `_jobs` (delete_job / cleanup TTL). Khác
        # `_terminal_hooks` (chỉ bắt QR_READY, dùng cho notify), hook này
        # bắt MỌI đường xoá job (qr_ready/error/stopped/pending đang xoá
        # dở) — dùng để các cache external keyed theo `job_id` (VD
        # `DeviceProfileAllocator._cache`) dọn theo, tránh memory leak.
        # Best-effort: exception trong hook được swallow (log rồi tiếp
        # tục), KHÔNG raise ra `delete_job`/`_cleanup_old_terminal_jobs`.
        self._job_removed_hooks: list["JobRemovedHook"] = []
        # Pull_Mode error notification hooks — callback được gọi SAU khi
        # `resolve_pull_error` chốt `pull_outcome = FAIL` cho job Pull_Mode
        # có Job_Pull_Assignment chuyển ERROR. Dùng cho
        # `PullJobNotifier.notify_pull_error` gửi message báo lỗi kỹ thuật
        # + nút "Nhận job tiếp" — đăng ký external để `JobManager` KHÔNG
        # phải import `notifiers.telegram.*` (Payment_Module_Boundary R20).
        # Best-effort giống `_terminal_hooks`: exception swallow per-hook,
        # KHÔNG rollback state (R13.6).
        self._pull_error_hooks: list["PullErrorHook"] = []
        # Mode-switch notify hooks — callback được gọi cho MỖI job bị
        # `set_operating_mode(force=True)` force-fail vì đang
        # assigned/pending lúc đổi Operating_Mode (design.md §3.1, §3.4,
        # R3.2 bước d, e). Dùng cho `PullJobCoordinator`
        # (notifiers/telegram/pull_mode.py) đăng ký gửi
        # `editMessageReplyMarkup` (gỡ nút QR) + `sendMessage` (báo worker
        # job đã bị hủy do đổi mode) mà KHÔNG cần `JobManager` import
        # `notifiers.telegram.*` (Payment_Module_Boundary R20). Best-effort
        # giống `_terminal_hooks`/`_pull_error_hooks`: exception per-hook
        # được swallow tại boundary, KHÔNG chặn vòng lặp force-fail cũng
        # KHÔNG chặn bước set mode mới cuối `set_operating_mode` (R3.6).
        self._mode_switch_notify_hooks: list["ModeSwitchNotifyHook"] = []
        # Plus-verified hooks — gọi 1 lần khi `record.plan` chuyển sang
        # `"plus"` (auto-check sau QR hoặc manual Check Plus). Dùng cho
        # TelegramNotifier tag reply + edit caption + tally batch.
        # Best-effort: exception swallow per-hook (giống terminal hooks).
        self._plus_verified_hooks: list[Any] = []
        # Plan-check timeout hooks — auto-poll 5 phút hết mà chưa plus.
        # Signature: async (job_id: str) -> None.
        self._plan_check_timeout_hooks: list[Any] = []
        # Live QR slot release hooks — SYNC, called on stop/rerun/delete
        # (via `_cancel_auto_check_plan`) so live gate frees slots when the
        # plus/timeout poll is cancelled. Hook schedules its own async work.
        self._live_slot_release_hooks: list[Any] = []
        # Cờ set trong `shutdown()` để các task requeue đang sleep tự exit
        # thay vì kick job vào queue khi event loop sắp đóng.
        self._shutting_down: bool = False

        # Lock bảo vệ mutation của `_pending_order`, `_jobs`, và concurrency
        # state khi update_max_concurrent chạy đồng thời với scheduler.
        self._lock: asyncio.Lock = asyncio.Lock()

        # Lock RIÊNG PER-JOB (khác `self._lock` chung) cho các thao tác
        # Pull_Mode cần atomic trên đúng 1 `_JobRecord` mà KHÔNG chặn toàn
        # bộ JobManager (VD `resolve_pull_outcome`, và về sau
        # `record_plus_check_transition` ở task 16) — design.md §3.1,
        # R22.11. Lazy-create qua `_get_pull_job_lock`, dọn qua
        # job-removed hook đăng ký ngay dưới đây để tránh leak dict khi
        # job bị xoá vĩnh viễn.
        self._pull_job_locks: dict[str, asyncio.Lock] = {}
        self.register_job_removed_hook(
            lambda job_id: self._pull_job_locks.pop(job_id, None)
        )

        # Push_Success_Gate hook (feature "success wait" Push_Mode) —
        # callable sync trả `True` khi gate đang PAUSED (đã đủ N success).
        # Khi paused, `_await_next_pending_job_id` skip job Push_Mode
        # (`pull_assignment_state is None`) trong `_pending_order`, chỉ
        # trả về job Pull_Mode nếu có. Nếu chỉ còn job Push → chờ event
        # (gate resume sẽ gọi `wake_scheduler()` để đánh thức). `None`
        # khi chưa wire (test unit không có gate) → không có effect,
        # scheduler hoạt động 100% như trước. Payment_Module_Boundary:
        # JobManager KHÔNG import `notifiers/`, wiring qua callback do
        # `bootstrap.py` inject.
        self._push_pause_checker: "Callable[[], bool] | None" = None

    # ------------------------------------------------------------------
    # Handler registry (Requirement 13.4, 13.5)
    # ------------------------------------------------------------------
    def register_handler(
        self, payment_method: str, handler: PaymentFlowHandler
    ) -> None:
        """Đăng ký `handler` cho `payment_method`. Raise nếu key đã đăng ký.

        Áp dụng duck-type check các method bắt buộc của `PaymentFlowHandler`
        ngay tại thời điểm đăng ký (Fail_Fast) — phát hiện handler thiếu
        method trước khi có job chạy, thay vì để lỗi runtime sâu trong
        scheduler loop.
        """
        if payment_method in self._handlers:
            raise HandlerAlreadyRegisteredError(payment_method)
        for attr in _REQUIRED_HANDLER_ATTRS:
            if not callable(getattr(handler, attr, None)):
                raise HandlerContractError(payment_method, attr)
        self._handlers[payment_method] = handler
        # Limiter mặc định 1 slot cho tới khi settings apply / update.
        if payment_method not in self._method_limiters:
            self._method_limiters[payment_method] = _MethodLimiter()

    def get_handler(self, payment_method: str) -> PaymentFlowHandler | None:
        """Trả handler đã đăng ký cho `payment_method`, `None` nếu chưa có.

        Public để API layer / integration test kiểm tra registry hiện tại
        (ví dụ trả 400 với payment_method không hợp lệ trước khi submit).
        """
        return self._handlers.get(payment_method)

    # ------------------------------------------------------------------
    # Terminal hooks (job success notification, ...)
    # ------------------------------------------------------------------
    def register_terminal_hook(self, hook: "JobTerminalHook") -> None:
        """Đăng ký callback được gọi khi job đạt trạng thái QR_READY.

        Best-effort (Fail_Fast_Policy chỉ áp dụng cho flow chính): mọi
        exception raised bên trong hook được swallow tại boundary
        `_invoke_terminal_hooks` — hook không được phép làm hỏng flow
        job chính (VD Telegram API down phải không ảnh hưởng gì tới
        JobManager tiếp tục xử lý các job khác).

        Idempotent: cho phép đăng ký nhiều hook (chạy tuần tự theo thứ
        tự đăng ký). Nếu cùng 1 callable được đăng ký 2 lần, SẼ được gọi
        2 lần — caller tự chịu trách nhiệm không double-register.

        Args:
            hook: Async callable nhận `_JobRecord` và trả về
                `Awaitable[None]`. Record đã có `status=QR_READY`,
                `artifact_path`, `payment_link`, `finished_at` set đầy đủ.
        """
        self._terminal_hooks.append(hook)

    def register_plus_verified_hook(self, hook: Any) -> None:
        """Đăng ký callback async khi job vừa chuyển `plan` → `"plus"`.

        Hook nhận `job_id: str`. Best-effort: exception swallow per-hook.
        Dùng để Telegram tag QR message + đếm Plus batch cho worker.
        """
        self._plus_verified_hooks.append(hook)

    def register_plan_check_timeout_hook(self, hook: Any) -> None:
        """Đăng ký callback async khi auto-check plan poll hết thời gian
        (default 5 phút) mà job vẫn chưa `plan=plus`.

        Hook nhận `job_id: str`. Best-effort: exception swallow per-hook.
        """
        self._plan_check_timeout_hooks.append(hook)

    def register_live_slot_release_hook(self, hook: Any) -> None:
        """Register SYNC hook(job_id) for live QR slot release on lifecycle end.

        Called from `_cancel_auto_check_plan` (stop/rerun/delete) so slots
        free when the plus/timeout poll is cancelled. Best-effort swallow.
        """
        self._live_slot_release_hooks.append(hook)

    def _invoke_live_slot_release_hooks(self, job_id: str) -> None:
        if not self._live_slot_release_hooks:
            return
        for hook in list(self._live_slot_release_hooks):
            try:
                hook(job_id)
            except Exception:
                import logging as _logging

                _logging.getLogger(__name__).exception(
                    "live-slot-release hook failed: job_id=%s hook=%r",
                    job_id,
                    hook,
                )

    async def _invoke_plus_verified_hooks(self, job_id: str) -> None:
        """Gọi plus-verified hooks — 1 hook fail không chặn hook sau."""
        if not self._plus_verified_hooks:
            return
        for hook in list(self._plus_verified_hooks):
            try:
                await hook(job_id)
            except Exception:
                import logging as _logging

                _logging.getLogger(__name__).exception(
                    "plus-verified hook failed: job_id=%s hook=%r",
                    job_id,
                    hook,
                )

    async def _invoke_plan_check_timeout_hooks(self, job_id: str) -> None:
        """Gọi timeout hooks — 1 hook fail không chặn hook sau."""
        if not self._plan_check_timeout_hooks:
            return
        for hook in list(self._plan_check_timeout_hooks):
            try:
                await hook(job_id)
            except Exception:
                import logging as _logging

                _logging.getLogger(__name__).exception(
                    "plan-check-timeout hook failed: job_id=%s hook=%r",
                    job_id,
                    hook,
                )

    def register_job_removed_hook(self, hook: "JobRemovedHook") -> None:
        """Đăng ký callback SYNC được gọi khi 1 job bị xoá vĩnh viễn khỏi `_jobs`.

        Bắt MỌI đường xoá job: `delete_job()` (user chủ động) và
        `_cleanup_old_terminal_jobs()` (TTL 24h / trần cứng 10k job) —
        khác `register_terminal_hook` chỉ bắt QR_READY. Dùng để module
        external giữ cache in-memory keyed theo `job_id` (VD
        `DeviceProfileAllocator._cache` ở `payments/ideal/`) đồng bộ dọn
        entry, tránh cache đó phình vô hạn theo tổng số job đã từng tồn
        tại trong đời process (memory leak).

        Best-effort: exception raised bên trong hook được swallow tại
        boundary `_invoke_job_removed_hooks` (log rồi tiếp tục) — hook
        lỗi KHÔNG được phép làm hỏng `delete_job`/cleanup loop.

        Sync (KHÔNG async): hook chỉ nên làm dọn dẹp in-memory nhẹ, không
        I/O. Nếu tương lai cần hook async, thêm `register_async_job_removed_hook`
        riêng thay vì đổi signature hook này (tránh phá hook cũ).

        Idempotent: cho phép đăng ký nhiều hook (chạy tuần tự theo thứ tự
        đăng ký). Đăng ký cùng 1 callable 2 lần → gọi 2 lần, caller tự
        chịu trách nhiệm không double-register.
        """
        self._job_removed_hooks.append(hook)

    def _invoke_job_removed_hooks(self, job_id: str) -> None:
        """Gọi lần lượt các job-removed hook cho `job_id` vừa bị xoá.

        Boundary duy nhất swallow exception per-hook — 1 hook fail KHÔNG
        chặn hook sau chạy, KHÔNG raise ra `delete_job`/cleanup loop. Sync
        vì hook là sync (xem `register_job_removed_hook`).

        Nếu list hook rỗng, sớm return để tránh overhead loop.
        """
        if not self._job_removed_hooks:
            return
        for hook in list(self._job_removed_hooks):
            try:
                hook(job_id)
            except Exception:
                import logging as _logging
                _logging.getLogger(__name__).exception(
                    "job-removed hook failed: job_id=%s hook=%r",
                    job_id,
                    hook,
                )

    async def _invoke_terminal_hooks(self, record: _JobRecord) -> None:
        """Gọi lần lượt các terminal hook cho `record` (chỉ khi QR_READY).

        Boundary duy nhất swallow exception per-hook — 1 hook fail KHÔNG
        chặn hook sau chạy, KHÔNG raise ra `_run_handler`. Log exception
        đầy đủ (bao gồm stack trace) để dev debug.

        Nếu list hook rỗng, sớm return để tránh overhead loop.
        """
        if not self._terminal_hooks:
            return
        for hook in list(self._terminal_hooks):
            try:
                await hook(record)
            except Exception:
                import logging as _logging
                _logging.getLogger(__name__).exception(
                    "terminal hook failed: job_id=%s hook=%r",
                    record.job.job_id,
                    hook,
                )

    # Auto Check Plus poll after QR_READY: user pays via QR → entitlement
    # may lag. Poll until plan=plus or wall-clock timeout (5 minutes).
    _AUTO_CHECK_PLAN_TIMEOUT_SECONDS: float = 300.0
    _AUTO_CHECK_PLAN_INTERVAL_SECONDS: float = 15.0

    def _schedule_auto_check_plan(self, job_id: str) -> None:
        """Spawn background plan-poll after a job hits QR_READY.

        Fire-and-forget so `_run_handler` can release proxy lease / slot
        without waiting on ChatGPT entitlement. Polls up to 5 minutes
        (every 15s) until plan becomes `plus` or the job disappears.
        No-op if the event loop is already closing (shutdown race).
        """
        # Cancel a previous poll for the same job (rerun / double QR).
        existing = getattr(self, "_auto_check_plan_tasks", None)
        if existing is None:
            self._auto_check_plan_tasks: dict[str, asyncio.Task] = {}
            existing = self._auto_check_plan_tasks
        prev = existing.pop(job_id, None)
        if prev is not None and not prev.done():
            prev.cancel()

        try:
            task = asyncio.create_task(
                self._auto_check_plan_after_qr(job_id),
                name=f"auto-check-plan-{job_id}",
            )
        except RuntimeError:
            return

        existing[job_id] = task

        def _done(t: "asyncio.Task[None]", *, jid: str = job_id) -> None:
            bucket = getattr(self, "_auto_check_plan_tasks", None)
            if bucket is not None and bucket.get(jid) is t:
                bucket.pop(jid, None)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                import logging as _logging

                _logging.getLogger(__name__).exception(
                    "auto check_plan task failed: job_id=%s",
                    jid,
                    exc_info=exc,
                )

        task.add_done_callback(_done)

    async def _auto_check_plan_after_qr(self, job_id: str) -> None:
        """Poll `check_plan_status` until plus / timeout / job gone.

        Never raises to the scheduler. Interval 15s, wall timeout 5 minutes
        so payment after QR has time to reflect on ChatGPT entitlement.
        """
        import logging as _logging
        import time as _time

        logger = _logging.getLogger(__name__)
        deadline = _time.monotonic() + self._AUTO_CHECK_PLAN_TIMEOUT_SECONDS
        attempt = 0

        while True:
            record = self._jobs.get(job_id)
            if record is None:
                return
            # Job left QR_READY (rerun / delete / stop) → stop polling.
            if record.status != JobStatus.QR_READY:
                return
            # Already verified plus (manual Check Plus won the race).
            if record.plan == "plus":
                return

            attempt += 1
            try:
                result = await self.check_plan_status(job_id)
            except Exception:
                logger.exception(
                    "auto check_plan poll failed: job_id=%s attempt=%s",
                    job_id,
                    attempt,
                )
                result = None

            if isinstance(result, dict) and result.get("plan") == "plus":
                logger.info(
                    "auto check_plan plus: job_id=%s attempt=%s",
                    job_id,
                    attempt,
                )
                return

            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                last_plan = (
                    (result or {}).get("plan") if isinstance(result, dict) else None
                )
                logger.info(
                    "auto check_plan timeout (%ss): job_id=%s last_plan=%s attempts=%s",
                    int(self._AUTO_CHECK_PLAN_TIMEOUT_SECONDS),
                    job_id,
                    last_plan,
                    attempt,
                )
                # Chỉ notify timeout khi vẫn chưa plus (hook Telegram tag ⌛).
                rec_now = self._jobs.get(job_id)
                if rec_now is not None and rec_now.plan != "plus":
                    await self._invoke_plan_check_timeout_hooks(job_id)
                return

            await asyncio.sleep(
                min(self._AUTO_CHECK_PLAN_INTERVAL_SECONDS, remaining)
            )

    def register_push_pause_checker(
        self, checker: "Callable[[], bool]"
    ) -> None:
        """Đăng ký callable SYNC trả `True` khi Push_Success_Gate đang PAUSED.

        Scheduler gọi mỗi vòng `_await_next_pending_job_id` để quyết định
        có bỏ qua job Push_Mode trong `_pending_order` hay không.

        Payment_Module_Boundary: JobManager KHÔNG import
        `notifiers.telegram.*` — wiring qua callback do `bootstrap.py`
        inject. Best-effort: exception trong callable được swallow ở
        `_is_push_paused` (log warning + coi như `False` = không pause).

        Args:
            checker: Callable SYNC (không async) trả `bool`. Được gọi
                RẤT thường xuyên (mỗi lần scheduler quét pending queue),
                phải nhanh — chỉ đọc field bool, KHÔNG I/O.
        """
        self._push_pause_checker = checker

    def _is_push_paused(self) -> bool:
        """Wrapper an toàn quanh `_push_pause_checker` — swallow exception.

        Trả `False` nếu chưa wire hoặc checker raise (Fail_Safe: không
        để gate bug làm sập scheduler; nếu gate lỗi thì tệ nhất là
        không pause, các job vẫn chạy).
        """
        if self._push_pause_checker is None:
            return False
        try:
            return bool(self._push_pause_checker())
        except Exception:  # noqa: BLE001
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "push_pause_checker raised — treat as not paused",
                exc_info=True,
            )
            return False

    def wake_scheduler(self) -> None:
        """Đánh thức scheduler loop — set `_new_job_event`.

        Được `PushSuccessGate.resume()` gọi khi user bấm "Tiếp tục" để
        scheduler kiểm tra lại queue (có thể còn job Push_Mode chờ đã bị
        skip vì gate paused). Sync, không I/O — an toàn gọi từ bất kỳ
        đâu (kể cả sync context).
        """
        self._new_job_event.set()

    def signal_pause_running_push_jobs(self) -> None:
        """Set cờ `pause` trên `cancellation_token` cho MỌI job Push_Mode
        đang `RUNNING` — được `PushSuccessGate` gọi khi hit threshold.

        Handler `IdealFlowHandler.run` (và các payment handler khác) check
        `token.is_paused()` ở các checkpoint SAU khi login xong (từ step 3
        trở đi trong iDEAL flow) → trả `JobResult(status=STOPPED,
        pause_requested=True)`. Wrapper `_run_handler` phát hiện flag →
        chuyển record về PENDING (giữ chỗ trong queue) thay vì terminal.

        Sync (chỉ set `asyncio.Event`), an toàn gọi từ bất kỳ context nào.
        KHÔNG lock `_lock` vì chỉ đọc snapshot `_jobs.values()` và mutate
        token của từng record — thao tác atomic per-record. Race hiếm:
        job đang chuyển state (RUNNING → terminal) song song → set pause
        vào token đã cancelled/consumed vẫn OK (không có side-effect).

        Job Pull_Mode (`pull_assignment_state is not None`) KHÔNG bị áp
        dụng — Push_Success_Gate chỉ chi phối luồng Push.
        """
        for record in list(self._jobs.values()):
            if record.status != JobStatus.RUNNING:
                continue
            if record.pull_assignment_state is not None:
                continue
            try:
                record.job.cancellation_token.pause()
            except AttributeError:
                # Token cũ (trước migration) không có method `pause` →
                # bỏ qua. Job này không hỗ trợ pause, sẽ chạy tới cùng.
                pass
            task = record.handler_task
            if task is not None and not task.done():
                task.cancel()

    def register_pull_error_hook(self, hook: "PullErrorHook") -> None:
        """Đăng ký callback được gọi sau `resolve_pull_error` (job Pull_Mode
        có Job_Pull_Assignment chuyển ERROR, R13.1-R13.3).

        Best-effort giống `register_terminal_hook`: mọi exception raised
        bên trong hook được swallow tại boundary `_invoke_pull_error_hooks`
        (log warning rồi tiếp tục hook kế) — KHÔNG rollback `pull_outcome`
        đã chốt (R13.6). Dùng để `PullJobNotifier` gửi message báo lỗi kỹ
        thuật + nút "Nhận job tiếp" vào `pull_origin_chat_id` mà KHÔNG cần
        `JobManager` import `notifiers.telegram.*` (Payment_Module_Boundary R20).

        Idempotent: cho phép đăng ký nhiều hook (chạy tuần tự theo thứ tự
        đăng ký). Đăng ký cùng 1 callable 2 lần → gọi 2 lần, caller tự
        chịu trách nhiệm không double-register.

        Args:
            hook: Async callable nhận `(_JobRecord, error_code: str | None)`
                và trả về `Awaitable[None]`. Record đã có `pull_outcome =
                FAIL` được chốt bởi `resolve_pull_error`.
        """
        self._pull_error_hooks.append(hook)

    async def _invoke_pull_error_hooks(
        self, record: _JobRecord, error_code: str | None
    ) -> None:
        """Gọi TUẦN TỰ các pull-error hook cho `record` + `error_code`.

        Boundary duy nhất swallow exception per-hook — 1 hook fail KHÔNG
        chặn hook kế chạy, KHÔNG raise ra `resolve_pull_error`/`_run_handler`
        (R13.6 — lỗi gửi Telegram không rollback state). Log warning kèm
        `job_id` + hook repr để dev debug.

        Nếu list hook rỗng, sớm return để tránh overhead loop.
        """
        if not self._pull_error_hooks:
            return
        for hook in list(self._pull_error_hooks):
            try:
                await hook(record, error_code)
            except Exception:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "pull-error hook failed: job_id=%s hook=%r",
                    record.job.job_id,
                    hook,
                    exc_info=True,
                )

    def register_mode_switch_notify_hook(self, hook: "ModeSwitchNotifyHook") -> None:
        """Đăng ký callback được gọi cho MỖI job vừa bị `set_operating_mode`
        force-fail vì đang assigned/pending lúc đổi Operating_Mode (design.md
        §3.4, R3.2 bước d, e).

        Best-effort giống `register_terminal_hook`/`register_pull_error_hook`:
        mọi exception raised bên trong hook được swallow tại boundary
        `_invoke_mode_switch_notify_hooks` (log warning rồi tiếp tục hook
        kế) — KHÔNG rollback `pull_outcome` đã chốt VÀ KHÔNG chặn
        `set_operating_mode` tiếp tục set mode mới ở bước cuối (R3.6).
        Dùng để `pull_mode.py` gửi `editMessageReplyMarkup(reply_markup=None)`
        + `sendMessage` báo worker job bị hủy do đổi mode mà KHÔNG cần
        `JobManager` import `notifiers.telegram.*` (Payment_Module_Boundary R20).

        Idempotent: cho phép đăng ký nhiều hook (chạy tuần tự theo thứ tự
        đăng ký). Đăng ký cùng 1 callable 2 lần → gọi 2 lần, caller tự
        chịu trách nhiệm không double-register.

        Args:
            hook: Async callable nhận `_JobRecord` (đã có `pull_outcome =
                FAIL` chốt bởi `resolve_pull_outcome` trong
                `set_operating_mode`) và trả về `Awaitable[None]`.
        """
        self._mode_switch_notify_hooks.append(hook)

    async def _invoke_mode_switch_notify_hooks(self, record: _JobRecord) -> None:
        """Gọi TUẦN TỰ các mode-switch-notify hook cho `record` vừa bị
        force-fail do đổi Operating_Mode.

        Boundary duy nhất swallow exception per-hook — 1 hook fail KHÔNG
        chặn hook kế chạy, KHÔNG raise ra `set_operating_mode` (R3.6 — lỗi
        gọi Telegram API không được chặn việc hoàn tất đổi mode). Log
        warning kèm `job_id` + hook repr để dev debug.

        Nếu list hook rỗng, sớm return để tránh overhead loop.
        """
        if not self._mode_switch_notify_hooks:
            return
        for hook in list(self._mode_switch_notify_hooks):
            try:
                await hook(record)
            except Exception:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "mode-switch-notify hook failed: job_id=%s hook=%r",
                    record.job.job_id,
                    hook,
                    exc_info=True,
                )

    async def record_telegram_notification(
        self,
        job_id: str,
        entry: dict[str, Any],
    ) -> None:
        """Append 1 bản ghi notification vào record + persist + broadcast SSE.

        Được `TelegramNotifier.notify_qr_ready` gọi sau khi mỗi lần
        `send_photo` hoàn tất (thành công hoặc thất bại) để UI hiển thị
        history đã gửi cho từng job.

        Semantic:
            - Job đã bị delete → no-op (không tái tạo record zombie).
            - Append entry vào cuối list (thứ tự thời gian).
            - Persist ngay (upsert cùng row jobs) để survive restart.
            - Broadcast SSE event `job_notified` với payload gọn (job_id +
              entry mới nhất) — FE merge patch, KHÔNG cần re-fetch full
              record.

        Args:
            job_id: ID job đích. Nếu không tồn tại trong `_jobs`, silent
                no-op (job đã bị delete giữa lúc notifier gửi — chấp
                nhận mất track vì entry chỉ có ý nghĩa với job còn sống).
            entry: Dict shape `{"chat_id": str, "chat_label": str,
                "sent_at": float, "success": bool, "error": dict | None}`.
                KHÔNG validate shape ở đây — notifier tự build đúng shape,
                mismatch chỉ ảnh hưởng UI (FE tự phòng thủ với default).
        """
        record = self._jobs.get(job_id)
        if record is None:
            return
        record.telegram_notifications.append(entry)
        # Bump `updated_at` để row DB reflect thời điểm sự kiện mới nhất
        # (giúp sort JobList theo "activity gần nhất" trên FE nếu cần).
        record.updated_at = time.time()

        # Lưu chat_id + message_id khi gửi QR thành công → plus-check
        # reply/edit caption đúng tin nhắn (Push_Mode). Chỉ set lần đầu
        # success có message_id; không ghi đè nếu Pull_Mode đã initialize.
        if (
            entry.get("success") is True
            and entry.get("message_id") is not None
            and entry.get("chat_id")
            and record.telegram_message_id is None
        ):
            try:
                mid = int(entry["message_id"])
            except (TypeError, ValueError):
                mid = None
            if mid is not None:
                record.telegram_message_chat_id = str(entry["chat_id"])
                record.telegram_message_id = mid

        await self._persist_record(record)
        # Broadcast qua SSE — dùng event mới `job_notified` để tránh trigger
        # logic transition status ở FE (job đã terminal, status không đổi).
        await self._sse.broadcast_job_notified(job_id, entry)

    # ------------------------------------------------------------------
    # Submit / query / stop (Requirement 8.1, 8.2, 8.6, 8.7)
    # ------------------------------------------------------------------
    async def submit_batch(
        self,
        payment_method: str,
        lines: list[str],
        *,
        force_rerun: bool = False,
        start: bool = True,
    ) -> BatchSubmitResult:
        """Parse mỗi dòng qua `handler.parse_account_line`, tạo Job cho dòng
        hợp lệ theo đúng thứ tự tạo (Requirement 8.1, 8.2).

        Semantics "1 account = 1 job" theo `force_rerun` (tham chiếu UPI
        `web/manager.py::UpiJobManager.add_jobs` — mặc định add-only,
        rerun là action riêng biệt do user chủ động):

        - `force_rerun=False` (mặc định, dùng cho nút "Tạo job" trên UI):
          account có `dedup_key` đã tồn tại → SKIP, KHÔNG chạy lại. Lý do
          skip phân biệt trạng thái cũ để FE hiển thị message hợp lý:
          * pending/running → `job_already_active`.
          * terminal (qr_ready/error/stopped) → `job_already_exists`.
          User phải chủ động dùng "Rerun" trên từng row, "Retry failed"
          bulk, hoặc xóa job cũ rồi tạo lại nếu muốn chạy lại.

        - `force_rerun=True` (dùng cho `rerun_job` API + `rerun-failed`
          bulk + CLI `run` single-shot): account đã có job:
          * pending/running → vẫn skip `job_already_active` (tránh
            double-schedule khi user bấm rerun trên job đang chạy).
          * terminal → RESET record cũ về `pending` (giữ `job_id`, xóa
            `logs`/`artifact_path`/`error_*`/`payment_link`, cấp
            `cancellation_token` mới) rồi push lại vào `_pending_order`.
            Frontend giữ nguyên item trong list, scheduler chạy lại flow
            trên cùng job_id đó.

        Handler KHÔNG implement `get_account_dedup_key(parsed)` → không dedup,
        mọi dòng tạo job mới (behavior cũ, backward-compatible).

        Snapshot Settings được chụp 1 lần cho toàn batch (Requirement 11.2 —
        mọi job trong cùng batch dùng cùng snapshot; user đổi Settings sau
        khi submit_batch trả về sẽ không retroactive tới job đã tạo).

        `skipped_lines` trả về đầy đủ danh sách dòng lỗi kèm reason cụ thể,
        KHÔNG raise nếu có dòng lỗi (chỉ raise `UnknownPaymentMethodError`
        khi `payment_method` chưa đăng ký handler).
        """
        handler = self._handlers.get(payment_method)
        if handler is None:
            raise UnknownPaymentMethodError(payment_method)

        snapshot = await self.get_settings_snapshot_for_new_jobs()
        created_ids: list[str] = []
        skipped: list[dict[str, str]] = []
        now = time.time()

        # Đọc live `telegram.mode` MỘT LẦN cho toàn batch (KHÔNG đọc lại
        # mỗi dòng) — mọi dòng hợp lệ trong lần gọi `submit_batch` này
        # được xử lý nhất quán theo cùng 1 quyết định Push/Pull, tránh 1
        # batch vừa có job vào `_pending_order` vừa có job vào
        # Pull_Account_Pool nếu `telegram.mode` đổi giữa lúc xử lý (design.md
        # §3.2, Requirement 4.1-4.6, 7.1-7.3).
        #
        # Đọc từ `snapshot` (đã có sẵn qua `get_settings_snapshot_for_new_jobs()`
        # → `SettingsRepository.list()`) THAY VÌ gọi thêm `self._settings.get(...)`
        # — tránh round-trip Settings thừa (list() đã trả toàn bộ key-value)
        # VÀ giữ tương thích với test double chỉ implement `list()` (không
        # có `get()`), khớp đúng interface `SettingsRepository` mà
        # `submit_batch` đã phụ thuộc từ trước.
        current_mode = snapshot.get("telegram.mode")
        is_pull_mode = current_mode == "pull"

        get_dedup_key = getattr(handler, "get_account_dedup_key", None)

        # Job ids whose plan was cleared by force_rerun — defer counted
        # outcomes (`1`→`2`) in one pass after the loop so multi-account
        # "retry failed" does not commit once per row.
        rerun_defer_job_ids: list[str] = []

        for line in lines:
            parsed = handler.parse_account_line(line)
            if isinstance(parsed, AccountLineError):
                skipped.append({"line": parsed.line, "reason": parsed.reason})
                continue
            if not isinstance(parsed, ParsedAccount):
                # Handler vi phạm contract — parse_account_line phải trả 1
                # trong 2 kiểu ParsedAccount | AccountLineError. Fail_Fast,
                # không suy diễn ngầm.
                raise HandlerContractError(payment_method, "parse_account_line")

            dedup_key: str | None = None
            if callable(get_dedup_key):
                raw_key = get_dedup_key(parsed)
                if isinstance(raw_key, str) and raw_key:
                    dedup_key = f"{payment_method}:{raw_key}"

            # Lookup job đã tồn tại cùng dedup_key → reuse thay vì tạo mới.
            existing_job_id = self._dedup_index.get(dedup_key) if dedup_key else None
            existing = self._jobs.get(existing_job_id) if existing_job_id else None

            if existing is not None:
                if existing.status in (JobStatus.PENDING, JobStatus.RUNNING):
                    # Đã có job đang xử lý cùng account — không schedule kép.
                    skipped.append({"line": line, "reason": "job_already_active"})
                    continue
                if not force_rerun:
                    # Account đã có job terminal (qr_ready/error/stopped) và
                    # caller KHÔNG yêu cầu force rerun → skip theo semantic
                    # "Tạo job = add-only" (giống UPI `add_jobs`). User muốn
                    # chạy lại phải dùng nút Rerun / Retry failed / xóa job.
                    skipped.append({"line": line, "reason": "job_already_exists"})
                    continue
                # Terminal + force_rerun=True → reset record về pending, giữ job_id ổn định
                # VÀ giữ nguyên vị trí trong `_jobs` dict (không đụng chạm
                # insertion order — dù retry_failed hay user submit lại).
                # Nguyên tắc: một khi job đã có vị trí trong list, vị trí
                # đó là stable cho tới khi user xóa job. Không hàm nào
                # được phép reorder ngầm.
                error_message_for_classification = existing.error_message
                if existing.error_code == "login_failed":
                    log_messages = "\n".join(
                        str(entry.get("message", "")) for entry in existing.logs
                    )
                    error_message_for_classification = (
                        f"{existing.error_message or ''}\n{log_messages}"
                    )
                non_rerunnable_reason = _non_rerunnable_error_reason(
                    existing.error_code,
                    error_message_for_classification,
                )
                if non_rerunnable_reason is not None:
                    skipped.append(
                        {
                            "line": line,
                            "reason": non_rerunnable_reason,
                        }
                    )
                    continue
                new_token = SimpleCancellationToken()
                existing.job = Job(
                    job_id=existing.job.job_id,
                    payment_method=payment_method,
                    account_line=line,
                    created_at=now,
                    cancellation_token=new_token,
                )
                existing.status = JobStatus.PENDING
                existing.updated_at = now
                existing.logs.clear()
                existing.artifact_path = None
                existing.error_code = None
                existing.error_message = None
                existing.payment_link = None
                existing.qr_expires_at = None
                existing.telegram_message_id = None
                existing.lease = None
                existing.handler_task = None
                existing.settings_snapshot = dict(snapshot)
                # Reset timing: job cũ đã chạy xong (terminal), rerun sẽ
                # tính elapsed lại từ đầu. Không giữ started_at cũ vì FE
                # sẽ hiển thị elapsed sai (đếm cả khoảng chờ giữa 2 lần
                # chạy).
                existing.started_at = None
                existing.finished_at = None
                # User thao tác thủ công (submit lại / rerun UI) → reset
                # retry budget về 0 và cancel task delayed-requeue đang
                # sleep (nếu có) để tránh double-enqueue khi user vừa
                # bấm "Rerun" trước khi timer auto-retry kịp trigger.
                existing.retry_count = 0
                # Reset `plan` — user rerun là cơ hội để tài khoản mới
                # đi qua flow tạo QR / thanh toán lại → plan cũ (nếu có)
                # không còn phản ánh trạng thái thật. Để None để lần
                # Check Plus tiếp theo (per-row hoặc bulk) verify lại
                # từ đầu. FE reactive theo `plan` — badge PLUS/FREE cũ
                # biến mất ngay.
                existing.plan = None
                # Defer durable plan-outcome claim for later period close
                # (counted `1`→`2`); never-counted `0` stays claimable.
                # Batched after the loop — see `rerun_defer_job_ids`.
                rerun_defer_job_ids.append(existing.job.job_id)
                self._cancel_delayed_requeue(existing.job.job_id)
                self._cancel_auto_check_plan(existing.job.job_id)
                # Pull_Mode (R4.1-4.6): nếu `telegram.mode == "pull"` tại
                # thời điểm rerun, job đi vào Pull_Account_Pool (chưa
                # assign) THAY VÌ vào `_pending_order` — áp dụng đồng
                # nhất cho nhánh dedup-reuse này giống nhánh tạo job mới.
                # KHÔNG áp dụng cho job đang pending/running (đã skip ở
                # trên với `job_already_active`) — chỉ nhánh terminal
                # được reset tới đây.
                if is_pull_mode:
                    existing.pull_assignment_state = PullAssignmentState.UNASSIGNED
                    existing.pull_outcome = PullOutcome.PENDING
                    # Pull_Mode: Pull_Account_Pool đã sẵn semantic "chờ
                    # worker claim" — `held` không dùng, giữ False để
                    # tránh double-holding logic. `start` flag chỉ có ý
                    # nghĩa với Push_Mode.
                    existing.held = False
                elif start:
                    existing.held = False
                    self._pending_order.append(existing.job.job_id)
                else:
                    # Push_Mode + start=False → user bấm "+ Add", job
                    # ở lại trạng thái `pending + held=True` cho tới khi
                    # `start_job` được gọi.
                    existing.held = True
                created_ids.append(existing.job.job_id)
                # `order` giữ nguyên giá trị cũ của `existing` — dedup
                # reuse KHÔNG tăng counter. FE dùng order để sort → vị
                # trí job không đổi khi retry/rerun.
                await self._persist_record(existing)
                await self._broadcast_status(existing, JobStatus.PENDING.value)
                continue

            # Tạo job mới — append cuối `_jobs` dict + gán `order` mới.
            job_id = uuid.uuid4().hex
            token = SimpleCancellationToken()
            job = Job(
                job_id=job_id,
                payment_method=payment_method,
                account_line=line,
                created_at=now,
                cancellation_token=token,
            )
            self._order_counter += 1
            record = _JobRecord(
                job=job,
                status=JobStatus.PENDING,
                logs=_new_logs_deque(),
                lease=None,
                settings_snapshot=dict(snapshot),
                updated_at=now,
                handler_task=None,
                dedup_key=dedup_key,
                order=self._order_counter,
            )
            # Pull_Mode (R4.1-4.6): job vừa tạo đi vào Pull_Account_Pool
            # (chưa assign cho worker nào) THAY VÌ vào `_pending_order`
            # khi `telegram.mode == "pull"` tại thời điểm submit_batch này.
            # Push_Mode (hoặc key chưa set) giữ 100% hành vi hiện tại —
            # 2 field mới giữ giá trị default `None`.
            if is_pull_mode:
                record.pull_assignment_state = PullAssignmentState.UNASSIGNED
                record.pull_outcome = PullOutcome.PENDING
            # Push_Mode + start=False → hold. Pull_Mode: bỏ qua `start`
            # (semantic tương đương đã sẵn có qua Pull_Account_Pool).
            elif not start:
                record.held = True
            self._jobs[job_id] = record
            if not is_pull_mode and not record.held:
                self._pending_order.append(job_id)
            created_ids.append(job_id)
            if dedup_key:
                self._dedup_index[dedup_key] = job_id
            await self._persist_record(record)
            # Phát SSE ngay khi job được tạo (R8.8: <2s state change).
            # `_broadcast_status` tự kèm `updated_at + order` để FE có
            # sort key + timestamp chính xác từ event đầu tiên.
            await self._broadcast_status(record, JobStatus.PENDING.value)

        # Defer plan-outcome claims for every force_rerun that cleared
        # `plan` — one commit for the batch (not N+1). WHERE flag=1
        # no-ops never-counted / already-deferred rows.
        if self._job_repo is not None and rerun_defer_job_ids:
            try:
                await self._job_repo.defer_plan_outcome_rearm_many(
                    rerun_defer_job_ids
                )
            except Exception:
                import logging as _logging
                _logging.getLogger(__name__).exception(
                    "defer_plan_outcome_rearm on force_rerun failed: "
                    "job_ids=%s",
                    rerun_defer_job_ids,
                )

        # Chỉ khởi scheduler khi có ít nhất 1 job mới — không tốn task nền
        # nếu toàn batch là dòng lỗi.
        if created_ids:
            self._ensure_scheduler_running()
            self._new_job_event.set()

        return BatchSubmitResult(created_job_ids=created_ids, skipped=skipped)

    async def stop(self, job_id: str) -> None:
        """Yêu cầu dừng 1 job (Requirement 8.6).

        - Job `pending` → chuyển thẳng `stopped`, xóa khỏi hàng đợi, phát SSE.
        - Job `running` → set cancellation_token, handler tự thoát trong lần
          check kế tiếp; wrapper `_run_handler` sẽ release lease + slot và
          phát SSE khi handler thực sự exit.
        - Job ở trạng thái terminal (qr_ready/error/stopped) → no-op.
        - `job_id` không tồn tại → no-op (API layer chịu trách nhiệm 404).
        """
        record = self._jobs.get(job_id)
        if record is None:
            return

        # User chủ động dừng → cancel task delayed-requeue đang chờ (nếu
        # có). Không phân biệt status trước vì task requeue chỉ tồn tại
        # khi job đang ở ERROR chờ auto-retry — cancel idempotent.
        self._cancel_delayed_requeue(job_id)
        self._cancel_auto_check_plan(job_id)

        if record.status == JobStatus.PENDING:
            async with self._lock:
                try:
                    self._pending_order.remove(job_id)
                except ValueError:
                    pass
                record.status = JobStatus.STOPPED
                record.updated_at = time.time()
                # Job chưa từng RUNNING → `started_at` vẫn None. Set
                # `finished_at` để đóng băng elapsed = 0 ở FE (job kết
                # thúc mà không có thời gian chạy thực tế).
                record.finished_at = record.updated_at
                record.job.cancellation_token.cancel()
            await self._persist_record(record)
            await self._broadcast_status(record, JobStatus.STOPPED.value)
            # FIX 7: job Pull_Mode đang ASSIGNED + PENDING bị stop khi còn
            # chờ trong hàng đợi → chốt FAIL để giải phóng slot worker.
            # Gọi NGOÀI `self._lock` (đã thoát block) — `resolve_pull_outcome`
            # dùng lock per-job, không đụng `self._lock`.
            await self._resolve_stopped_pull_job(record)
            return

        if record.status == JobStatus.RUNNING:
            # Signal cancel — handler sẽ tự thoát; `_run_handler` cập nhật
            # state và release lease/slot trong finally block.
            record.job.cancellation_token.cancel()

    # ------------------------------------------------------------------
    # Start held jobs (feature Add / Run tách bạch)
    # ------------------------------------------------------------------
    async def start_job(self, job_id: str) -> bool:
        """Chuyển 1 job đang `pending + held=True` vào `_pending_order`
        để scheduler chạy.

        Trả:
            True nếu job vừa được start (transition thực sự).
            False nếu:
              - Job không tồn tại (caller layer trả 404).
              - Job không ở trạng thái `pending` (VD đã RUNNING / terminal).
              - Job không bị held (đã không cần start — no-op idempotent).
              - Job Pull_Mode (không dùng cơ chế held — Pull đã có
                Pull_Account_Pool).

        Không raise — caller (API route) tự map False sang HTTP 409 nếu
        cần phân biệt "already started" với "not found". Semantics đơn
        giản để bulk `start_all_held` gọi lặp không cần try/except.
        """
        record = self._jobs.get(job_id)
        if record is None:
            return False
        if record.status != JobStatus.PENDING:
            return False
        if not record.held:
            return False
        if record.pull_assignment_state is not None:
            # Job Pull_Mode — bảo vệ chống race giữa migration chưa clear
            # held cho row Pull cũ và user bấm Start. Pull đã có cơ chế
            # riêng qua Pull_Account_Pool.
            return False

        async with self._lock:
            record.held = False
            record.updated_at = time.time()
            self._pending_order.append(job_id)
            self._ensure_scheduler_running()
            self._new_job_event.set()

        await self._persist_record(record)
        # `status` không đổi (vẫn PENDING) — FE nhận SSE + merge patch
        # `held: false` sang view model để bỏ badge / nút Start.
        await self._broadcast_status(record, JobStatus.PENDING.value)
        return True

    async def start_all_held(self) -> list[str]:
        """Bulk start mọi job `pending + held=True` — mirror
        `stop_all_active` / `retry_failed_jobs`.

        Iterate snapshot của `_jobs` (không giữ lock cả vòng lặp — mỗi
        `start_job` tự lấy lock riêng cho đúng đường ghi state của nó,
        tránh chặn scheduler quá lâu khi held count lớn).

        Returns:
            List `job_id` vừa được start (theo thứ tự order tăng dần).
        """
        # Snapshot list theo `order` để bulk start giữ đúng FIFO.
        candidates = sorted(
            (
                r for r in self._jobs.values()
                if r.status == JobStatus.PENDING
                and r.held
                and r.pull_assignment_state is None
            ),
            key=lambda r: r.order,
        )
        started: list[str] = []
        for record in candidates:
            if await self.start_job(record.job.job_id):
                started.append(record.job.job_id)
        return started

    # ------------------------------------------------------------------
    # Settings snapshot / concurrency reconfig (R11.2, R13.2, R13.5)
    # ------------------------------------------------------------------
    async def get_settings_snapshot_for_new_jobs(self) -> dict[str, Any]:
        """Chụp snapshot toàn bộ Settings tại thời điểm gọi (Requirement 11.2).

        Deviation từ design signature (sync) → async: `SettingsRepository`
        là async (aiosqlite), không thể trả sync dict mà không block event
        loop. Semantic quan trọng nhất — snapshot-at-creation-time — được
        giữ nguyên: mỗi lần `submit_batch` gọi hàm này 1 lần cho cả batch,
        và các job trong batch dùng cùng snapshot bất kể user đổi Settings
        sau đó.
        """
        return await self._settings.list()

    async def update_method_max_concurrent(
        self, payment_method: str, new_value: int
    ) -> None:
        """Rebuild limiter của ĐÚNG 1 payment_method, KHÔNG giết job running.

        Cơ chế đảm bảo capacity method luôn `<= new_value` sau khi các job
        over-budget kết thúc:
        - Đếm job RUNNING của method đó.
        - Ghi `configured_max = new_value`.
        - Nếu `running_count > new_value`: `over_budget_slots = running -
          new_value`. Mỗi job over-budget finish → `_release_slot(method)`
          giảm counter thay vì free capacity mới.
        - Đánh thức scheduler (limit tăng → có thể lấy pending của method).
        """
        if not isinstance(new_value, int) or isinstance(new_value, bool):
            raise TypeError(
                f"max_concurrent phải là int, nhận {type(new_value).__name__}"
            )
        if new_value < 1:
            raise ValueError(f"max_concurrent phải >= 1, nhận {new_value}")

        async with self._lock:
            limiter = self._method_limiters.get(payment_method)
            if limiter is None:
                # Method chưa register — tạo entry để settings apply sớm
                # không mất giá trị; register_handler sẽ reuse.
                limiter = _MethodLimiter()
                self._method_limiters[payment_method] = limiter

            running_count = sum(
                1
                for r in self._jobs.values()
                if r.status == JobStatus.RUNNING
                and r.job.payment_method == payment_method
            )
            # in_use có thể lệch nếu gọi khi không có job (idle) — neo về
            # running_count thực tế để over_budget đúng.
            if limiter.in_use < running_count:
                limiter.in_use = running_count
            limiter.configured_max = new_value
            limiter.over_budget_slots = max(0, running_count - new_value)

            if (
                self._scheduler_task is not None
                and not self._scheduler_task.done()
            ):
                # Không còn block trên global semaphore; chỉ cần wake event.
                # Vẫn cancel+respawn nếu scheduler kẹt chờ event cũ — an toàn.
                pass
            if self._pending_order:
                self._ensure_scheduler_running()
            self._new_job_event.set()

    async def update_max_concurrent(self, new_value: int) -> None:
        """Backward-compat shim: apply `new_value` cho MỌI method đã đăng ký.

        Production path dùng `update_method_max_concurrent` / settings
        per-key. Shim giữ unit test & caller cũ (`update_max_concurrent(N)`
        single-method) không gãy. Multi-method production KHÔNG nên gọi
        shim này — nó cố ý set cùng limit cho tất cả method.
        """
        if not isinstance(new_value, int) or isinstance(new_value, bool):
            raise TypeError(
                f"max_concurrent phải là int, nhận {type(new_value).__name__}"
            )
        if new_value < 1:
            raise ValueError(f"max_concurrent phải >= 1, nhận {new_value}")

        methods = list(self._method_limiters.keys())
        if not methods:
            # Chưa register handler — lưu entry ẩn; register sẽ có limiter
            # riêng (default 1) và caller thường update sau register.
            return
        for payment_method in methods:
            await self.update_method_max_concurrent(payment_method, new_value)

    def get_concurrency_keys(self) -> set[str]:
        """Trả set các Settings-key concurrency của TẤT CẢ handler đã đăng ký.

        Dùng cho tầng API (`routes_settings`) và bootstrap: khi user ghi
        1 trong các key này qua HTTP, hoặc khi server khởi động, cần đồng
        bộ giá trị vào limiter runtime per-method — nếu bỏ qua thì DB có
        giá trị mới nhưng limiter vẫn giữ default 1.
        """
        return {handler.get_max_concurrent_key() for handler in self._handlers.values()}

    def _iter_method_concurrency_keys(self) -> list[tuple[str, str]]:
        """Stable (payment_method, settings_key) pairs theo thứ tự register."""
        return [
            (method, handler.get_max_concurrent_key())
            for method, handler in self._handlers.items()
        ]

    async def apply_max_concurrent_from_settings(self) -> None:
        """Đọc concurrency settings và apply TỪNG method vào limiter riêng.

        Được `bootstrap_services()` gọi 1 lần sau khi mọi handler đã đăng
        ký. Mỗi `(payment_method, key)` cập nhật CHỈ limiter của method
        đó — không last-write-wins global.

        Fail-safe: key None / invalid → giữ limiter hiện tại, không crash.
        """
        for payment_method, key in self._iter_method_concurrency_keys():
            value = await self._settings.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
                await self.update_method_max_concurrent(payment_method, value)

    async def maybe_reload_concurrency(self, changed_keys: list[str]) -> None:
        """Reload limiter chỉ cho method có key nằm trong `changed_keys`.

        Write-through từ `routes_settings`: Multi 1→2 kick scheduler; Multi
        3→2 set over_budget per-method, không stop job đang chạy.
        """
        pairs = self._iter_method_concurrency_keys()
        if not any(key in changed_keys for _, key in pairs):
            return
        for payment_method, key in pairs:
            if key not in changed_keys:
                continue
            value = await self._settings.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
                await self.update_method_max_concurrent(payment_method, value)

    async def delete_job(self, job_id: str) -> bool:
        """Xóa job khỏi list (hard delete — không phục hồi được).

        - Job pending → xóa khỏi `_pending_order` + `_jobs` dict.
        - Job running → cancel token trước (handler thoát trong lần check
          kế), rồi xóa khỏi `_jobs` (record vẫn giữ trong closure của
          handler_task nếu đang chạy — nhưng chúng ta chấp nhận, vì mục
          tiêu là xóa khỏi UI list).
        - Job terminal → xóa ngay khỏi `_jobs`.
        - Job không tồn tại → return False.

        Returns True nếu đã xóa, False nếu không tồn tại. Payment_Module_
        Boundary: KHÔNG xóa artifact PNG khỏi disk ở đây — trách nhiệm đó
        thuộc payment module (nếu muốn dọn), core chỉ thao tác trên state
        in-memory của JobManager.
        """
        record = self._jobs.get(job_id)
        if record is None:
            return False

        # Cancel task delayed-requeue trước khi async lock — task đó chỉ
        # đọc `self._jobs` sau `asyncio.sleep`, cancel ngay để không kick
        # job đã bị xóa vào queue (dẫn tới "ghost job" chạy trên record
        # `None`, panic ở scheduler loop).
        self._cancel_delayed_requeue(job_id)
        self._cancel_auto_check_plan(job_id)

        async with self._lock:
            # Nếu đang running, signal cancel — handler sẽ tự exit.
            if record.status == JobStatus.RUNNING:
                record.job.cancellation_token.cancel()

            try:
                self._pending_order.remove(job_id)
            except ValueError:
                pass
            # Xoá cả dedup index để account cùng email có thể tạo job mới sau.
            if record.dedup_key and self._dedup_index.get(record.dedup_key) == job_id:
                self._dedup_index.pop(record.dedup_key, None)
            self._jobs.pop(job_id, None)
        await self._delete_persisted(job_id)
        self._invoke_job_removed_hooks(job_id)
        return True

    async def check_plan_status(self, job_id: str) -> dict[str, Any] | None:
        """Delegate check-plan cho handler tương ứng.

        Return `None` khi job_id không tồn tại — route layer trả 404.
        Return dict (có key `plan`) khi handler trả về. Nếu handler không
        implement method `check_plan_status`, trả `{"plan": "unknown",
        "error": "not_supported"}`.

        Persist `record.plan` để nhớ kết quả qua reload/restart (yêu cầu
        2026-07 — Successful accounts luôn hiện, chỉ Clear all mới mất):
          - Kết quả `"plus"` / `"free"` → ghi vào `record.plan` + persist
            DB. FE dùng `plan` từ `GET /api/jobs` để render badge và
            SuccessOutputPanel — không cần in-memory `planStates` nữa.
          - Kết quả `"unknown"` (transport error / no_session_cached /
            handler thiếu check_plan_status) → KHÔNG ghi (coi như chưa
            check), giữ nguyên giá trị cũ để lần sau còn cơ hội thử lại.
        Broadcast `job_status` sau khi persist để các client khác đang
        mở trang cũng nhận merge patch mà không cần refresh.
        """
        record = self._jobs.get(job_id)
        if record is None:
            return None

        handler = self._handlers.get(record.job.payment_method)
        if handler is None:
            return {"plan": "unknown", "error": "unknown_payment_method"}

        check_method = getattr(handler, "check_plan_status", None)
        if not callable(check_method):
            return {"plan": "unknown", "error": "not_supported"}

        job_to_check = record.job
        if record.payment_link and getattr(job_to_check, "payment_link", None) != record.payment_link:
            import dataclasses
            job_to_check = dataclasses.replace(job_to_check, payment_link=record.payment_link)

        result = await check_method(job_to_check)

        # Chỉ persist khi kết quả rõ ràng — "unknown" coi như chưa check.
        # Whitelist chống dữ liệu bất ngờ (VD "premium" từ handler khác).
        plan_value = result.get("plan") if isinstance(result, dict) else None
        became_plus = False
        if plan_value in ("plus", "free") and record.plan != plan_value:
            became_plus = plan_value == "plus"
            record.plan = plan_value
            await self._persist_record(record)
            # Broadcast để các tab client khác nhận cập nhật badge plan
            # ngay (không cần user F5). `_broadcast_status` tự kèm mọi
            # field patch qua kwargs — thêm `plan` để FE merge vào
            # JobViewModel qua `applyStatusEvent`.
            await self._broadcast_status(
                record, record.status.value, plan=plan_value
            )

        # Lần đầu plan → plus (auto-check hoặc manual) → tag Telegram worker.
        if became_plus:
            await self._invoke_plus_verified_hooks(job_id)

        return result

    async def mark_plus_verified_from_signal(
        self,
        job_id: str,
        *,
        source: str,
        detail: str | None = None,
    ) -> bool:
        """Mark a job as Plus from a trusted non-check-plan signal.

        Used by payment flows that keep a browser alive after QR_READY. When
        the browser reaches a success/onboarding URL, the cached API token may
        already be stale, so waiting for check-plan can miss the success.
        """
        record = self._jobs.get(job_id)
        if record is None:
            return False
        if record.plan == "plus":
            return False

        record.plan = "plus"
        await self._append_log(
            job_id,
            "plus_verified_signal",
            source=source,
            detail=detail,
        )
        await self._persist_record(record)
        await self._broadcast_status(record, record.status.value, plan="plus")
        await self._invoke_plus_verified_hooks(job_id)
        return True

    async def defer_qr_ready_for_gate(
        self,
        job_id: str,
        *,
        reason: str = "push_gate_full_before_notify",
    ) -> bool:
        """Move an unsent QR_READY push job back to pending.

        Telegram Live QR gate uses this when capacity is already reserved/full
        before a QR_READY notification is sent. The old QR artifact/link is
        discarded; the later retry will create a fresh QR instead of sending a
        stale one after waiting in the notifier queue.
        """
        record = self._jobs.get(job_id)
        if record is None:
            return False
        if record.status != JobStatus.QR_READY:
            return False
        if record.pull_assignment_state is not None:
            return False
        await self._handle_pause_requested(record, reason=reason)
        self._new_job_event.set()
        return True

    # ------------------------------------------------------------------
    # Pull_Mode — claim account từ Pull_Account_Pool (design.md §3.1,
    # R4.6, R5.6, R5.8, R6.2, R6.3, R7.1, R7.2, R7.3, R22.1, R22.2, R22.5,
    # R22.6, R22.10)
    # ------------------------------------------------------------------
    def get_worker_active_pull_count(self, telegram_user_id: str) -> int:
        """Đếm số job Pull_Mode đang "active" (đã gán, chưa resolve) của
        1 worker (Requirement 6.2).

        Active = `pull_assignment_state == ASSIGNED AND pull_outcome ==
        PENDING`. Sync, chỉ đọc `self._jobs` — KHÔNG cần `self._lock`
        (dict iteration snapshot đủ cho mục đích hiển thị/check trước khi
        claim; `claim_pull_account` tự re-count dưới lock để tránh TOCTOU,
        R22.10).
        """
        return sum(
            1
            for record in self._jobs.values()
            if record.pull_assigned_telegram_user_id == telegram_user_id
            and record.pull_assignment_state == PullAssignmentState.ASSIGNED
            and record.pull_outcome == PullOutcome.PENDING
        )

    async def claim_pull_account(
        self,
        telegram_user_id: str,
        chat_id: str,
        username: str | None,
        first_name: str | None,
    ) -> str | None:
        """Dequeue 1 account `unassigned` đầu FIFO (theo `order` tăng dần)
        trong Pull_Account_Pool, gán cho worker `telegram_user_id`, đưa
        vào `_pending_order` để scheduler chạy ngay (R5.6, R5.8, R7.3).

        Atomic dưới `self._lock` — bảo đảm (R5.8, R22.1, R22.2, R22.5,
        R22.10):
          - Không gán trùng 1 account cho 2 job.
          - Không để 1 worker vượt
            `telegram.pull_mode.max_concurrent_jobs_per_user` (đọc live
            từ Settings_Store TẠI THỜI ĐIỂM gọi, R6.3).

        Method này là data operation thuần — KHÔNG tự check `chat_id` có
        thuộc `allowed_chat_ids` hay không (caller tự check trước, R5.2),
        cũng KHÔNG tự check `telegram.mode == "pull"` (caller
        Pull_Job_Coordinator tự check, R5.3). `chat_id` chỉ được lưu vào
        `pull_origin_chat_id` để các thông báo tiếp theo gửi đúng đích.

        Returns:
            `job_id` vừa gán, hoặc `None` nếu:
              - Worker đã đạt giới hạn concurrent (R5.4).
              - Pull_Account_Pool rỗng (R5.5).
            Coordinator dựa vào `None` + tự phân biệt lý do bằng cách gọi
            `get_worker_active_pull_count()` trước để build message phù hợp.
        """
        async with self._lock:
            # (a) + (b): đếm job active của worker này NGAY TRONG lock —
            # không tách rời khỏi bước dequeue để tránh TOCTOU (R22.10,
            # R22.2). Tái dùng đúng logic đếm của
            # `get_worker_active_pull_count` (method sync, gọi trực tiếp
            # từ trong lock của CHÍNH task này là an toàn vì `asyncio.Lock`
            # không bảo vệ CPU-bound sync code, chỉ chặn interleaving
            # giữa các coroutine — gọi 1 method sync thuần đọc dict không
            # tạo race mới).
            active_count = self.get_worker_active_pull_count(telegram_user_id)

            raw_limit = await self._settings.get(
                "telegram.pull_mode.max_concurrent_jobs_per_user"
            )
            # Fail-safe (giống `apply_max_concurrent_from_settings`): giá
            # trị chưa set hoặc không hợp lệ (không phải int >= 1) → coi
            # như KHÔNG có capacity, trả None thay vì crash. Trong thực
            # tế key này luôn có giá trị sau khi
            # `register_telegram_namespace` seed default = 1.
            if (
                not isinstance(raw_limit, int)
                or isinstance(raw_limit, bool)
                or raw_limit < 1
            ):
                return None
            limit = raw_limit

            if active_count >= limit:
                return None

            # (c) Tìm account `unassigned` đầu tiên theo `order` tăng dần.
            candidate: _JobRecord | None = None
            for record in sorted(self._jobs.values(), key=lambda r: r.order):
                if record.pull_assignment_state == PullAssignmentState.UNASSIGNED:
                    candidate = record
                    break
            if candidate is None:
                return None

            # (d) Gán account cho worker.
            now = time.time()
            candidate.pull_assignment_state = PullAssignmentState.ASSIGNED
            candidate.pull_assigned_telegram_user_id = telegram_user_id
            candidate.pull_origin_chat_id = chat_id
            candidate.pull_assigned_username = username
            candidate.pull_assigned_first_name = first_name
            candidate.updated_at = now

            # (e) Đưa job vào `_pending_order` để scheduler chạy ngay —
            # đúng cơ chế đánh thức đã dùng ở `submit_batch`.
            self._pending_order.append(candidate.job.job_id)
            self._ensure_scheduler_running()
            self._new_job_event.set()

            # (f) Persist + broadcast — status job KHÔNG đổi (vẫn PENDING),
            # chỉ các field pull_* thay đổi.
            await self._persist_record(candidate)
            await self._broadcast_status(candidate, candidate.status.value)

            return candidate.job.job_id

    async def claim_pull_batch(
        self,
        telegram_user_id: str,
        chat_id: str,
        username: str | None,
        first_name: str | None,
    ) -> tuple[str, list[str]]:
        """Claim MỘT BATCH tối đa N account `unassigned` đầu FIFO cho worker
        `telegram_user_id` trong 1 lần bấm "Nhận job" — N =
        `telegram.pull_mode.max_concurrent_jobs_per_user` (đọc live).

        Ngữ nghĩa batch (thay model claim-từng-cái cũ):
            - Worker CHỈ được claim batch mới khi đã xử lý xong batch trước
              (không còn job nào ASSIGNED + PENDING). Đây là cách áp
              "phải xử lý hết mới nhận job mới" — gate `active_count > 0`.
            - Claim đồng thời tối đa N account, đưa cả N vào `_pending_order`
              để scheduler chạy song song (giới hạn thực tế bởi
              `_semaphore` = `max_concurrent` global của tool).

        Atomic dưới `self._lock` — count active + dequeue N account trong
        cùng 1 critical section (tránh TOCTOU y hệt `claim_pull_account`).

        Returns:
            Tuple `(status, job_ids)`:
              - `("holding", [])`  : worker còn job batch cũ chưa xử lý hết.
              - `("empty", [])`    : pool rỗng (không account nào unassigned)
                                     HOẶC `max_concurrent` chưa cấu hình hợp lệ.
              - `("claimed", ids)` : đã claim ≥1 account (len ≤ N).
        """
        async with self._lock:
            active_count = self.get_worker_active_pull_count(telegram_user_id)
            if active_count > 0:
                return ("holding", [])

            raw_limit = await self._settings.get(
                "telegram.pull_mode.max_concurrent_jobs_per_user"
            )
            if (
                not isinstance(raw_limit, int)
                or isinstance(raw_limit, bool)
                or raw_limit < 1
            ):
                return ("empty", [])
            limit = raw_limit

            now = time.time()
            claimed_ids: list[str] = []
            for record in sorted(self._jobs.values(), key=lambda r: r.order):
                if len(claimed_ids) >= limit:
                    break
                if record.pull_assignment_state != PullAssignmentState.UNASSIGNED:
                    continue
                record.pull_assignment_state = PullAssignmentState.ASSIGNED
                record.pull_assigned_telegram_user_id = telegram_user_id
                record.pull_origin_chat_id = chat_id
                record.pull_assigned_username = username
                record.pull_assigned_first_name = first_name
                record.updated_at = now
                self._pending_order.append(record.job.job_id)
                claimed_ids.append(record.job.job_id)

            if not claimed_ids:
                return ("empty", [])

            self._ensure_scheduler_running()
            self._new_job_event.set()

            for job_id in claimed_ids:
                claimed = self._jobs[job_id]
                await self._persist_record(claimed)
                await self._broadcast_status(claimed, claimed.status.value)

            return ("claimed", claimed_ids)

    def _get_pull_job_lock(self, job_id: str) -> asyncio.Lock:
        """Trả `asyncio.Lock` riêng cho `job_id`, lazy-create nếu chưa có.

        Dùng cho các thao tác Pull_Mode cần atomic trên đúng 1
        `_JobRecord` (VD `resolve_pull_outcome`, và về sau
        `record_plus_check_transition`) mà KHÔNG chặn toàn bộ
        JobManager qua `self._lock` chung — nhiều job khác nhau resolve
        đồng thời vẫn chạy song song, chỉ race trên CÙNG 1 job_id mới bị
        serialize (design.md §3.1, R22.11).

        Lock được dọn khỏi `self._pull_job_locks` qua job-removed hook
        đăng ký ở `__init__` khi job bị xoá vĩnh viễn — tránh leak dict
        theo tổng số job đã từng tồn tại.
        """
        if job_id not in self._pull_job_locks:
            self._pull_job_locks[job_id] = asyncio.Lock()
        return self._pull_job_locks[job_id]

    async def resolve_pull_outcome(self, job_id: str, outcome: "PullOutcome") -> None:
        """Chốt kết quả tính công cho 1 job Pull_Mode — nguồn DUY NHẤT ghi
        `pull_outcome` (design.md §3.1, R20.5, R20.6, R22.4, R22.7).

        Atomic dưới lock per-job (`_get_pull_job_lock`) — 4 đường kết thúc
        khác nhau (verify Plus thành công, hết lượt check, worker tự bấm
        Thất_Bại, job chuyển ERROR) đều đi qua đúng method này, chỉ đúng 1
        đường thắng race ghi được `pull_outcome` (R22.11).

        Side-effect:
          1. Job không tồn tại -> raise `JobNotFoundError` (R20.5).
          2. `pull_outcome` đã khác `PENDING` (đã terminal từ trước) ->
             raise `JobAlreadyResolvedError` (R20.6 — bảo vệ invariant
             "chỉ tính công đúng 1 lần", R22.4).
          3. Set `record.pull_outcome = outcome`.
          4. KHÔNG cần giải phóng slot concurrent thủ công — slot được
             TÍNH TOÁN động qua `get_worker_active_pull_count()`, không
             phải counter riêng phải decrement.
          5. Gọi `record_worker_stat_delta` (+1 success hoặc +1 fail) cho
             worker đã nhận job này (R15.3, R22.12).
          6. Persist record + broadcast SSE `job_status` (status job
             không đổi, chỉ field `pull_outcome` thay đổi).

        Args:
            job_id: ID job Pull_Mode cần chốt kết quả.
            outcome: `PullOutcome.SUCCESS` hoặc `PullOutcome.FAIL` — KHÔNG
                được truyền `PENDING` (không có ý nghĩa "chốt về pending").

        Raises:
            JobNotFoundError: `job_id` không tồn tại trong `_jobs` (R20.5).
            JobAlreadyResolvedError: job đã có `pull_outcome` terminal từ
                trước (R20.6).
            ValueError: `outcome == PullOutcome.PENDING` — không phải kết
                quả "chốt" hợp lệ (FIX 9).
        """
        # FIX 9 (LOW): `PENDING` KHÔNG phải giá trị "chốt" hợp lệ — method
        # này CHỈ dùng để chuyển từ PENDING sang terminal (SUCCESS/FAIL).
        # Nếu caller lỡ truyền PENDING, guard `pull_outcome != PENDING`
        # phía dưới sẽ cho lọt (vì đang PENDING) rồi ghi lại PENDING và
        # gọi `record_worker_stat_delta(success=0, fail=0)` — tạo 1 row
        # thống kê worker 0/0 vô nghĩa. Fail-fast ngay tại đây thay vì tạo
        # side-effect rác. Không caller hợp lệ nào truyền PENDING (mọi
        # đường resolve đều là SUCCESS/FAIL).
        if outcome == PullOutcome.PENDING:
            raise ValueError(
                "resolve_pull_outcome: outcome=PENDING không hợp lệ — chỉ "
                "chấp nhận PullOutcome.SUCCESS hoặc PullOutcome.FAIL "
                f"(job_id={job_id})"
            )

        lock = self._get_pull_job_lock(job_id)
        async with lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise JobNotFoundError(job_id)

            if record.pull_outcome != PullOutcome.PENDING:
                raise JobAlreadyResolvedError(job_id)

            record.pull_outcome = outcome
            record.updated_at = time.time()

            # FIX H2 (R22.4 — count-once qua restart): THỨ TỰ BẮT BUỘC là
            # PERSIST-BEFORE-STAT. Persist strict row `jobs` (fail-fast)
            # PHẢI chạy TRƯỚC `record_worker_stat_delta`:
            #   - Nếu persist strict hỏng → RAISE ngay tại đây, DỪNG hẳn:
            #     stat KHÔNG được tăng, row `jobs` vẫn `pull_outcome='pending'`
            #     dưới DB → sau restart job reload như CHƯA resolve và được
            #     resolve lại ĐÚNG 1 LẦN (không double-count counter worker).
            #   - Nếu persist strict thành công rồi crash NGAY TRƯỚC khi
            #     `record_worker_stat_delta` commit → row `jobs` đã terminal
            #     nên restart KHÔNG resolve lại; counter worker chỉ bị
            #     UNDER-count đúng 1 — hướng an toàn theo R15.7 (stat là
            #     best-effort, thà thiếu 1 còn hơn đếm trùng 1).
            # (Thứ tự cũ stat-before-persist gây double-count: stat đã +1
            # nhưng persist hỏng → DB vẫn 'pending' → restart resolve lại +1.)
            #
            # FIX 8 (R3.5 fail-fast bước a + c): persist `pull_outcome` dùng
            # biến thể strict — nếu DB write hỏng, RAISE (không swallow như
            # `_persist_record`) để `set_operating_mode` force-loop dừng
            # ngay và KHÔNG đổi mode (tránh trạng thái nửa-chuyển-đổi:
            # mode đã đổi nhưng outcome chưa lưu bền vững).
            await self._persist_record_strict(record)

            # Chỉ tính công nếu job thực sự có worker đã nhận (Pull_Mode
            # hợp lệ). Trường hợp `pull_assigned_telegram_user_id is None`
            # không nên xảy ra khi `pull_outcome` đã từng là PENDING (chỉ
            # `claim_pull_account` mới set PENDING, luôn kèm gán worker) —
            # guard defensive để tránh gọi `record_worker_stat_delta` với
            # `telegram_user_id=None` làm hỏng bảng thống kê. Bước này chạy
            # SAU persist strict (xem lý giải count-once FIX H2 ở trên) và
            # vẫn best-effort (R15.7) — lỗi commit stat chỉ under-count 1.
            if record.pull_assigned_telegram_user_id is not None:
                await self.record_worker_stat_delta(
                    record.pull_assigned_telegram_user_id,
                    success_delta=1 if outcome == PullOutcome.SUCCESS else 0,
                    fail_delta=1 if outcome == PullOutcome.FAIL else 0,
                    username=record.pull_assigned_username,
                    first_name=record.pull_assigned_first_name,
                )
            else:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "resolve_pull_outcome: job_id=%s không có "
                    "pull_assigned_telegram_user_id — bỏ qua record_worker_stat_delta",
                    job_id,
                )

            await self._broadcast_status(record, record.status.value)

    async def resolve_pull_error(self, job_id: str, error_code: str | None) -> None:
        """Boundary gọi từ `_run_handler` khi job có Job_Pull_Assignment
        chuyển `ERROR` (design.md §3.1, R13.1-R13.3, R13.6) — tách khỏi
        `resolve_pull_outcome` thuần vì cần thêm side-effect gửi Telegram
        (khác `resolve_pull_outcome` được gọi trực tiếp từ
        Pull_Job_Coordinator vốn đã có message riêng).

        1. Gọi `resolve_pull_outcome(job_id, PullOutcome.FAIL)` — nếu raise
           `JobAlreadyResolvedError` (job đã terminal qua đường khác race
           trước), SWALLOW tại đây (log info, KHÔNG raise tiếp — R13.1
           "chỉ thực hiện đúng 1 lần").
        2. Nếu resolve thành công, gọi `_invoke_pull_error_hooks(record,
           error_code)` — danh sách hook đăng ký qua
           `register_pull_error_hook(hook)`, mỗi hook nhận
           `(record, error_code)`, boundary swallow exception per-hook
           (R13.6 — lỗi gửi Telegram không rollback state).

        `JobNotFoundError` KHÔNG được catch ở đây — job không tồn tại ngay
        sau khi `_run_handler` vừa set ERROR là điều kiện bất thường, cho
        propagate ra để lộ rõ lỗi thay vì silent swallow.

        `_run_handler` gọi method này NGAY SAU khi ghi `record.status =
        ERROR` VÀ CHỈ khi `record.pull_assignment_state ==
        PullAssignmentState.ASSIGNED` — thay thế hoàn toàn việc gọi
        `_maybe_auto_retry` cho nhánh này (R13.5).

        Args:
            job_id: ID job Pull_Mode vừa chuyển ERROR.
            error_code: `JobResult.error_code` tương ứng (có thể `None`),
                truyền thẳng cho hook để build message báo lỗi.
        """
        try:
            await self.resolve_pull_outcome(job_id, PullOutcome.FAIL)
        except JobAlreadyResolvedError:
            import logging as _logging
            _logging.getLogger(__name__).info(
                "resolve_pull_error: job đã resolve từ trước, swallow — "
                "job_id=%s",
                job_id,
            )
            return

        record = self._jobs.get(job_id)
        if record is None:
            # Defensive: job bị xoá giữa lúc resolve_pull_outcome thành
            # công và lúc lookup lại đây (race hiếm) — không có record để
            # truyền cho hook, bỏ qua invoke.
            return
        await self._invoke_pull_error_hooks(record, error_code)

    async def initialize_pull_verify_state(
        self, job_id: str, chat_id: str, message_id: int
    ) -> None:
        """Khởi tạo Pull_Verify_State sau khi `sendPhoto` QR Pull_Mode
        thành công (design.md §3.1, R8.4) — method public MỚI bổ sung vào
        danh sách R20.1.

        Gọi bởi `PullJobNotifier.notify_pull_qr_ready` NGAY SAU khi
        `sendPhoto` trả thành công, để đặt `plus_check_state = ARMED`,
        `plus_check_attempts = 0`, `telegram_message_chat_id`,
        `telegram_message_id` trên `_JobRecord` — module Pull_Mode KHÔNG
        được mutate trực tiếp các field này (R20.2), PHẢI đi qua method
        này.

        Semantic best-effort (KHÁC `resolve_pull_outcome`/
        `resolve_pull_error` vốn Fail_Fast): no-op + log warning nếu
        `job_id` không tồn tại (cùng semantic với
        `record_telegram_notification` hiện có) — method này chỉ khởi
        tạo state hiển thị, không quyết định tính công, nên không cần
        raise khi job đã bị xoá giữa lúc gửi Telegram và lúc gọi lại đây.

        Args:
            job_id: ID job Pull_Mode vừa gửi QR thành công.
            chat_id: `chat_id` của message QR vừa gửi (=
                `record.pull_origin_chat_id` tại thời điểm gửi).
            message_id: `message_id` Telegram trả về trong response
                `sendPhoto`.
        """
        # FIX 14: mutate `plus_check_state`/`plus_check_attempts` dưới lock
        # per-job — nhất quán với `record_plus_check_transition` (cùng đọc/
        # ghi 2 field này) để 1 transition callback chồng lấp với lần khởi
        # tạo state này không đọc/ghi xen kẽ nửa vời trên cùng `job_id`.
        lock = self._get_pull_job_lock(job_id)
        async with lock:
            record = self._jobs.get(job_id)
            if record is None:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "initialize_pull_verify_state: job_id=%s không tồn tại — "
                    "no-op (best-effort, job có thể đã bị xoá)",
                    job_id,
                )
                return

            record.plus_check_state = PlusCheckState.ARMED
            record.plus_check_attempts = 0
            record.telegram_message_chat_id = chat_id
            record.telegram_message_id = message_id
            record.updated_at = time.time()

            await self._persist_record(record)
            await self._broadcast_status(record, record.status.value)

    async def _dispatch_error_outcome(
        self, record: _JobRecord, error_code: str | None
    ) -> None:
        """Định tuyến 1 job VỪA chuyển `ERROR` sang đúng luồng kết thúc
        (design.md §3.1, R13.1, R13.5).

        - Job Pull_Mode CÓ Job_Pull_Assignment (`pull_assignment_state ==
          ASSIGNED`) → THỬ `_maybe_auto_retry` TRƯỚC (requeue chính
          account đó về PENDING chạy lại theo whitelist transient +
          budget `ideal.auto_retry_blocked_max`). `_perform_auto_requeue`
          KHÔNG đụng các field `pull_*` nên job vẫn giữ nguyên assignment
          (cùng worker, cùng `pull_origin_chat_id`, `pull_outcome` vẫn
          `PENDING`) → tiếp tục đếm là "active". CHỈ khi retry KHÔNG được
          schedule (hết budget / code không thuộc whitelist / feature
          tắt) mới `resolve_pull_error` để chốt `pull_outcome = FAIL`
          (batch tracker ghi nhận 1 job lỗi qua pull-error hook).
        - Job Push_Mode (hoặc Pull_Mode còn `unassigned`, `None`) →
          `_maybe_auto_retry` theo whitelist transient hiện có, giữ 100%
          hành vi Push_Mode.

        Boundary DÙNG CHUNG cho các nhánh ERROR "sau khi handler chạy"
        trong `_run_handler` (nhánh handler-trả-ERROR và nhánh
        unexpected-exception) — tránh lặp guard `pull_assignment_state`
        ở nhiều chỗ và bảo đảm MỌI job Pull_Mode lỗi kỹ thuật đều được
        chốt FAIL (R13.1, không để `pull_outcome` kẹt `PENDING`) sau khi
        đã dùng hết lượt retry.
        """
        if record.pull_assignment_state == PullAssignmentState.ASSIGNED:
            scheduled = await self._maybe_auto_retry(record)
            if not scheduled:
                await self.resolve_pull_error(record.job.job_id, error_code)
        else:
            await self._maybe_auto_retry(record)

    async def _resolve_stopped_pull_job(self, record: _JobRecord) -> None:
        """FIX 7 (STOPPED slot leak): giải phóng slot worker khi 1 job
        Pull_Mode đang `ASSIGNED` + `pull_outcome == PENDING` kết thúc ở
        `JobStatus.STOPPED` (user stop-all / cancel-override).

        Nếu KHÔNG chốt outcome, slot concurrent của worker (tính động qua
        `get_worker_active_pull_count`, đếm job `ASSIGNED + PENDING`) bị
        kẹt vĩnh viễn — worker không nhận thêm được job dù job đã dừng.
        Chốt `pull_outcome = FAIL` nhất quán với nhánh ERROR
        (`_dispatch_error_outcome` → `resolve_pull_error`).

        Idempotent: swallow `JobAlreadyResolvedError` (job có thể đã được
        resolve qua đường khác race trước — VD force mode-switch). Job
        Push_Mode (hoặc Pull_Mode còn `unassigned`/`None`) → no-op, KHÔNG
        đụng hành vi stop hiện có.
        """
        if not (
            record.pull_assignment_state == PullAssignmentState.ASSIGNED
            and record.pull_outcome == PullOutcome.PENDING
        ):
            return
        try:
            await self.resolve_pull_outcome(record.job.job_id, PullOutcome.FAIL)
        except JobAlreadyResolvedError:
            import logging as _logging
            _logging.getLogger(__name__).info(
                "_resolve_stopped_pull_job: job đã resolve từ trước, "
                "swallow — job_id=%s",
                record.job.job_id,
            )

    # ------------------------------------------------------------------
    # Mode_Switch_Guard (design.md §3.1, Requirement 3)
    # ------------------------------------------------------------------
    async def set_operating_mode(self, new_mode: str, *, force: bool = False) -> None:
        """Mode_Switch_Guard — đường DUY NHẤT hợp lệ để đổi `telegram.mode`
        (R3). Caller PHẢI gọi method này THAY CHO
        `settings.set("telegram.mode", ...)` trực tiếp để Guard chạy.

        0. Validate `new_mode` thuộc enum `{"push", "pull"}` NGAY ĐẦU
           method (R3.1 dựa trên R1.2) — Fail_Fast trước khi làm bất kỳ
           side-effect nào (liệt kê job / force-fail), tránh trạng thái
           nửa-chuyển-đổi nếu giá trị mode tự nó không hợp lệ.
        1. Liệt kê `_JobRecord` đang `pull_assignment_state == ASSIGNED`
           VÀ `pull_outcome == PENDING`, sort theo `record.order` tăng
           dần (R3.2).
        2. Nếu danh sách không rỗng VÀ `force=False` -> raise
           `ModeSwitchBlockedError(job_ids=[...])` (R3.1).
        3. Nếu danh sách không rỗng VÀ `force=True` -> tuần tự cho MỖI
           job theo đúng thứ tự đã sort:
           a. `resolve_pull_outcome(job_id, PullOutcome.FAIL)` — KHÔNG
              catch (data-integrity: ghi pull_outcome + tăng fail_count
              + giải phóng slot concurrent qua
              `get_worker_active_pull_count` tính động). Lỗi propagate
              ngay, DỪNG xử lý, KHÔNG set mode mới (R3.5).
              (chỉ THU THẬP record vào `notified_records` trong lock).
        3c. (FIX H1 — R3.6) SAU KHI nhả `self._lock`, best-effort gọi
           `_invoke_mode_switch_notify_hooks(record)` cho mỗi record đã
           thu thập (editMessageReplyMarkup + sendMessage qua hook đăng ký
           ở `pull_mode.py`, xem §3.4). Notify là Telegram network I/O nên
           PHẢI chạy NGOÀI global lock (không đóng băng
           `claim_pull_account`/`update_max_concurrent`); R3.6 cho phép
           notify hoàn tất sau khi mode đã set và fail mà không chặn switch.
           Hook tự swallow exception nội bộ; bọc thêm try/except ở đây để
           tuyệt đối không cho lỗi từ chính cơ chế invoke làm hỏng vòng lặp.
        3b. (FIX 1 — R3.4) Nếu `new_mode == "push"`: chuyển MỌI record còn
           `pull_assignment_state == UNASSIGNED` thành job PENDING thông
           thường (`pull_assignment_state = None`, `pull_outcome = None`,
           append vào `_pending_order` theo FIFO `order`, persist, đánh
           thức scheduler) — TRƯỚC bước 4. Áp dụng bất kể `force`. KHÔNG
           chuyển khi `new_mode == "pull"`.
        4. `await self._settings.set("telegram.mode", new_mode)` — chỉ
           chạy tới đây khi bước 1-3b hoàn tất không lỗi.

        Các bước 1-4 (data-integrity + set mode) chạy dưới `self._lock`
        (FIX 6 — atomic w.r.t. `claim_pull_account`, chống TOCTOU). Bước 0
        (validate enum) chạy TRƯỚC lock. Bước 3c (notify hooks, FIX H1)
        chạy SAU khi nhả lock — network I/O không được giữ global lock.

        Args:
            new_mode: Giá trị mode mới, PHẢI là `"push"` hoặc `"pull"`.
            force: Nếu `True`, tự động kết luận THẤT BẠI cho các job đang
                dở dang rồi mới đổi mode (R3.2). Nếu `False` (default) và
                còn job dở dang, raise `ModeSwitchBlockedError`.

        Raises:
            SettingsValidationError: `new_mode` không thuộc enum
                `{"push", "pull"}` (R3.1/R1.2).
            ModeSwitchBlockedError: còn job đang assigned/pending và
                `force=False` (R3.1).
            JobNotFoundError, JobAlreadyResolvedError: propagate từ
                `resolve_pull_outcome` nếu data-integrity operation lỗi
                (R3.5) — mode KHÔNG được set trong trường hợp này.
        """
        # Enum validation TRƯỚC khi acquire lock (FIX 6) — Fail_Fast không
        # cần độc quyền JobManager, và giữ nguyên yêu cầu R3.1 "validate
        # ngay đầu method trước mọi side-effect".
        if new_mode not in ("push", "pull"):
            raise SettingsValidationError(
                "telegram.mode",
                f"invalid mode value: {new_mode!r}, expected 'push' or 'pull'",
            )

        # FIX 6 (TOCTOU): bọc TOÀN BỘ thân guard (liệt kê assigned+pending,
        # force-fail loop, chuyển unassigned→push, và set settings cuối)
        # dưới `self._lock` để atomic w.r.t. `claim_pull_account` (cũng
        # acquire `self._lock`). Nếu không, 1 worker có thể `claim` 1
        # account `unassigned` NGAY GIỮA lúc guard đã liệt kê xong danh
        # sách assigned/pending nhưng chưa set mode → account vừa claim
        # kẹt `ASSIGNED + PENDING` sau khi mode đã đổi (không bị force-fail,
        # cũng không được convert). Serialize 2 method loại bỏ cửa sổ race.
        #
        # Deadlock analysis: các method chạy DƯỚI `self._lock` ở đây —
        # `resolve_pull_outcome`, `_persist_record*`, `_broadcast_status`,
        # `record_worker_stat_delta` — đều CHỈ acquire lock per-job
        # (`_get_pull_job_lock`) hoặc KHÔNG acquire lock nào; KHÔNG method
        # nào acquire lại `self._lock` (đã đọc & xác nhận). Thứ tự lock
        # luôn là `self._lock` → per-job lock, không có đường ngược lại →
        # không có chu trình chờ.
        # FIX H1: `_invoke_mode_switch_notify_hooks` (Telegram network I/O)
        # KHÔNG còn chạy dưới `self._lock` — đã chuyển ra NGOÀI lock (bước
        # 3c) nên không giữ global lock qua các call mạng dài (~15s), gỡ
        # nguy cơ đóng băng `claim_pull_account`/`update_max_concurrent`.
        async with self._lock:
            pending_records = sorted(
                (
                    record
                    for record in self._jobs.values()
                    if record.pull_assignment_state == PullAssignmentState.ASSIGNED
                    and record.pull_outcome == PullOutcome.PENDING
                ),
                key=lambda r: r.order,
            )

            if pending_records and not force:
                raise ModeSwitchBlockedError(
                    job_ids=[record.job.job_id for record in pending_records]
                )

            # FIX H1: các record vừa force-fail cần được notify Telegram
            # (steps d/e) — nhưng notify là network I/O (~15s/call ×2/job)
            # KHÔNG được chạy dưới `self._lock` (sẽ đóng băng
            # `claim_pull_account`/`update_max_concurrent`). Chỉ THU THẬP
            # record vào đây; invoke hook SAU khi nhả lock (R3.6 cho phép
            # notify hoàn tất sau khi mode đã set). Chỉ populated ở nhánh
            # force → nhánh không-force để rỗng, vòng loop sau lock là no-op.
            notified_records: list[_JobRecord] = []

            if pending_records and force:
                for record in pending_records:
                    # (a) Data-integrity — KHÔNG catch, để lỗi propagate ngay,
                    # dừng toàn bộ, KHÔNG set mode mới (R3.5). PHẢI chạy dưới
                    # lock để atomic w.r.t. `claim_pull_account`.
                    await self.resolve_pull_outcome(
                        record.job.job_id, PullOutcome.FAIL
                    )

                    # (b) Thu thập record để notify SAU khi nhả lock (FIX H1)
                    # — KHÔNG invoke hook ở đây (tránh giữ `self._lock` qua
                    # Telegram network I/O). Notify là best-effort (R3.6).
                    notified_records.append(record)

            # FIX 1 (R3.4): khi chuyển sang Push_Mode, các account còn
            # `unassigned` (chưa từng được gán cho worker) PHẢI được
            # chuyển thành job PENDING thông thường để scheduler tự chạy
            # theo hành vi Push_Mode mặc định — nếu không, chúng kẹt vĩnh
            # viễn trong `_jobs` mà không bao giờ vào `_pending_order`.
            # Áp dụng BẤT KỂ `force` (R3.4 độc lập với force). KHÔNG áp
            # dụng khi chuyển sang Pull_Mode (giữ nguyên pool unassigned).
            if new_mode == "push":
                unassigned_records = sorted(
                    (
                        record
                        for record in self._jobs.values()
                        if record.pull_assignment_state
                        == PullAssignmentState.UNASSIGNED
                    ),
                    key=lambda r: r.order,
                )
                for record in unassigned_records:
                    # Gỡ hoàn toàn dấu vết Pull_Mode → job trở thành job
                    # Push_Mode thuần (không có Job_Pull_Assignment nên khi
                    # QR_READY sẽ được Push_Mode_Notifier broadcast bình
                    # thường — R3.4).
                    record.pull_assignment_state = None
                    record.pull_outcome = None
                    record.updated_at = time.time()
                    self._pending_order.append(record.job.job_id)
                    await self._persist_record(record)
                if unassigned_records:
                    # Đánh thức scheduler để nhận các job vừa convert (cùng
                    # cơ chế wake đã dùng ở `submit_batch`/`claim_pull_account`).
                    self._ensure_scheduler_running()
                    self._new_job_event.set()

            await self._settings.set("telegram.mode", new_mode)

        # FIX H1: notify hooks chạy NGOÀI `self._lock` (đã nhả ở dòng
        # trên) — steps d/e (editMessageReplyMarkup + sendMessage) là
        # Telegram network I/O, R3.6 cho phép hoàn tất SAU khi mode đã set
        # và được phép fail mà không chặn việc đổi mode. Đặt ngoài lock để
        # gỡ network I/O khỏi global lock (không còn đóng băng
        # `claim_pull_account`/`update_max_concurrent`). Data-integrity
        # (steps a/b/c) đã xong trong lock trước khi tới đây. Vòng loop này
        # no-op khi không force (`notified_records` rỗng).
        for record in notified_records:
            try:
                await self._invoke_mode_switch_notify_hooks(record)
            except Exception:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "set_operating_mode: mode-switch-notify hook "
                    "invocation raised unexpectedly, job_id=%s",
                    record.job.job_id,
                    exc_info=True,
                )

    # Whitelist cố định cho `record_plus_check_transition` (design.md §3.1,
    # spec `telegram-callback-verify-plus` R9.2-R9.4). Key là cặp
    # `(from_state, to_state)` (giá trị string của `PlusCheckState`); value
    # là điều kiện `attempts` bắt buộc để transition hợp lệ — `None` nghĩa
    # là transition KHÔNG bị gate bởi `attempts` (luôn hợp lệ nếu state
    # khớp), số nguyên nghĩa là transition CHỈ hợp lệ khi
    # `plus_check_attempts` hiện tại (VÀ giá trị `attempts` caller truyền
    # vào) đúng bằng số đó.
    _PLUS_CHECK_TRANSITION_WHITELIST: dict[tuple[str, str], "int | None"] = {
        ("armed", "checking"): None,
        ("checking", "verified"): None,
        ("checking", "armed"): 1,
        ("checking", "exhausted_2"): 2,
        ("armed", "failed_marked"): None,
        ("checking", "failed_marked"): None,
    }

    async def record_plus_check_transition(
        self, job_id: str, from_state: str, to_state: str, attempts: int
    ) -> bool:
        """Transition `plus_check_state`/`plus_check_attempts` dưới lock
        per-job, theo whitelist cố định (design.md §3.1, spec
        `telegram-callback-verify-plus` R9.1-R9.4) — tái dùng cho CẢ job
        Push_Mode (spec kia) và Pull_Mode (spec này, R10/R11 do
        Pull_Job_Coordinator chủ động gọi SAU khi transition thành công,
        KHÔNG nhúng ở đây để giữ method này generic).

        Whitelist (`_PLUS_CHECK_TRANSITION_WHITELIST`): `armed->checking`,
        `checking->verified`, `checking->armed` (chỉ khi `attempts==1`),
        `checking->exhausted_2` (chỉ khi `attempts==2`), `armed->failed_marked`,
        `checking->failed_marked`.

        Ngữ nghĩa tham số `attempts` (điểm cần làm rõ vì design.md không nêu
        tường minh — quyết định implement tại đây, ghi lại cho task 27 —
        Pull_Job_Coordinator — biết chính xác giá trị cần truyền):

        - Với 2 transition có điều kiện gate trong whitelist
          (`checking->armed` cần `attempts==1`, `checking->exhausted_2` cần
          `attempts==2`), tham số `attempts` là giá trị caller TỰ ĐỌC được
          từ `plus_check_attempts` hiện tại của job (VD qua `get_job(job_id)`)
          TRƯỚC khi gọi method này. Method validate CẢ 2 điều kiện: (a)
          `attempts` phải đúng bằng hằng số whitelist yêu cầu (1 hoặc 2),
          VÀ (b) `record.plus_check_attempts` (giá trị THẬT dưới lock) phải
          KHỚP với `attempts` caller truyền — đây là compare-and-swap style
          check chống race (R22.9, Property 9): nếu 1 request khác đã lướt
          qua trước và đổi `plus_check_attempts`/`plus_check_state` trong
          lúc caller đọc xong nhưng chưa kịp giành lock, request sau sẽ
          thấy giá trị không khớp và bị từ chối (trả `False`) thay vì áp
          dụng nhầm side-effect. `plus_check_attempts` KHÔNG bị ghi đè bởi
          `attempts` param ở 2 transition này — nó đã đúng giá trị đó (mới
          được validate khớp), giữ nguyên.
        - Với `armed->checking`, whitelist KHÔNG gate theo `attempts` (giá
          trị `attempts` truyền vào bị NGƠ LƠ khi validate) — nhưng theo
          R9 spec kia (Callback_Handler R2: "tăng plus_check_attempts lên 1
          đơn vị NGAY TRƯỚC KHI gọi check_plan_status"), side-effect của
          CHÍNH transition này là tăng `record.plus_check_attempts` lên
          đúng 1 (so với giá trị hiện tại, KHÔNG gán trực tiếp bằng tham số
          `attempts`). Caller nên truyền giá trị `attempts` hiện tại (VD 0)
          cho nhất quán logging, nhưng method không dùng nó để gate hay set.
        - Với `checking->verified`, `armed->failed_marked`,
          `checking->failed_marked`: không có điều kiện `attempts` trong
          whitelist — `attempts` truyền vào bị bỏ qua hoàn toàn, VÀ
          `record.plus_check_attempts` được GIỮ NGUYÊN không đổi (đạt
          `verified`/`failed_marked` không cần reset hay set lại counter,
          design.md không có yêu cầu nào khác).

        Chạy dưới `self._get_pull_job_lock(job_id)` — cùng lock per-job
        dùng cho `resolve_pull_outcome`, đảm bảo 2 callback chồng lấp cho
        cùng `job_id` chỉ có tối đa 1 thắng race (R22.9, Property 9).

        Args:
            job_id: ID job cần transition.
            from_state: giá trị string của `PlusCheckState` mà caller kỳ
                vọng là state HIỆN TẠI (bị validate khớp với
                `record.plus_check_state.value`).
            to_state: giá trị string của `PlusCheckState` muốn chuyển tới.
            attempts: xem giải thích ngữ nghĩa ở trên — tuỳ transition mà
                được dùng để gate (CAS check) hoặc bị bỏ qua hoàn toàn.

        Returns:
            `True` nếu transition hợp lệ và đã áp dụng (đã set state, cập
            nhật attempts theo đúng ngữ nghĩa, persist, broadcast); `False`
            nếu job không tồn tại, `from_state` không khớp state hiện tại,
            hoặc `(from_state, to_state[, attempts])` không thuộc whitelist
            — mọi trường hợp `False` đều CHỈ log warning, KHÔNG raise, KHÔNG
            mutate state.
        """
        import logging as _logging

        logger = _logging.getLogger(__name__)
        lock = self._get_pull_job_lock(job_id)
        async with lock:
            record = self._jobs.get(job_id)
            if record is None:
                logger.warning(
                    "record_plus_check_transition: job_id=%s không tồn tại",
                    job_id,
                )
                return False

            if record.plus_check_state.value != from_state:
                logger.warning(
                    "record_plus_check_transition: state mismatch job_id=%s "
                    "expected from_state=%s but current=%s",
                    job_id,
                    from_state,
                    record.plus_check_state.value,
                )
                return False

            key = (from_state, to_state)
            if key not in self._PLUS_CHECK_TRANSITION_WHITELIST:
                logger.warning(
                    "record_plus_check_transition: transition không thuộc "
                    "whitelist job_id=%s from_state=%s to_state=%s attempts=%s",
                    job_id,
                    from_state,
                    to_state,
                    attempts,
                )
                return False

            required_attempts = self._PLUS_CHECK_TRANSITION_WHITELIST[key]
            if required_attempts is not None and (
                attempts != required_attempts
                or record.plus_check_attempts != required_attempts
            ):
                logger.warning(
                    "record_plus_check_transition: attempts mismatch "
                    "job_id=%s from_state=%s to_state=%s attempts_param=%s "
                    "current_attempts=%s required=%s",
                    job_id,
                    from_state,
                    to_state,
                    attempts,
                    record.plus_check_attempts,
                    required_attempts,
                )
                return False

            record.plus_check_state = PlusCheckState(to_state)
            if key == ("armed", "checking"):
                record.plus_check_attempts += 1
            # Các transition khác giữ nguyên `plus_check_attempts` — 2
            # transition có gate đã được validate khớp giá trị whitelist
            # yêu cầu ở trên (không cần ghi lại), 3 transition không gate
            # (`checking->verified`, `armed->failed_marked`,
            # `checking->failed_marked`) không có yêu cầu nào về counter.
            record.updated_at = time.time()

            await self._persist_record(record)
            await self._broadcast_status(record, record.status.value)
            return True

    async def record_worker_stat_delta(
        self,
        telegram_user_id: str,
        *,
        success_delta: int = 0,
        fail_delta: int = 0,
        username: str | None = None,
        first_name: str | None = None,
    ) -> None:
        """Cộng dồn atomic `success_count`/`fail_count` cho 1 Telegram_Worker
        (design.md §3.1, R15.3, R15.4, R22.12).

        Ủy quyền hoàn toàn cho `JobRepository.upsert_worker_stat_delta` —
        upsert atomic `INSERT ... ON CONFLICT DO UPDATE SET success_count =
        success_count + ?` ở tầng SQL, KHÔNG đọc-sửa-ghi ở đây (tránh race
        condition khi nhiều worker resolve outcome cùng lúc). `username`/
        `first_name` chỉ ghi đè khi khác `None` — quyết định đó nằm ở
        repository (COALESCE), method này chỉ forward nguyên giá trị.

        Fail-safe (R15.7): giống pattern `_persist_record` — no-op nếu
        `_job_repo` chưa inject (test unit không cần DB); lỗi DB chỉ log
        warning, KHÔNG raise ra ngoài (1 worker fail ghi điểm không được
        phép làm hỏng flow `resolve_pull_outcome` đang gọi method này).

        Args:
            telegram_user_id: định danh worker cần cộng điểm.
            success_delta/fail_delta: giá trị CỘNG THÊM (thường `0` hoặc
                `1`), KHÔNG phải giá trị tuyệt đối.
            username/first_name: snapshot định danh worker tại thời điểm
                gọi — `None` nghĩa là "giữ giá trị cũ trong DB".
        """
        if self._job_repo is None:
            return
        try:
            await self._job_repo.upsert_worker_stat_delta(
                telegram_user_id,
                success_delta=success_delta,
                fail_delta=fail_delta,
                username=username,
                first_name=first_name,
            )
        except Exception:
            import logging as _logging
            _logging.getLogger(__name__).exception(
                "record_worker_stat_delta failed: telegram_user_id=%s",
                telegram_user_id,
            )

    async def get_worker_stats_ranking(self) -> list[dict[str, Any]]:
        """Trả bảng xếp hạng công Telegram_Worker (design.md §3.1, R16.1,
        R20.7).

        Query trực tiếp `telegram_worker_stats` qua `JobRepository.list_worker_stats`
        — repository ĐÃ sort sẵn theo `success_count DESC, fail_count ASC,
        telegram_user_id ASC`, method này KHÔNG re-sort, chỉ convert
        `WorkerStatRow` → dict shape `{telegram_user_id, username,
        first_name, success_count, fail_count}` cho `commands.py` dùng
        (bỏ `updated_at` — không nằm trong shape §3.1).

        Trả `[]` nếu `_job_repo` chưa inject (test unit không có DB —
        sensible default khi không có tầng persistence).
        """
        if self._job_repo is None:
            return []
        rows = await self._job_repo.list_worker_stats()
        return [
            {
                "telegram_user_id": row.telegram_user_id,
                "username": row.username,
                "first_name": row.first_name,
                "success_count": row.success_count,
                "fail_count": row.fail_count,
            }
            for row in rows
        ]

    async def reset_all_worker_stats(self) -> None:
        """Reset `success_count`/`fail_count` về 0 cho TOÀN BỘ worker
        (design.md §3.1, R17.2) — ủy quyền cho `JobRepository.reset_worker_stats`.

        No-op nếu `_job_repo` chưa inject. KHÔNG bọc try/except: đây là
        hành động do user chủ động khởi tạo (bấm nút xác nhận `/reset`)
        — caller (Pull_Mode command handler, task sau) cần biết ngay nếu
        DB write thất bại để báo lỗi cho user, thay vì âm thầm no-op như
        `record_worker_stat_delta` (side-effect nền, không do user trực
        tiếp chờ kết quả).
        """
        if self._job_repo is None:
            return
        await self._job_repo.reset_worker_stats()

    async def claim_and_count_plan_outcome(
        self,
        chat_id: str,
        job_id: str,
        *,
        plus_delta: int = 0,
        expired_delta: int = 0,
    ) -> tuple[bool, tuple[int, int] | None]:
        """Atomically claim a plan outcome once and bump the chat tally.

        Delegates to `JobRepository.try_claim_and_increment_tally`. Returns
        `(True, (plus_count, expired_count))` when this call wins the claim
        (for the `#N` tag), else `(False, None)`.

        Fail-safe like `record_worker_stat_delta`: missing `_job_repo` or a
        DB error yields `(False, None)` and logs — nothing is committed, so
        a later retry can re-attempt. Bare unit constructions without a repo
        therefore skip counting rather than inventing in-memory tallies.
        """
        if self._job_repo is None:
            return (False, None)
        try:
            return await self._job_repo.try_claim_and_increment_tally(
                chat_id,
                job_id,
                plus_delta=plus_delta,
                expired_delta=expired_delta,
            )
        except Exception:
            import logging as _logging
            _logging.getLogger(__name__).exception(
                "claim_and_count_plan_outcome failed: chat_id=%s job_id=%s",
                chat_id,
                job_id,
            )
            return (False, None)

    async def read_batch_tally(
        self, chat_id: str
    ) -> tuple[int, int, int | None]:
        """Return `(plus_count, expired_count, tally_message_id)` for a chat.

        Missing repo or missing chat → `(0, 0, None)`. Background read is
        fail-safe (log-not-raise) so a transient DB fault does not break
        the notifier path.
        """
        if self._job_repo is None:
            return (0, 0, None)
        try:
            return await self._job_repo.read_batch_tally(chat_id)
        except Exception:
            import logging as _logging
            _logging.getLogger(__name__).exception(
                "read_batch_tally failed: chat_id=%s",
                chat_id,
            )
            return (0, 0, None)

    async def persist_tally_message_id(
        self,
        chat_id: str,
        message_id: int,
        *,
        durable: bool = False,
    ) -> None:
        """Persist the live tally Telegram message id for a chat.

        Default is fail-safe (log-not-raise), matching background counter
        writes. When `durable=True` (first-send id persist) DB errors
        surface so the caller can avoid orphaning a tally message that
        Telegram already accepted.
        """
        if self._job_repo is None:
            return
        try:
            await self._job_repo.set_batch_tally_message_id(chat_id, message_id)
        except Exception:
            if durable:
                raise
            import logging as _logging
            _logging.getLogger(__name__).exception(
                "persist_tally_message_id failed: chat_id=%s message_id=%s",
                chat_id,
                message_id,
            )

    async def list_batch_tally_chats(self) -> list["BatchTallyRow"]:
        """Return every batch-tally row (period-close loop input).

        `[]` when `_job_repo` is missing. Fail-safe on DB error so a
        background list cannot crash the close command prematurely.
        """
        if self._job_repo is None:
            return []
        try:
            return await self._job_repo.list_batch_tally()
        except Exception:
            import logging as _logging
            _logging.getLogger(__name__).exception(
                "list_batch_tally_chats failed"
            )
            return []

    async def close_batch_period(
        self,
        chat_id: str,
        *,
        plus_receipted: int,
        expired_receipted: int,
    ) -> None:
        """Decrement one chat by receipted amounts and clear message id.

        User-initiated close primitive — surfaces DB errors (like
        `reset_all_worker_stats`) so the caller can report failure.
        No-op when `_job_repo` is missing.
        """
        if self._job_repo is None:
            return
        await self._job_repo.close_batch_period(
            chat_id,
            plus_receipted=plus_receipted,
            expired_receipted=expired_receipted,
        )

    async def rearm_deferred_plan_outcomes(self) -> None:
        """Re-arm deferred plan outcomes (`2`→`0`) for the next period.

        User-initiated close primitive — surfaces DB errors. No-op when
        `_job_repo` is missing.
        """
        if self._job_repo is None:
            return
        await self._job_repo.rearm_deferred_plan_outcomes()

    # ------------------------------------------------------------------
    # Introspection cho API layer (task 23.1)
    # ------------------------------------------------------------------
    def get_job(self, job_id: str) -> _JobRecord | None:
        """Trả record cho `job_id`, `None` nếu không tồn tại. API layer
        (task 23.1) dùng để build response `GET /api/jobs/{id}`."""
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[_JobRecord]:
        """Trả toàn bộ record theo thứ tự tạo. API layer (task 23.1) dùng để
        build response `GET /api/jobs` (compact list)."""
        # Trả copy để caller không mutate được internal state.
        return list(self._jobs.values())

    @property
    def current_max_concurrent(self) -> int:
        """Aggregate observability: max `configured_max` across all methods.

        Concurrency is now per payment method, so no single number is the
        "current" limit. This aggregate exists only for coarse debug/telemetry.
        To observe the limit a specific method is running under — and to assert
        on it in tests — use `get_method_max_concurrent(payment_method)`.
        """
        if not self._method_limiters:
            return 1
        return max(lim.configured_max for lim in self._method_limiters.values())

    def get_method_max_concurrent(self, payment_method: str) -> int:
        """Configured concurrency limit for one payment method.

        Returns `0` if the method has no limiter yet (never registered /
        settings not applied). This is the per-method replacement for the
        former single-limit `current_max_concurrent` observability.
        """
        limiter = self._method_limiters.get(payment_method)
        return limiter.configured_max if limiter is not None else 0

    # ------------------------------------------------------------------
    # Persistence (Job Repository) — nhớ job qua restart Backend_Service
    # ------------------------------------------------------------------
    async def _broadcast_status(
        self,
        record: _JobRecord,
        status_value: str,
        **extra: Any,
    ) -> None:
        """Wrapper cho `sse.broadcast_job_status` — luôn kèm `updated_at` +
        `order` từ record.

        Fix bug: `SseBroadcaster.broadcast_job_status` mặc định chỉ đưa
        `{job_id, status}` + kwargs → nếu caller quên gắn `updated_at`,
        FE fallback về `Date.now()/1000` (chỉ gần đúng, sai lệch với
        backend time thực tế). Wrapper này đảm bảo mọi SSE `job_status`
        đều mang đúng `updated_at` server-side.

        Caller vẫn có thể override qua `**extra` (VD test) — priority:
        `extra` > default.

        Guard chống broadcast SSE cho job đã bị pop khỏi `self._jobs`
        (delete_job / cleanup chạy song song với `_run_handler` /
        `_perform_auto_requeue`). Nếu vẫn broadcast:
        `useJobsStore.applyStatusEvent` sẽ TỰ TẠO entry ghost (fallback
        "if !existing → create new"), user click row → `loadDetail(id)`
        → 404 `job_not_found` → toast phiền. Silent no-op = fix triệt để.
        """
        if record.job.job_id not in self._jobs:
            return
        payload: dict[str, Any] = {
            "updated_at": record.updated_at,
            "order": record.order,
            # `retry_count`: FE render badge "R{n}/{max}" ngay từ SSE
            # event đầu tiên, không phải chờ `loadAll` refresh list.
            "retry_count": record.retry_count,
            # Mốc thời gian chạy — FE dùng cho cột elapsed:
            #   running → tick từ started_at
            #   terminal → đóng băng finished_at - started_at
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            # `held`: FE hiển thị badge "Held" + nút Start ▶ khi
            # pending+held=True. Gửi kể cả khi False để FE có thể merge
            # patch ngay lúc scheduler chuyển sang RUNNING (clear held).
            "held": record.held,
            # `plan`: kết quả check-plan đã persist (yêu cầu 2026-07).
            # Gửi kèm mọi status event để FE reactive giữ đúng
            # SuccessOutputPanel + badge PLUS/FREE mà không cần
            # `loadAll` refresh. `None` = chưa check → FE fallback
            # badge "?".
            "plan": record.plan,
        }
        payload.update(extra)
        await self._sse.broadcast_job_status(record.job.job_id, status_value, **payload)

    async def _persist_record(self, record: _JobRecord) -> None:
        """Upsert record vào DB. No-op nếu `_job_repo` chưa inject (test unit).

        Fail-safe: nếu DB write raise, log-and-continue → không phá scheduler
        vì lỗi persist. Ưu tiên semantic "job vẫn chạy được" hơn "state
        được lưu chính xác" — user có thể mất state khi restart nếu DB fail,
        nhưng flow hiện tại không bị gián đoạn.
        """
        if self._job_repo is None:
            return
        # Guard shutdown: nếu process đang shutdown, connection SQLite có
        # thể đã (hoặc sắp) bị `db_engine.close()`. Bất kỳ handler_task /
        # delayed_requeue_task nào chưa kịp cancel & await sẽ hit
        # `sqlite3.ProgrammingError: Cannot operate on a closed database`.
        # Skip persist trong shutdown window — job vẫn chạy đúng logic
        # trong memory nhưng KHÔNG cố ghi DB. `JobManager.shutdown()` sẽ
        # cancel/await những task này ngay sau; guard này chỉ là lưới an
        # toàn cho race window ngắn giữa set flag và await gather.
        if self._shutting_down:
            return
        # Guard: record đã bị pop khỏi map (delete_job / cleanup) mà
        # handler_task / delayed_requeue_task vẫn cố persist → upsert
        # sẽ tái tạo row zombie trong DB, restart backend sẽ load lại
        # job "đã xóa". Skip idempotent để `delete_job` là source of truth.
        if record.job.job_id not in self._jobs:
            return
        try:
            await self._job_repo.upsert(self._build_job_row(record))
        except Exception:
            import logging as _logging
            _logging.getLogger(__name__).exception(
                "persist job failed: job_id=%s", record.job.job_id
            )

    async def _persist_record_strict(self, record: _JobRecord) -> None:
        """Biến thể FAIL-FAST của `_persist_record` (FIX 8 — R3.5 bước a).

        Giống `_persist_record` NHƯNG KHÔNG swallow exception khi DB write
        lỗi — để lỗi propagate ra caller. Dùng cho các đường ghi state
        mang tính data-integrity mà mode/flow phụ thuộc vào việc lưu bền
        vững thành công (VD `resolve_pull_outcome` ghi `pull_outcome`: nếu
        không lưu được, `set_operating_mode(force=True)` PHẢI dừng và KHÔNG
        đổi mode — R3.5).

        Giữ nguyên 2 điều kiện no-op an toàn của `_persist_record`:
          - `_job_repo` chưa inject (test unit tạo JobManager 3-arg) → no-op,
            KHÔNG raise (không có tầng persistence để fail-fast).
          - record đã bị pop khỏi `self._jobs` (đã delete) → no-op idempotent.

        Chỉ KHÁC ở chỗ: khi thực sự gọi `upsert` và DB raise, lỗi được ném
        tiếp thay vì log-and-continue.
        """
        if self._job_repo is None:
            return
        if record.job.job_id not in self._jobs:
            return
        await self._job_repo.upsert(self._build_job_row(record))

    def _build_job_row(self, record: _JobRecord) -> "JobRow":
        """Dựng `JobRow` DTO từ 1 `_JobRecord` để persist.

        Tách khỏi `_persist_record` để dùng chung với biến thể fail-fast
        `_persist_record_strict` (FIX 8) — tránh lặp toàn bộ mapping field
        ở 2 chỗ (DRY).
        """
        from app.core.job_repo import JobRow

        return JobRow(
            job_id=record.job.job_id,
            payment_method=record.job.payment_method,
            account_line=record.job.account_line,
            status=record.status.value,
            order_num=record.order,
            created_at=record.job.created_at,
            updated_at=record.updated_at,
            dedup_key=record.dedup_key,
            artifact_path=record.artifact_path,
            error_code=record.error_code,
            error_message=record.error_message,
            payment_link=record.payment_link,
            settings_snapshot=record.settings_snapshot,
            retry_count=record.retry_count,
            started_at=record.started_at,
            finished_at=record.finished_at,
            telegram_notifications=list(record.telegram_notifications),
            # Pull_Mode + Plus_Verify (design.md §3.2, R21.1, R21.2,
            # R4.4-4.6, R7.1-7.3): `JobRow` chỉ nhận `str | None` thô
            # (Payment_Module_Boundary — DTO thuần) nên convert enum
            # qua `.value`. `pull_assignment_state`/`pull_outcome`
            # giữ `None` nếu record `None` (job Push_Mode) — KHÔNG
            # ép về giá trị mặc định của enum.
            pull_assignment_state=(
                record.pull_assignment_state.value
                if record.pull_assignment_state
                else None
            ),
            pull_assigned_telegram_user_id=record.pull_assigned_telegram_user_id,
            pull_origin_chat_id=record.pull_origin_chat_id,
            pull_outcome=(
                record.pull_outcome.value if record.pull_outcome else None
            ),
            pull_assigned_username=record.pull_assigned_username,
            pull_assigned_first_name=record.pull_assigned_first_name,
            plus_check_state=record.plus_check_state.value,
            plus_check_attempts=record.plus_check_attempts,
            telegram_message_chat_id=record.telegram_message_chat_id,
            telegram_message_id=record.telegram_message_id,
            held=record.held,
            plan=record.plan,
        )

    async def _delete_persisted(self, job_id: str) -> None:
        """Xóa row DB cho job đã bị `delete_job`. No-op nếu chưa inject repo."""
        if self._job_repo is None:
            return
        # Guard shutdown: xem docstring `_persist_record`.
        if self._shutting_down:
            return
        try:
            await self._job_repo.delete(job_id)
        except Exception:
            import logging as _logging
            _logging.getLogger(__name__).exception(
                "delete job persist failed: job_id=%s", job_id
            )

    async def load_from_db(self) -> None:
        """Rebuild in-memory state từ DB tại startup (Requirement: nhớ qua restart).

        Được `bootstrap_services()` gọi 1 lần SAU khi `register_ideal_handler`
        (cần handler để đảm bảo mọi `payment_method` có dispatcher) và
        TRƯỚC khi Backend nhận request.

        Xử lý status khi load:
        - `RUNNING` → chuyển thẳng về `PENDING`. Job đang chạy lúc process
          bị kill không thể resume proxy lease / network state → cách an
          toàn là restart flow từ đầu. Cancel_token đã cancel (record cũ)
          không được persist → job mới có token mới, không bị treat như
          đã cancel.
        - `PENDING`, `QR_READY`, `ERROR`, `STOPPED` → giữ nguyên.

        `_pending_order` được rebuild theo `order_num ASC` cho các job
        cần schedule (PENDING sau khi normalize).
        `_dedup_index` rebuild theo mọi row có `dedup_key`.
        `_order_counter` reset về `MAX(order_num)` từ DB — job mới sau
        đó nhận order = max + 1, không đụng job cũ.

        Idempotent: gọi lại (test/reload) sẽ overwrite in-memory state
        bằng snapshot DB hiện tại.
        """
        if self._job_repo is None:
            return

        rows = await self._job_repo.list_all()
        self._jobs = {}
        self._pending_order = []
        self._dedup_index = {}
        max_order = 0

        for row in rows:
            # Normalize RUNNING → PENDING (không thể resume flow qua restart).
            status_str = row.status
            was_running = status_str == JobStatus.RUNNING.value
            if was_running:
                status_str = JobStatus.PENDING.value
            try:
                status = JobStatus(status_str)
            except ValueError:
                # Status không hợp lệ (DB được sửa thô) — skip row.
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "skip job with invalid status: job_id=%s status=%s",
                    row.job_id,
                    row.status,
                )
                continue

            token = SimpleCancellationToken()
            job = Job(
                job_id=row.job_id,
                payment_method=row.payment_method,
                account_line=row.account_line,
                created_at=row.created_at,
                cancellation_token=token,
            )
            # Job đang RUNNING lúc shutdown → normalize sang PENDING và
            # RESET timing (started_at/finished_at) — flow sẽ chạy lại từ
            # đầu, không nên giữ started_at cũ vì FE sẽ đếm elapsed sai
            # (bao gồm cả downtime của backend).
            started_at = None if was_running else row.started_at
            finished_at = None if was_running else row.finished_at

            # FIX M3: convert 3 enum Pull_Mode/Plus_Verify CÓ GUARD trước
            # khi dựng `_JobRecord`. Trước đây các conversion này nằm INLINE
            # trong `_JobRecord(...)` không bọc try/except — 1 string
            # NON-EMPTY không hợp lệ (DB bị sửa thô / migration lỗi) raise
            # ValueError propagate ra và LÀM SẬP TOÀN BỘ startup. Nay skip
            # đúng row hỏng (log warning + `continue`) — nhất quán với cách
            # xử lý status không hợp lệ ở trên.
            # Giữ nguyên semantics happy-path: `pull_assignment_state`/
            # `pull_outcome` = None khi giá trị row falsy (None/"" → job
            # Push_Mode, KHÔNG ép về default enum); `plus_check_state` LUÔN
            # convert (DB default 'armed'). CHỈ string NON-EMPTY không hợp
            # lệ mới trigger skip.
            try:
                pull_assignment_state = (
                    PullAssignmentState(row.pull_assignment_state)
                    if row.pull_assignment_state
                    else None
                )
                pull_outcome = (
                    PullOutcome(row.pull_outcome) if row.pull_outcome else None
                )
                plus_check_state = PlusCheckState(row.plus_check_state)
            except ValueError as exc:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "skip job with invalid pull/plus enum: job_id=%s "
                    "pull_assignment_state=%r pull_outcome=%r "
                    "plus_check_state=%r (%s)",
                    row.job_id,
                    row.pull_assignment_state,
                    row.pull_outcome,
                    row.plus_check_state,
                    exc,
                )
                continue

            record = _JobRecord(
                job=job,
                status=status,
                # KHÔNG persist logs — mất log qua restart, chấp nhận.
                # Dùng factory deque để record có buffer log bounded ngay
                # sau khi load, giống job mới tạo qua submit_batch.
                logs=_new_logs_deque(),
                lease=None,
                settings_snapshot=dict(row.settings_snapshot),
                updated_at=row.updated_at,
                handler_task=None,
                artifact_path=row.artifact_path,
                error_code=row.error_code,
                error_message=row.error_message,
                payment_link=row.payment_link,
                dedup_key=row.dedup_key,
                order=row.order_num,
                retry_count=row.retry_count,
                started_at=started_at,
                finished_at=finished_at,
                # Copy list để record memory không share reference với JobRow
                # (JobRow là dataclass mutable — nếu tương lai code chạm
                # `row.telegram_notifications.append(...)` sẽ vô tình cũng
                # mutate record).
                telegram_notifications=list(row.telegram_notifications),
                # Pull_Mode + Plus_Verify (design.md §3.2, R21.2): `JobRow`
                # là DTO thuần (Payment_Module_Boundary) — các cột
                # pull_*/plus_check_state là `str | None` thô, cần convert
                # sang enum tương ứng của `_JobRecord`. `pull_assignment_state`/
                # `pull_outcome` giữ `None` nếu row `None` (job Push_Mode —
                # KHÔNG ép về giá trị mặc định của enum). `plus_check_state`
                # LUÔN có giá trị (DB default `"armed"`), convert trực tiếp.
                pull_assignment_state=pull_assignment_state,
                pull_assigned_telegram_user_id=row.pull_assigned_telegram_user_id,
                pull_origin_chat_id=row.pull_origin_chat_id,
                pull_assigned_username=row.pull_assigned_username,
                pull_assigned_first_name=row.pull_assigned_first_name,
                pull_outcome=pull_outcome,
                plus_check_state=plus_check_state,
                plus_check_attempts=row.plus_check_attempts,
                telegram_message_chat_id=row.telegram_message_chat_id,
                telegram_message_id=row.telegram_message_id,
                # `held`: nhớ qua restart. Job Add nhưng chưa Start giữ
                # nguyên `held=True` sau restart → scheduler vẫn bỏ qua
                # (đường if dưới đây skip append `_pending_order`). Nếu
                # job từng RUNNING lúc shutdown thì đã bị normalize sang
                # PENDING nhưng `held` giữ nguyên giá trị persisted —
                # tuy nhiên khi transition RUNNING scheduler đã clear
                # `held=False` (xem `_run_handler`), nên restart resume
                # bình thường không kẹt held.
                held=row.held,
                # `plan`: nhớ qua restart để Successful accounts luôn
                # hiện khi reload trang. Job đang RUNNING lúc shutdown
                # đã normalize PENDING ở trên — plan cũ vẫn giữ (không
                # reset ở đây) vì tài khoản Plus vẫn là Plus dù flow
                # chạy lại; nếu user muốn re-verify thì bấm Check Plus
                # per-row.
                plan=row.plan,
            )
            self._jobs[row.job_id] = record
            if row.dedup_key:
                self._dedup_index[row.dedup_key] = row.job_id
            # Pull_Mode (R4.5, R4.6, R7.1-7.3): job PENDING còn
            # `pull_assignment_state == UNASSIGNED` PHẢI tiếp tục nằm
            # ngoài `_pending_order` sau restart — vẫn ở Pull_Account_Pool
            # chờ `claim_pull_account`, KHÔNG được scheduler nhận nhầm.
            # Job `assigned` (đang chạy dở khi shutdown, normalize
            # RUNNING→PENDING ở trên) VẪN phải vào `_pending_order` để
            # scheduler resume — giữ đúng hành vi hiện có.
            #
            # Held (feature Add/Run tách bạch): job PENDING + held=True
            # đã tạo qua "+ Add" nhưng chưa user bấm Start → KHÔNG vào
            # queue sau restart, chờ Start_Job_API. Guard này AND với 2
            # điều kiện Pull_Mode ở trên (Push held = skip; Pull held =
            # cũng skip, dù thực tế Pull không dùng held vì auto-claim
            # từ pool đã tách bạch job và worker).
            if (
                status == JobStatus.PENDING
                and record.pull_assignment_state != PullAssignmentState.UNASSIGNED
                and not record.held
            ):
                self._pending_order.append(row.job_id)
            if row.order_num > max_order:
                max_order = row.order_num

            # Nếu status vừa normalize (RUNNING → PENDING) thì persist lại
            # để DB đồng nhất với memory. Không await song song để đảm
            # bảo thứ tự write; overhead nhỏ (chỉ áp dụng cho job đang
            # RUNNING lúc shutdown, thường ít).
            if row.status != status.value:
                await self._persist_record(record)

        self._order_counter = max_order

        # Nếu có job PENDING sau load → kick scheduler.
        if self._pending_order:
            self._ensure_scheduler_running()
            self._new_job_event.set()

    # ==================================================================
    # Internal helpers — scheduler loop, handler wrapper, log emitter
    # ==================================================================
    def _ensure_scheduler_running(self) -> None:
        """Spawn scheduler + cleanup task lần đầu (idempotent).

        Cả 2 task đều lazy start — chỉ spawn khi thực sự có job (qua
        `submit_batch`) hoặc có job load từ DB (qua `load_from_db`).
        Test unit không gọi 2 method trên sẽ KHÔNG có task nền → giữ
        được test đơn giản.
        """
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self) -> None:
        """Vòng lặp dọn job terminal cũ định kỳ.

        Chạy mỗi `_JOB_CLEANUP_INTERVAL_SECONDS` giây. Fail_Fast (R14.2):
        exception trong 1 lần cleanup KHÔNG được sập vòng lặp — nếu 1 lần
        dọn hỏng (VD DB tạm lỗi) thì log-and-continue, lần sau thử lại.
        """
        while True:
            try:
                await asyncio.sleep(_JOB_CLEANUP_INTERVAL_SECONDS)
                await self._cleanup_old_terminal_jobs()
            except asyncio.CancelledError:
                raise
            except Exception:
                import logging as _logging
                _logging.getLogger(__name__).exception(
                    "job cleanup loop iteration failed — will retry next tick"
                )

    async def _cleanup_old_terminal_jobs(self) -> None:
        """Dọn job terminal quá `_JOB_RETENTION_SECONDS` HOẶC vượt trần
        `_JOB_RETENTION_MAX_COUNT`.

        Điều kiện xoá:
        1. Job ở trạng thái terminal (`qr_ready`/`error`/`stopped`) VÀ
           `updated_at` cũ hơn `_JOB_RETENTION_SECONDS` so với `now`.
        2. Nếu tổng số job terminal (sau bước 1) vẫn > `_JOB_RETENTION_MAX_COUNT`,
           xoá tiếp job terminal cũ nhất (sort theo `updated_at` tăng dần)
           cho tới khi bằng trần.

        KHÔNG BAO GIỜ đụng job `pending`/`running` — chỉ user (qua
        `stop`/`delete_job`) mới có quyền huỷ job đang xử lý.

        Persist: mỗi job bị xoá cũng gọi `_delete_persisted` để DB
        không còn row → restart backend không load lại (nếu load lại thì
        cleanup tick kế cũng dọn, nhưng tốn 1 chu kỳ chờ và làm phình
        `_jobs` tạm thời sau restart → xoá luôn cả DB rõ ràng hơn).

        Idempotent: gọi lại ngay lập tức là no-op (không còn job đủ điều
        kiện). An toàn khi race với `delete_job` — thao tác `pop` trên
        dict không tồn tại là no-op.
        """
        now = time.time()
        terminal_statuses = (JobStatus.QR_READY, JobStatus.ERROR, JobStatus.STOPPED)

        # Snapshot values() để tránh RuntimeError mutation-during-iter.
        # Dict được mutate ngoài lock đâu đó khác (submit_batch) — snapshot
        # ổn định là an toàn.
        terminal_records: list[_JobRecord] = [
            r for r in list(self._jobs.values()) if r.status in terminal_statuses
        ]

        to_delete_ids: list[str] = []

        # Bước 1: TTL.
        for record in terminal_records:
            if now - record.updated_at > _JOB_RETENTION_SECONDS:
                to_delete_ids.append(record.job.job_id)

        # Bước 2: trần cứng — chỉ tính job terminal CHƯA bị đánh dấu xoá
        # ở bước 1, tránh double count.
        remaining_terminal = [
            r for r in terminal_records if r.job.job_id not in set(to_delete_ids)
        ]
        overflow = len(remaining_terminal) - _JOB_RETENTION_MAX_COUNT
        if overflow > 0:
            # Sort ASC theo updated_at → cũ nhất trước → xoá `overflow` phần tử đầu.
            remaining_terminal.sort(key=lambda r: r.updated_at)
            for record in remaining_terminal[:overflow]:
                to_delete_ids.append(record.job.job_id)

        if not to_delete_ids:
            return

        # Chỉ những job THỰC SỰ bị pop khỏi `_jobs` trong lock dưới đây mới
        # được persist-delete / báo hook — job bị skip (resubmit qua dedup
        # reuse giữa lúc snapshot và lúc acquire lock) phải giữ nguyên DB
        # row + cache external (VD DeviceProfileAllocator), vì nó vẫn còn
        # sống dưới cùng `job_id`.
        actually_removed_ids: list[str] = []
        async with self._lock:
            for job_id in to_delete_ids:
                record = self._jobs.get(job_id)
                if record is None:
                    continue
                # Double-check status — có thể job vừa được resubmit qua
                # dedup reuse (status đổi lại thành PENDING) giữa 2 lần
                # check. Nếu vậy skip để không xoá nhầm job đang chờ.
                if record.status not in terminal_statuses:
                    continue
                if record.dedup_key and self._dedup_index.get(record.dedup_key) == job_id:
                    self._dedup_index.pop(record.dedup_key, None)
                self._jobs.pop(job_id, None)
                actually_removed_ids.append(job_id)

        for job_id in actually_removed_ids:
            await self._delete_persisted(job_id)
            self._invoke_job_removed_hooks(job_id)

        import logging as _logging
        _logging.getLogger(__name__).info(
            "job cleanup: removed %d terminal jobs older than %ds "
            "(retention=%ds, max_count=%d)",
            len(actually_removed_ids),
            int(_JOB_RETENTION_SECONDS),
            int(_JOB_RETENTION_SECONDS),
            _JOB_RETENTION_MAX_COUNT,
        )

    async def _scheduler_loop(self) -> None:
        """Vòng lặp scheduler — chờ job pending + method còn slot → spawn.

        Kiến trúc: mỗi vòng lặp
        1. Chờ job pending Push/Pull-eligible mà method còn capacity
           (`_await_next_pending_job_id`) — saturated method KHÔNG chặn
           job method khác phía sau trong FIFO.
        2. Acquire limiter của đúng payment_method.
        3. Re-check job vẫn `pending` — nếu không, release slot method và
           tiếp job kế.
        4. Xóa khỏi `_pending_order` và spawn `_run_handler(record)`.
           `_run_handler` release lease + method slot khi thoát.

        Fail_Fast (R14.2): exception thuộc 1 job do `_run_handler` catch.
        Loop chỉ catch `Exception` để không sập scheduler.
        """
        while True:
            try:
                job_id = await self._await_next_pending_job_id()
                record_peek = self._jobs.get(job_id)
                if record_peek is None:
                    continue
                payment_method = record_peek.job.payment_method
                if not self._try_acquire_method_slot(payment_method):
                    # Race hiếm: capacity vừa hết giữa pick và acquire —
                    # chờ event (release/settings) rồi thử lại.
                    self._new_job_event.clear()
                    await self._new_job_event.wait()
                    continue

                async with self._lock:
                    record = self._jobs.get(job_id)
                    if record is None or record.status != JobStatus.PENDING:
                        # Job đã bị stop / xoá giữa 2 lần check — trả slot
                        # method và tiếp job kế.
                        self._release_slot(payment_method)
                        continue

                    try:
                        self._pending_order.remove(job_id)
                    except ValueError:
                        pass
                    record.handler_task = asyncio.create_task(
                        self._run_handler(record)
                    )
            except asyncio.CancelledError:
                # Scheduler bị cancel (shutdown) — propagate để task chấm dứt.
                # Slot đã acquire nhưng chưa spawn handler: không xảy ra
                # trong nhánh này (CancelledError trước acquire hoặc sau
                # spawn). Nếu cancel giữa acquire và spawn, finally của
                # handler không chạy — nhánh re-check PENDING handle bằng
                # `_release_slot` ở continue; cancel task ở đây hiếm. An toàn
                # hơn: không acquire trước khi vào critical section nếu
                # cancel — chấp nhận, shutdown dọn process.
                raise
            except Exception:
                # Bug trong scheduler (không thuộc 1 job cụ thể) — nhường
                # event loop 1 tick rồi tiếp tục, KHÔNG sập vòng lặp.
                # Không có `job_id` cụ thể để log qua SSE ở đây.
                await asyncio.sleep(0)
                continue

    async def _await_next_pending_job_id(self) -> str:
        """Chờ job pending "khả dụng" + method còn slot.

        Trả `job_id` sớm nhất trong FIFO thoả:
          1. Push/Pull pause semantics (gate paused → chỉ Pull),
          2. method của job còn capacity (`has_capacity`).

        Saturated method A với job đứng đầu queue KHÔNG chặn job method B
        phía sau — scan tiếp trong `_pending_order`.

        Chưa xóa khỏi `_pending_order` — scheduler acquire slot rồi mới xóa.
        """
        while True:
            if self._pending_order:
                push_paused = self._is_push_paused()
                for job_id in self._pending_order:
                    record = self._jobs.get(job_id)
                    if record is None:
                        continue
                    if record.status != JobStatus.PENDING:
                        continue
                    # Push paused → bỏ qua job Push (pull_assignment_state None).
                    if push_paused and record.pull_assignment_state is None:
                        continue
                    method = record.job.payment_method
                    limiter = self._method_limiters.get(method)
                    if limiter is None or not limiter.has_capacity():
                        continue
                    return job_id
                # Có pending nhưng không job nào vừa eligible vừa còn slot
                # (tất cả method saturated, hoặc chỉ còn Push khi paused)
                # → chờ event.
            self._new_job_event.clear()
            await self._new_job_event.wait()

    async def _run_handler(self, record: _JobRecord) -> None:
        """Wrapper thực thi handler cho 1 job.

        Đây là "boundary duy nhất" (R14.2, Fail_Fast) — mọi exception trong
        `handler.run()` được catch tại đây → set job → ERROR + log rõ nguyên
        nhân, KHÔNG raise ra ngoài scheduler loop.

        Trách nhiệm cleanup (release lease, release slot, phát SSE cuối cùng)
        nằm trong `finally` block để LUÔN chạy dù handler thoát thế nào —
        thành công, lỗi, hay bị cancel.
        """
        job_id = record.job.job_id
        lease: "ProxyLease | None" = None
        try:
            # Cancellation kiểm tra trước khi acquire proxy — tránh tốn 1
            # lease nếu job đã bị stop() trong lúc chờ slot.
            if record.job.cancellation_token.is_cancelled():
                record.status = JobStatus.STOPPED
                record.updated_at = time.time()
                record.finished_at = record.updated_at
                await self._persist_record(record)
                await self._broadcast_status(record, JobStatus.STOPPED.value)
                return

            # Acquire proxy lease QUA `acquire_live_proxy` — probe tươi
            # trước khi cấp cho job (Requirement 9.6 mở rộng, port từ
            # `gpt_signup_hybrid/web/proxy_health.py::acquire_live_proxy`).
            #
            # 3 khả năng return:
            #   - `ProxyLease` non-None: đã probe live, `materialized_url`
            #     là URL đã materialize SID (không phải template raw).
            #   - `None` khi pool rỗng: Direct_Mode hợp lệ (R9.2).
            #   - `None` khi pool có proxy nhưng cạn `probe_max_tries` /
            #     tất cả proxy fail probe: caller quyết định — fallback
            #     direct nếu `proxy.fallback_direct_on_exhausted=True`,
            #     hoặc emit `proxy_exhausted` error.
            #
            # `wait_for_release=True` (bên trong `acquire_live_proxy`):
            # pool poll đợi khi còn proxy alive nhưng tất cả đang bị
            # lease. Semantic đúng cho Multi-20 với 5 proxy: 5 job đầu
            # chiếm 5 lease, 15 job sau ĐỢI trong pool thay vì fail ngay.
            #
            # `ProxyExhaustedError` chỉ raise khi `dead_count == total_proxies`
            # (tất cả proxy đã hỏng threshold-based) — không có hy vọng
            # tự phục hồi.
            # `probe_config` là property mới trên `ProxyPool` — dùng
            # `getattr` fallback về `ProbeConfig(enabled=False)` (probe
            # disabled → passthrough `pool.acquire()`) khi caller inject
            # 1 pool implementation không có `probe_config` (test double
            # cũ) để giữ backward-compat với fake `FakeProxyPool` trong
            # test suite chưa mở rộng interface.
            from app.core.proxy_health import ProbeConfig as _ProbeConfig
            probe_config = getattr(
                self._proxy_pool, "probe_config", _ProbeConfig(enabled=False)
            )
            import logging as _logging
            try:
                lease = await acquire_live_proxy(
                    self._proxy_pool,
                    job_id,
                    probe_config,
                    logger=_logging.getLogger(__name__),
                    cancellation_token=record.job.cancellation_token,
                )
            except ProxyExhaustedError as ex:
                # Fallback Direct_Mode nếu user opt-in — chấp nhận rủi ro
                # lộ IP thật ra chatgpt.com để job không chết oan.
                if probe_config.fallback_direct_on_exhausted:
                    await self._append_log(
                        job_id,
                        "proxy_fallback_direct",
                        reason="proxy_exhausted",
                        detail=str(ex),
                    )
                    lease = None
                else:
                    record.status = JobStatus.ERROR
                    record.error_code = "proxy_exhausted"
                    record.error_message = str(ex)
                    record.updated_at = time.time()
                    record.finished_at = record.updated_at
                    await self._append_log(
                        job_id,
                        "proxy_exhausted",
                        error_code="proxy_exhausted",
                        error_message=str(ex),
                    )
                    await self._persist_record(record)
                    await self._broadcast_status(
                        record,
                        JobStatus.ERROR.value,
                        error_code="proxy_exhausted",
                        error_message=str(ex),
                    )
                    # R13.1: job Pull_Mode đã ASSIGNED chết ở bước xin
                    # proxy vẫn là "lỗi kỹ thuật trước QR_READY" → chốt
                    # FAIL + gửi thông báo kèm nút "Nhận job tiếp". Push_Mode
                    # giữ nguyên hành vi cũ (return, KHÔNG auto-retry cho
                    # proxy_exhausted) — chỉ thêm nhánh Pull_Mode, không đổi
                    # đường đi Push_Mode. `finally` phía dưới vẫn chạy để
                    # release slot (lease is None nên không release pool).
                    if (
                        record.pull_assignment_state
                        == PullAssignmentState.ASSIGNED
                    ):
                        await self.resolve_pull_error(job_id, "proxy_exhausted")
                    return
            except asyncio.CancelledError:
                if (
                    record.job.cancellation_token.is_paused()
                    and not record.job.cancellation_token.is_cancelled()
                ):
                    await self._handle_pause_requested(
                        record,
                        reason="push_gate_hard_pause_during_proxy_acquire",
                    )
                    return
                # User cancel job đang chờ proxy — mark STOPPED và raise
                # lại để task cleanup đúng cách. `finally` block phía
                # dưới sẽ release slot (lease vẫn `None` — không có gì
                # để release trên pool).
                record.status = JobStatus.STOPPED
                record.updated_at = time.time()
                record.finished_at = record.updated_at
                await self._persist_record(record)
                await self._broadcast_status(record, JobStatus.STOPPED.value)
                raise

            # Log rõ proxy vừa acquire — mask credential URL + hash-safe
            # proxy_id (KHÔNG log raw lease.proxy_id — raw line/template
            # thường chứa secret). Direct mode → mask 'direct', proxy_id None.
            from app.core.proxy_format import (
                mask_proxy as _mask_proxy,
                safe_proxy_id as _safe_proxy_id,
            )
            proxy_masked = _mask_proxy(lease.materialized_url if lease else None)
            await self._append_log(
                job_id,
                "proxy_acquired",
                proxy_id=(
                    _safe_proxy_id(lease.proxy_id) if lease is not None else None
                ),
                proxy=proxy_masked,
                mode="proxied" if lease else "direct",
            )

            record.lease = lease
            record.status = JobStatus.RUNNING
            now = time.time()
            # `started_at` set 1 lần duy nhất khi job vào RUNNING lần đầu
            # (bao gồm trường hợp dedup reuse — record đã reset về None
            # trong `submit_batch`). FE dùng làm mốc đếm elapsed.
            record.started_at = now
            record.finished_at = None
            record.updated_at = now
            # `held` chỉ có ý nghĩa lúc pending — job vào RUNNING nghĩa là
            # đã được start (dù qua `start_job` hay đường auto khác), clear
            # để restart resume không kẹt lại state held. Persist ngay ở
            # dòng dưới đồng bộ giá trị này với DB.
            record.held = False
            await self._persist_record(record)
            await self._broadcast_status(record, JobStatus.RUNNING.value)

            handler = self._handlers[record.job.payment_method]
            result: JobResult = await handler.run(record.job, lease)

            # Push_Success_Gate pause: handler tự nguyện dừng SỚM sau
            # checkpoint an toàn (login xong, cache session đã persist)
            # vì gate đã hit threshold. Ưu tiên xử lý TRƯỚC nhánh
            # cancel-override và mọi hook (terminal/auto-retry) —
            # pause_requested KHÔNG PHẢI terminal, chỉ là "quay lại
            # PENDING chờ user resume".
            #
            # Nếu user cancel giữa lúc pause (edge case race):
            # `is_cancelled=True` VÀ handler trả pause_requested=True
            # → ưu tiên CANCEL (fall qua nhánh dưới sẽ override thành
            # STOPPED). Check `is_cancelled()` trước để honor intent
            # user mạnh nhất.
            if (
                result.pause_requested
                and not record.job.cancellation_token.is_cancelled()
            ):
                await self._handle_pause_requested(record)
                return

            # BUG FIX (stop-all vẫn gửi QR về Telegram):
            #
            # Race condition giữa `stop()` và `handler.run()` return:
            # `handler.run()` chỉ check `cancellation_token.is_cancelled()`
            # TRƯỚC mỗi bước, không check giữa lần request network của
            # từng bước. Nếu user gọi `stop-all` khi handler đang trong
            # step, có 2 nhánh kết quả không mong muốn:
            #
            # 1. `QR_READY`: token set NGAY SAU lần check cuối ở step 12
            #    → handler kịp render QR + trả `JobResult(QR_READY)` →
            #    `_invoke_terminal_hooks` được gọi → Telegram nhận QR
            #    dù user đã yêu cầu dừng.
            #
            # 2. `ERROR` với error_code trong whitelist auto-retry: token
            #    set nhưng handler map network fail thành ERROR
            #    (`pay_ideal_page_http_error`, ...) → `_maybe_auto_retry`
            #    schedule requeue → `_perform_auto_requeue` cấp
            #    `cancellation_token` MỚI (sạch, token cũ bị vứt) → job
            #    chạy lại đầy đủ → có thể QR_READY → Telegram gửi.
            #
            # Chính sách sửa: nếu user đã cancel giữa lúc handler chạy,
            # HONOR intent tuyệt đối — override kết quả thành STOPPED
            # bất kể handler trả gì. Việc này:
            #   - Skip `_invoke_terminal_hooks` (chỉ chạy khi QR_READY).
            #   - Skip `_maybe_auto_retry` (chỉ chạy khi ERROR).
            # Artifact PNG (nếu đã sinh) còn trên đĩa nhưng KHÔNG gán
            # `record.artifact_path` để tránh FE hiển thị nút tải cho
            # job STOPPED (không nhất quán trạng thái).
            if (
                result.status in (JobStatus.QR_READY, JobStatus.ERROR)
                and record.job.cancellation_token.is_cancelled()
            ):
                import logging as _logging
                _logging.getLogger(__name__).info(
                    "job cancelled during handler execution — override "
                    "%s to stopped, skip terminal hooks / auto-retry: "
                    "job_id=%s",
                    result.status.value,
                    job_id,
                )
                await self._append_log(
                    job_id,
                    "stopped_after_handler_return",
                    original_status=result.status.value,
                    original_error_code=result.error_code,
                    reason=(
                        "cancellation signaled between last is_cancelled() "
                        "check and handler return"
                    ),
                )
                result = JobResult(status=JobStatus.STOPPED)

            # Live QR / Push gate can become blocked while a handler is already
            # past its last pause checkpoint and is returning QR_READY. Do not
            # persist/enqueue that QR: it would sit behind the gate and can
            # expire before Telegram is allowed to send it. Requeue the account
            # so the next run creates a fresh QR after capacity is available.
            if (
                result.status == JobStatus.QR_READY
                and record.pull_assignment_state is None
                and self._is_push_paused()
            ):
                import logging as _logging

                _logging.getLogger(__name__).info(
                    "push gate blocked after handler returned QR_READY; "
                    "requeue before terminal hooks: job_id=%s",
                    job_id,
                )
                await self._handle_pause_requested(
                    record,
                    reason="push_gate_blocked_after_qr_ready",
                )
                return

            # Health feedback về pool — sau khi flow trả result:
            #   - Explicit `result.proxy_lease_health` (handler mới, VD
            #     upi_direct): ALIVE → mark_alive, DEAD → mark_dead; SKIP
            #     legacy inference.
            #   - Legacy (proxy_lease_health is None — ideal/upi/upi_nocdk):
            #       * QR_READY → mark_alive
            #       * ERROR + is_network_error(error_message) → mark_dead
            #       * business/auth lỗi khác — không mark
            # `getattr` fallback cho fake pool trong test không implement API.
            if lease is not None:
                explicit_health = getattr(result, "proxy_lease_health", None)
                if explicit_health is not None:
                    if explicit_health == ProxyLeaseHealth.ALIVE:
                        mark_alive = getattr(self._proxy_pool, "mark_alive", None)
                        if mark_alive is not None:
                            mark_alive(lease.proxy_id)
                    elif explicit_health == ProxyLeaseHealth.DEAD:
                        mark_dead = getattr(self._proxy_pool, "mark_dead", None)
                        if mark_dead is not None:
                            mark_dead(lease.proxy_id)
                elif result.status == JobStatus.QR_READY:
                    mark_alive = getattr(self._proxy_pool, "mark_alive", None)
                    if mark_alive is not None:
                        mark_alive(lease.proxy_id)
                elif result.status == JobStatus.ERROR and is_network_error(
                    result.error_message
                ):
                    mark_dead = getattr(self._proxy_pool, "mark_dead", None)
                    if mark_dead is not None:
                        mark_dead(lease.proxy_id)

            record.status = result.status
            record.artifact_path = result.artifact_path
            record.error_code = result.error_code
            record.error_message = result.error_message
            record.payment_link = result.payment_link
            record.qr_expires_at = getattr(result, "qr_expires_at", None)
            # Handler-proven plan (e.g. already-paid checkout → plus) so
            # Free export excludes the row without a separate check-plan.
            became_plus = False
            plan_hint = getattr(result, "plan", None)
            if plan_hint in ("plus", "free") and record.plan != plan_hint:
                became_plus = plan_hint == "plus"
                record.plan = plan_hint
            record.updated_at = time.time()
            # Handler đã trả về → job ở trạng thái TERMINAL (qr_ready /
            # error / stopped). Đóng băng finished_at để FE dừng đếm.
            record.finished_at = record.updated_at
            extra: dict[str, Any] = {}
            if result.error_code:
                extra["error_code"] = result.error_code
            if result.error_message:
                extra["error_message"] = result.error_message
            if result.artifact_path:
                extra["artifact_path"] = result.artifact_path
            if result.payment_link:
                # SSE consumer (FE `applyStatusEvent`) đọc key này để populate
                # `payment_link` vào JobViewModel ngay khi qr_ready — không cần
                # fetch lại `GET /api/jobs/{id}`.
                extra["payment_link"] = result.payment_link
            # Kèm retry_count để FE render badge "R{n}/{max}" ngay từ SSE
            # đầu tiên, không phải chờ loadAll refresh.
            extra["retry_count"] = record.retry_count
            await self._persist_record(record)
            await self._broadcast_status(record, result.status.value, **extra)

            # Terminal hooks — chỉ trigger khi QR_READY (job success theo
            # yêu cầu: bot Telegram chỉ báo cho user khi có QR sẵn để
            # thanh toán). ERROR/STOPPED KHÔNG kick hook. Gọi SAU khi
            # persist + broadcast SSE để FE cập nhật state trước khi
            # external notification đi ra ngoài (giữ đúng thứ tự
            # observability: user thấy job đổi status → nhận notify).
            if (
                record.status == JobStatus.QR_READY
                and getattr(record.job, "payment_method", None) != "momo_check"
            ):
                await self._invoke_terminal_hooks(record)
                # Auto Check Plus sau QR — cùng semantic nút "Check Plus" /
                # Check Plus All, nhưng fire-and-forget ngay khi job
                # `qr_ready` để Success panel + badge plan fill không cần
                # user bấm tay. Áp dụng mọi payment method có
                # `handler.check_plan_status` (iDEAL + UPI). Best-effort:
                # không block lease release / scheduler; lỗi network để
                # plan=null (user vẫn bấm Check Plus được).
                self._schedule_auto_check_plan(job_id)

            # Handler-proven plus (already-paid, etc.) — same hooks as
            # check_plan_status so Telegram/live-slot release still runs.
            if became_plus:
                await self._invoke_plus_verified_hooks(job_id)

            # Auto-retry cho job kết thúc với error_code thuộc whitelist
            # transient (Requirement mới — tham chiếu UPI `web/manager.py`).
            # Gọi SAU khi persist + broadcast state ERROR để FE thấy chuỗi:
            #   ERROR → (delay) → PENDING → RUNNING → ...
            # thay vì skip qua ERROR (giữ trace debug rõ ràng).
            #
            # Job Pull_Mode CÓ Job_Pull_Assignment (`pull_assignment_state
            # == ASSIGNED`) KHÔNG được auto-retry (R13.5) — thay vào đó
            # `resolve_pull_error` chốt `pull_outcome = FAIL` + gửi thông
            # báo lỗi kỹ thuật kèm nút "Nhận job tiếp" qua pull-error hook,
            # để worker chủ động pull account MỚI thay vì hệ thống tự
            # requeue lại account đã lỗi.
            if record.status == JobStatus.ERROR:
                await self._dispatch_error_outcome(record, result.error_code)

            # FIX 7 (STOPPED slot leak): cancel-override path phía trên đã
            # ép `result = JobResult(STOPPED)` khi user cancel giữa lúc
            # handler chạy. Với job Pull_Mode ASSIGNED + PENDING, STOPPED
            # KHÔNG kích hoạt terminal hook (chỉ QR_READY) cũng KHÔNG
            # auto-retry (chỉ ERROR) → nếu không chốt outcome, slot worker
            # kẹt vĩnh viễn. Chốt FAIL để giải phóng (no-op cho Push_Mode
            # → giữ nguyên hành vi stop hiện có).
            if record.status == JobStatus.STOPPED:
                await self._resolve_stopped_pull_job(record)
        except asyncio.CancelledError:
            if (
                record.job.cancellation_token.is_paused()
                and not record.job.cancellation_token.is_cancelled()
            ):
                await self._handle_pause_requested(
                    record,
                    reason="push_gate_hard_pause_cancelled_task",
                )
                return
            # Handler task bị cancel từ ngoài (ví dụ shutdown scheduler) —
            # mark stopped + persist best-effort + propagate để task thực
            # sự exit. Best-effort vì event loop có thể đang shutdown,
            # persist có thể fail; `_persist_record` đã fail-safe.
            record.status = JobStatus.STOPPED
            record.updated_at = time.time()
            record.finished_at = record.updated_at
            await self._persist_record(record)
            raise
        except Exception as ex:
            # Fail_Fast (R14.2): log lỗi cụ thể, KHÔNG re-raise → không sập
            # scheduler / các job khác. Message không lộ giá trị nhạy cảm
            # (redact_dict áp dụng cho extra).
            #
            # Format `error_message` = "<ExceptionType>: <message>" để phân
            # biệt được loại exception, giúp debug lỗi bugs ngoài
            # `IdealFlowError` hierarchy (Requirement 14.2 — log đầy đủ
            # nguyên nhân, KHÔNG dùng thông báo chung mơ hồ).
            #
            # Traceback được log qua `logging.getLogger(__name__)` để
            # xuất hiện trong stderr — dev có stack trace debug mà không
            # cần thay đổi format SSE (Frontend/CLI).
            import logging as _logging
            _logging.getLogger(__name__).exception(
                "unexpected exception in _run_handler: job_id=%s", job_id
            )
            record.status = JobStatus.ERROR
            record.error_code = "internal_error"
            record.error_message = f"{type(ex).__name__}: {ex}"
            record.updated_at = time.time()
            record.finished_at = record.updated_at
            await self._append_log(
                job_id,
                "internal_error",
                error_code="internal_error",
                error_type=type(ex).__name__,
                error_message=str(ex),
            )
            await self._persist_record(record)
            await self._broadcast_status(
                record,
                JobStatus.ERROR.value,
                error_code="internal_error",
                error_message=f"{type(ex).__name__}: {ex}",
                retry_count=record.retry_count,
            )
            # Job Pull_Mode CÓ Job_Pull_Assignment lỗi kỹ thuật bất ngờ
            # (`internal_error`) PHẢI được chốt FAIL ngay (R13.1) — nếu
            # không, `pull_outcome` kẹt `PENDING` vĩnh viễn, worker bị
            # chiếm slot concurrent và không nhận được thông báo. Job
            # Push_Mode giữ nguyên hành vi: `internal_error` KHÔNG thuộc
            # default whitelist retry, nhưng vẫn gọi qua boundary duy nhất
            # `_maybe_auto_retry` (hàm tự check whitelist rồi no-op hay
            # schedule) để user có thể tự thêm code này qua Settings.
            await self._dispatch_error_outcome(record, "internal_error")
        finally:
            if lease is not None:
                self._proxy_pool.release(lease)
                record.lease = None
            self._release_slot(record.job.payment_method)
            # Đánh thức scheduler để nhận job kế tiếp (nếu còn pending).
            self._new_job_event.set()

    def _try_acquire_method_slot(self, payment_method: str) -> bool:
        """Non-blocking acquire 1 slot của `payment_method`. True nếu lấy được."""
        limiter = self._method_limiters.get(payment_method)
        if limiter is None or not limiter.has_capacity():
            return False
        limiter.acquire()
        return True

    def _release_slot(self, payment_method: str) -> None:
        """Giải phóng 1 slot concurrency của ĐÚNG payment_method.

        Ưu tiên giảm `over_budget_slots` của method — sinh ra khi
        `update_method_max_concurrent` hạ limit dưới số job đang running
        của method đó. Đảm bảo capacity method không vượt configured_max mới.
        """
        limiter = self._method_limiters.get(payment_method)
        if limiter is None:
            return
        limiter.release()

    def record_log_line(
        self, job_id: str, message: str, ts: float, level: str
    ) -> bool:
        """Lưu 1 dòng log runtime (từ `SseLogHandler`) vào buffer của job.

        Được `SseLogHandler` gọi sync (không async) từ logger emit — nên
        method này là sync và chỉ mutate in-memory state, KHÔNG broadcast
        SSE (handler tự broadcast sau đó qua `run_coroutine_threadsafe`).

        Return `True` nếu record tồn tại và log đã được lưu; `False` nếu
        job đã bị pop khỏi map (delete_job / cleanup). `SseLogHandler`
        dùng return value để quyết định có broadcast SSE hay không:
        broadcast cho job đã xóa → FE `applyLogEvent` tự tạo entry ghost
        → toast 404 khi user click. Skip broadcast là fix triệt để.
        """
        record = self._jobs.get(job_id)
        if record is None:
            return False
        entry: dict[str, Any] = {"ts": ts, "message": message, "level": level}
        record.logs.append(entry)
        if ts > record.updated_at:
            record.updated_at = ts
        return True

    async def _append_log(self, job_id: str, message: str, **extra: Any) -> None:
        """Ghi 1 log entry vào buffer của job + phát qua SSE (Requirement 8.8).

        Áp dụng `redact_dict()` cho `extra` trước khi ghi buffer và trước
        khi phát SSE — Sensitive_Data_Redaction (R14.8). SseBroadcaster
        cũng tự redact 1 lần nữa như lớp phòng thủ bổ sung.

        Guard: nếu job đã bị pop khỏi map (delete_job / cleanup), skip
        cả buffer lẫn broadcast SSE — nếu vẫn broadcast, FE
        `applyLogEvent` sẽ TỰ TẠO entry ghost trong Map (fallback
        "if !existing → create shell") dẫn tới toast 404 khi user click.
        """
        record = self._jobs.get(job_id)
        if record is None:
            return
        redacted_extra = redact_dict(extra)
        now = time.time()
        log_entry: dict[str, Any] = {"ts": now, "message": message, **redacted_extra}
        record.logs.append(log_entry)
        record.updated_at = now
        await self._sse.broadcast_job_log(job_id, message, ts=now, **redacted_extra)

    # ==================================================================
    # Auto-retry cho job kết thúc với error_code "transient / blocked"
    # ------------------------------------------------------------------
    # Tham chiếu: gpt_signup_hybrid/web/manager.py::_maybe_auto_retry.
    #
    # Cơ chế:
    #   1. Handler trả về `JobResult(status=ERROR, error_code=<code>)`.
    #   2. `_run_handler` gọi `_maybe_auto_retry(record)` tại boundary
    #      duy nhất — hàm này đọc 4 setting live (`enabled`/`max`/
    #      `delay`/`codes`), quyết định retry hay không, log rõ ràng.
    #   3. Nếu retry → spawn task nền `_delayed_requeue_runner` sleep
    #      `delay * retry_count` giây (backoff tuyến tính), sau đó reset
    #      record về PENDING và kick scheduler.
    #   4. Task lưu trong `_delayed_requeue_tasks` để `stop()`/
    #      `delete_job()`/`shutdown()` cancel được → tránh double
    #      enqueue hoặc requeue "ghost".
    #
    # Design decision — dễ mở rộng:
    #   - Whitelist `codes` là setting `ideal.auto_retry_blocked_codes`
    #     (list_str, sửa qua Settings UI/API) — thêm/bớt code không cần
    #     đụng vào code Python.
    #   - Backoff tuyến tính `delay * (retry_count)` — cùng công thức
    #     UPI. Nếu tương lai muốn exponential/jitter, chỉ sửa 1 chỗ tính
    #     `delay_seconds` trong `_maybe_auto_retry`.
    #   - Auto-retry là generic ở tầng `core/` — không hardcode payment
    #     method. Prefix key `ideal.*` chỉ do namespace payment method
    #     đang có duy nhất; nếu thêm payment method khác dùng cùng cơ
    #     chế, đưa key vào namespace tương ứng và mở rộng `_read_settings`
    #     xử lý dispatch theo `record.job.payment_method`.
    # ==================================================================

    # Namespace key trong Settings_Store — hardcode ở tầng `core/` là
    # tradeoff có ý thức: cả 4 key thuộc namespace payment method
    # `ideal.*`, nhưng bản thân logic retry là generic. Khi có payment
    # method thứ hai muốn có auto-retry riêng, refactor `_get_auto_retry_config`
    # nhận `payment_method` và tra bảng namespace tương ứng.
    _AUTO_RETRY_KEY_ENABLED = "ideal.auto_retry_blocked_enabled"
    _AUTO_RETRY_KEY_MAX = "ideal.auto_retry_blocked_max"
    _AUTO_RETRY_KEY_DELAY = "ideal.auto_retry_blocked_delay_seconds"
    _AUTO_RETRY_KEY_CODES = "ideal.auto_retry_blocked_codes"
    _AUTO_RETRY_KEY_MODE = "ideal.auto_retry_mode"
    # Toggle "failed → pending" — sau exhaust `max` lượt vẫn fail: True
    # = quay về PENDING (reset `retry_count=0`) → loop vòng mới; False
    # = ERROR final (hành vi cũ, user tự bấm "Retry failed").
    _AUTO_RETRY_KEY_FAILED_TO_PENDING = "ideal.auto_retry_failed_to_pending"
    # 2 mode retry hỗ trợ:
    #   - `same_account`: sleep(cap-linear backoff) rồi requeue cùng job
    #     (behavior gốc UPI, phù hợp anti-fraud cần cooldown).
    #   - `round_robin`: KHÔNG sleep, append thẳng vào cuối queue để
    #     scheduler chạy account khác trước; đến khi vòng lại account
    #     này. Phù hợp khi user muốn "hết list 1 vòng rồi retry".
    _AUTO_RETRY_MODE_SAME_ACCOUNT = "same_account"
    _AUTO_RETRY_MODE_ROUND_ROBIN = "round_robin"

    # Backoff cap-linear: `delay = min(base × retry_count, base × N)`.
    # Với N=3, retry ban đầu tăng dần (base, 2×base, 3×base), sau đó
    # giữ ở 3×base mãi mãi — cho phép retry hàng trăm lần mà không leo
    # thời gian chờ vô hạn. Khác `delay × retry_count` thuần của UPI
    # (đúng khi max ≤ 10, nhưng vô lý khi max lên hàng trăm).
    #
    # Tại sao N=3 mà không phải 2/5/10:
    #   - N=2: retry ổn định quá nhanh (2×base), có thể hammer server
    #     khi anti-fraud chưa reset.
    #   - N=5+: retry cuối chờ 5-10 phút, tổng thời gian với max=300
    #     leo lên 5-10h — quay lại vấn đề "chết người".
    #   - N=3 cân bằng: cap ở 3×base = ~45s với base=15 → cho anti-fraud
    #     đủ thời gian nguội, tổng 300 retry ≤ 4h.
    _AUTO_RETRY_CAP_MULTIPLIER: float = 3.0

    async def _get_auto_retry_config(
        self,
    ) -> tuple[bool, int, float, set[str], str, bool]:
        """Đọc 6 setting auto-retry live từ Settings_Store.

        Fail-safe: nếu key chưa set hoặc kiểu sai (DB bị sửa thô) → dùng
        default an toàn (disabled, không retry) thay vì raise và làm sập
        `_run_handler`. Đảm bảo job error path luôn kết thúc gọn gàng.

        Returns:
            Tuple `(enabled, max_retries, delay_seconds, codes_set, mode,
            failed_to_pending)`. `failed_to_pending`: True → sau hết
            `max_retries` job quay về PENDING vòng mới thay vì ERROR final.
        """
        enabled = await self._settings.get(self._AUTO_RETRY_KEY_ENABLED)
        max_retries = await self._settings.get(self._AUTO_RETRY_KEY_MAX)
        delay_seconds = await self._settings.get(self._AUTO_RETRY_KEY_DELAY)
        codes = await self._settings.get(self._AUTO_RETRY_KEY_CODES)
        mode = await self._settings.get(self._AUTO_RETRY_KEY_MODE)
        failed_to_pending = await self._settings.get(
            self._AUTO_RETRY_KEY_FAILED_TO_PENDING
        )

        # Normalize + default hợp lý khi bất kỳ key nào chưa set.
        enabled_bool = bool(enabled) if isinstance(enabled, bool) else False
        max_int = (
            int(max_retries)
            if isinstance(max_retries, int) and not isinstance(max_retries, bool)
            else 0
        )
        delay_float = (
            float(delay_seconds)
            if isinstance(delay_seconds, (int, float))
            and not isinstance(delay_seconds, bool)
            else 0.0
        )
        codes_set: set[str] = (
            {c for c in codes if isinstance(c, str) and c}
            if isinstance(codes, list)
            else set()
        )
        # Fallback default `round_robin` khi giá trị DB không hợp lệ —
        # KHÔNG raise, giữ auto-retry hoạt động ở mode "chuyển sang
        # account khác" (an toàn hơn same_account vì không kẹt slot).
        if mode not in (
            self._AUTO_RETRY_MODE_SAME_ACCOUNT,
            self._AUTO_RETRY_MODE_ROUND_ROBIN,
        ):
            mode = self._AUTO_RETRY_MODE_ROUND_ROBIN
        # Default False (giữ hành vi cũ = ERROR final) khi key chưa set
        # hoặc kiểu sai — an toàn hơn ON default vì tránh loop bất ngờ
        # cho DB cũ chưa migrate seed.
        failed_to_pending_bool = (
            bool(failed_to_pending) if isinstance(failed_to_pending, bool) else False
        )
        return (
            enabled_bool,
            max_int,
            delay_float,
            codes_set,
            mode,
            failed_to_pending_bool,
        )

    async def _maybe_auto_retry(self, record: _JobRecord) -> bool:
        """Quyết định + lịch auto-retry cho 1 job vừa kết thúc ERROR.

        Return `True` nếu đã schedule retry (job sẽ tự PENDING sau delay),
        `False` nếu không đủ điều kiện (feature tắt, code không thuộc
        whitelist, đã hết retry budget, hoặc backend đang shutdown).

        Điều kiện KHÔNG retry (theo thứ tự check — ngắn trước):
            1. `_shutting_down` — backend sắp exit, không lịch task mới.
            2. `record.status != ERROR` — chỉ retry job kết thúc lỗi.
            3. `error_code = None` — không có code phân loại, không biết
               có transient không.
            4. `error_code = "cancelled"` — user chủ động dừng, không
               retry dù có nằm trong whitelist (phòng ngừa vô tình).
            5. Config `enabled=False`.
            6. `error_code` không thuộc `codes_set` (whitelist).
            7. `retry_count >= max_retries` — hết budget.

        Cách reset record để retry (giống dedup reuse thủ công nhưng
        GIỮ retry_count để backoff tuyến tính leo dần):
            - `retry_count += 1`
            - `status = PENDING`
            - clear `error_*`/`artifact_path`/`payment_link`/`logs`
            - `started_at = finished_at = None`
            - `cancellation_token` mới (token cũ đã cancel/consumed).
            - Persist + broadcast SSE PENDING → FE thấy chuỗi:
                ERROR → (delay) → PENDING → RUNNING → …
        """
        if self._shutting_down:
            return False
        if record.status != JobStatus.ERROR:
            return False
        error_code = record.error_code or ""
        if not error_code or error_code == "cancelled":
            return False

        (
            enabled,
            max_retries,
            delay_seconds,
            codes_set,
            mode,
            failed_to_pending,
        ) = await self._get_auto_retry_config()
        if not enabled:
            return False
        if error_code not in codes_set:
            return False
        if record.retry_count >= max_retries:
            await self._append_log(
                record.job.job_id,
                "auto_retry_exhausted",
                error_code=error_code,
                retry_count=record.retry_count,
                max_retries=max_retries,
                failed_to_pending=failed_to_pending,
            )
            if not failed_to_pending:
                # Hành vi cũ: hết budget → giữ ERROR final, user tự bấm
                # "Retry failed" bulk.
                return False
            # `failed_to_pending=True`: reset retry_count về 0 rồi requeue
            # NGAY (không sleep) — coi như "vòng grinding kế tiếp". Loop
            # vô hạn cho tới khi thành công (user đã confirm chấp nhận).
            # Semantic khác nhánh retry thường bên dưới:
            #   - KHÔNG tăng retry_count (reset về 0, đúng khái niệm
            #     "vòng mới").
            #   - KHÔNG sleep dù mode `same_account` — vòng mới nên bắt
            #     đầu như 1 job pending bình thường, chỉ backoff trong
            #     phạm vi 1 vòng.
            record.retry_count = 0
            await self._perform_auto_requeue(record)
            return True

        record.retry_count += 1

        # Chọn strategy retry theo `mode` — xem docstring
        # `_AUTO_RETRY_MODE_*` ở đầu class.
        if mode == self._AUTO_RETRY_MODE_ROUND_ROBIN:
            # Requeue NGAY (không sleep) → append thẳng vào cuối
            # `_pending_order`. Scheduler tiếp tục chạy các account
            # khác trước (chúng đứng đầu queue) → đến hết list mới
            # quay lại account fail này. Đúng semantic user muốn.
            await self._append_log(
                record.job.job_id,
                "auto_retry_scheduled",
                error_code=error_code,
                attempt=record.retry_count,
                max_retries=max_retries,
                mode=mode,
                delay_seconds=0.0,
            )
            await self._perform_auto_requeue(record)
            return True

        # Mode `same_account` — backoff cap-linear rồi requeue cùng job.
        #   delay = min(base × retry_count, base × CAP_MULTIPLIER)
        # Sleep async qua `_schedule_delayed_requeue` để KHÔNG chặn
        # scheduler chính; slot semaphore đã release trong finally của
        # `_run_handler`, task delayed-requeue tự kick lại khi hết delay.
        base = float(delay_seconds)
        delay = min(
            base * float(record.retry_count),
            base * self._AUTO_RETRY_CAP_MULTIPLIER,
        )

        await self._append_log(
            record.job.job_id,
            "auto_retry_scheduled",
            error_code=error_code,
            attempt=record.retry_count,
            max_retries=max_retries,
            mode=mode,
            delay_seconds=round(delay, 2),
        )

        self._schedule_delayed_requeue(record.job.job_id, delay)
        return True

    def _schedule_delayed_requeue(self, job_id: str, delay: float) -> None:
        """Spawn task nền sleep `delay` giây rồi requeue `job_id` về PENDING.

        Task được track trong `_delayed_requeue_tasks[job_id]`. Nếu đã có
        task cùng job_id đang chờ → cancel task cũ (tránh duplicate enqueue
        khi `_maybe_auto_retry` được gọi 2 lần liên tiếp cho cùng job —
        không được xảy ra theo lifecycle hiện tại nhưng defensive).

        Fail_Fast: nếu không có event loop chạy (test unit gọi thẳng), skip
        schedule — không raise để không phá path error thông thường.
        """
        if self._shutting_down:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # Test unit gọi ở context sync — không có loop, skip.
            return

        existing = self._delayed_requeue_tasks.get(job_id)
        if existing is not None and not existing.done():
            existing.cancel()

        task = asyncio.create_task(self._delayed_requeue_runner(job_id, delay))
        self._delayed_requeue_tasks[job_id] = task
        # Cleanup entry khi task xong (success/cancel/error) — chỉ pop nếu
        # entry hiện tại vẫn là task này (phòng race với schedule mới đè
        # lên slot map trước khi task cũ done).
        def _cleanup(t: asyncio.Task, jid: str = job_id) -> None:
            if self._delayed_requeue_tasks.get(jid) is t:
                self._delayed_requeue_tasks.pop(jid, None)
        task.add_done_callback(_cleanup)

    async def _delayed_requeue_runner(self, job_id: str, delay: float) -> None:
        """Task nền cho `_schedule_delayed_requeue` — sleep rồi requeue.

        Xử lý mọi edge case xuất hiện trong khoảng `delay` giây:
            - `_shutting_down` set → thoát, không kick job.
            - Job đã bị `delete_job` → record biến mất, thoát.
            - Job status không còn ERROR (VD user thao tác thủ công
              trong lúc chờ) → thoát, không đè state của user.

        Fail_Fast_Policy: bất kỳ exception ngoài `CancelledError` được
        catch tại đây (không sập scheduler chính). `CancelledError` re-raise
        để task chấm dứt sạch cho asyncio.
        """
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise

        if self._shutting_down:
            return

        record = self._jobs.get(job_id)
        if record is None:
            # Job đã bị xóa trong lúc sleep — no-op.
            return
        if record.status != JobStatus.ERROR:
            # User đã thao tác (submit lại / delete / rerun) — không đè
            # state hiện tại của user bằng auto-retry đã lỗi thời.
            return

        try:
            await self._perform_auto_requeue(record)
        except asyncio.CancelledError:
            raise
        except Exception:
            import logging as _logging
            _logging.getLogger(__name__).exception(
                "auto-retry requeue failed for job_id=%s", job_id
            )

    async def _handle_pause_requested(
        self,
        record: _JobRecord,
        *,
        reason: str = "push_success_gate_paused",
    ) -> None:
        """Chuyển 1 job RUNNING trở về PENDING vì Push_Success_Gate paused.

        Handler đã tự dừng SỚM (trả `JobResult.pause_requested=True`)
        SAU khi qua checkpoint an toàn — cụ thể với iDEAL: đã login xong
        và cache session đã `save()`. Chạy lại sẽ hit cache ở step 2, KHÔNG
        login lại lần nữa.

        Reset tối thiểu (giữ context người dùng thấy được):
            - `status = PENDING`.
            - `started_at`/`finished_at`/`error_*`/`artifact_path`/
              `payment_link` reset (chưa hoàn thành công việc — dữ liệu
              cũ misleading nếu để lại).
            - `retry_count` giữ nguyên (không phải retry sau error).
            - `logs` GIỮ NGUYÊN để user thấy được đã đi qua bao nhiêu
              step trước khi pause (không clear như `_perform_auto_requeue`).
            - `cancellation_token` MỚI để lần chạy lại không dính pause/
              cancel cũ.
            - Add lại vào `_pending_order` CUỐI queue (fair với job mới,
              không jump line). Scheduler sẽ tự skip khi gate paused và
              chỉ dispatch khi user Tiếp tục.

        Sau khi return, `finally` block của `_run_handler` sẽ release
        lease + slot bình thường — không đặc biệt hoá.
        """
        job_id = record.job.job_id
        now = time.time()

        async with self._lock:
            # Job có thể đã bị delete giữa lúc handler chạy → skip.
            if job_id not in self._jobs:
                return
            new_token = SimpleCancellationToken()
            record.job = Job(
                job_id=record.job.job_id,
                payment_method=record.job.payment_method,
                account_line=record.job.account_line,
                created_at=record.job.created_at,
                cancellation_token=new_token,
            )
            record.status = JobStatus.PENDING
            record.updated_at = now
            record.artifact_path = None
            record.error_code = None
            record.error_message = None
            record.payment_link = None
            record.qr_expires_at = None
            record.started_at = None
            record.finished_at = None
            record.lease = None
            record.handler_task = None
            if job_id not in self._pending_order:
                self._pending_order.append(job_id)

        await self._persist_record(record)
        await self._append_log(
            job_id,
            "paused_for_gate",
            reason=reason,
        )
        await self._broadcast_status(record, JobStatus.PENDING.value)

        # KHÔNG kick `_new_job_event` — gate vẫn đang paused, scheduler
        # đánh thức cũng chỉ để skip. Khi user bấm Tiếp tục,
        # `PushSuccessGate.resume()` sẽ gọi `wake_scheduler()` — lúc đó
        # scheduler mới nhặt job Push kể cả job vừa pause này.

    async def _perform_auto_requeue(self, record: _JobRecord) -> None:
        """Reset record về PENDING và kick scheduler (bên trong lock).

        Tách khỏi runner để test unit gọi được sync mà không cần task nền.
        Preserve `retry_count` (đã tăng ở `_maybe_auto_retry`) và `order`
        (dedup reuse KHÔNG đổi order — FE giữ nguyên vị trí row).
        """
        job_id = record.job.job_id
        now = time.time()

        async with self._lock:
            # Re-check dưới lock: task requeue có thể đã race qua
            # `_delayed_requeue_runner` (nó check trước lock) nhưng khi
            # tới đây job đã bị `delete_job` xóa khỏi map. Nếu tiếp tục
            # thì persist_record + broadcast_status sẽ tái tạo row DB
            # zombie + tạo entry ghost ở FE. Silent abort là fix triệt để.
            if job_id not in self._jobs:
                return
            # Cấp `cancellation_token` mới — token cũ có thể đã trong trạng
            # thái cancelled sau flow trước; job mới phải có token sạch.
            new_token = SimpleCancellationToken()
            record.job = Job(
                job_id=record.job.job_id,
                payment_method=record.job.payment_method,
                account_line=record.job.account_line,
                created_at=record.job.created_at,
                cancellation_token=new_token,
            )
            record.status = JobStatus.PENDING
            record.updated_at = now
            record.logs.clear()
            record.artifact_path = None
            record.error_code = None
            record.error_message = None
            record.payment_link = None
            record.qr_expires_at = None
            record.telegram_message_id = None
            record.started_at = None
            record.finished_at = None
            record.lease = None
            record.handler_task = None
            # Chống double enqueue: chỉ append nếu chưa có trong queue.
            if job_id not in self._pending_order:
                self._pending_order.append(job_id)

        await self._persist_record(record)
        await self._append_log(
            job_id,
            "auto_retry_start",
            attempt=record.retry_count,
        )
        await self._broadcast_status(record, JobStatus.PENDING.value)

        # Kick scheduler + đảm bảo task nền đang chạy (submit_batch mới
        # đảm bảo điều này; auto-retry cần kick lại nếu scheduler đã idle).
        self._ensure_scheduler_running()
        self._new_job_event.set()

    def _cancel_delayed_requeue(self, job_id: str) -> None:
        """Cancel task requeue đang chờ cho `job_id` (idempotent).

        Được gọi khi user `stop`/`delete_job`/`submit_batch` dedup reuse
        job đó — tránh double-enqueue hoặc kick job vào PENDING sau khi
        user đã chuyển sang state khác.
        """
        task = self._delayed_requeue_tasks.pop(job_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _cancel_auto_check_plan(self, job_id: str) -> None:
        """Cancel auto Check Plus poll for `job_id` (idempotent).

        Also fires live-slot release hooks — stop/rerun/delete cancel the
        poll so plus/timeout release never runs; free slots immediately.
        """
        bucket = getattr(self, "_auto_check_plan_tasks", None)
        if bucket:
            task = bucket.pop(job_id, None)
            if task is not None and not task.done():
                task.cancel()
        self._invoke_live_slot_release_hooks(job_id)

    async def shutdown(self) -> None:
        """Cancel toàn bộ task nền + CHỜ chúng exit trước khi return.

        Được `main.py` lifespan gọi TRƯỚC `db_engine.close()`. Không đóng
        `SseBroadcaster` vì client tự disconnect khi uvicorn stop.

        Sau khi gọi:
            - `_shutting_down = True` → mọi `_persist_record`/`_delete_persisted`/
              `_maybe_auto_retry` mới sẽ no-op ngay cả khi tồn tại task nào
              chưa kịp cancel (guard bảo vệ khỏi shutdown race).
            - Task delayed-requeue đang sleep bị cancel.
            - Task scheduler/cleanup bị cancel.
            - **Handler task in-flight** cũng bị cancel VÀ được `await` với
              `return_exceptions=True` trước khi return. Đây là fix cho lỗi
              `sqlite3.ProgrammingError: Cannot operate on a closed database`
              trong `_persist_record` sau khi main.py đóng `db_engine`. Nếu
              không await ở đây, handler_task đang chạy sẽ tiếp tục gọi
              `_persist_record` sau khi connection đã close.

        Idempotent: gọi 2 lần vẫn OK — lần 2 flag đã set, các task đã done,
        `gather` với list rỗng return ngay.
        """
        self._shutting_down = True

        # Thu thập TẤT CẢ task cần chờ trước khi cancel để `gather` bên
        # dưới bao phủ toàn bộ. Handler task đóng vai trò quan trọng nhất:
        # đây là task đang chạy 12-step flow, gọi `_persist_record` nhiều
        # lần trong vòng đời. Nếu shutdown return trước khi handler_task
        # exit, `db_engine.close()` sẽ đóng SQLite connection trong khi
        # handler_task còn cố commit → ProgrammingError.
        tasks_to_await: list[asyncio.Task] = []

        # Cancel toàn bộ task delayed-retry đang chờ.
        for task in list(self._delayed_requeue_tasks.values()):
            if not task.done():
                task.cancel()
                tasks_to_await.append(task)
        self._delayed_requeue_tasks.clear()

        # Cancel auto Check Plus polls (up to 5 min each after QR).
        auto_checks = getattr(self, "_auto_check_plan_tasks", None)
        if auto_checks:
            for task in list(auto_checks.values()):
                if not task.done():
                    task.cancel()
                    tasks_to_await.append(task)
            auto_checks.clear()

        # Cancel scheduler + cleanup.
        for task in (self._scheduler_task, self._cleanup_task):
            if task is not None and not task.done():
                task.cancel()
                tasks_to_await.append(task)
        self._scheduler_task = None
        self._cleanup_task = None

        # Cancel HANDLER TASK in-flight — job đang chạy flow. Handler tự có
        # `finally` block release lease/slot nên cancel an toàn (chỉ mất
        # công `_persist_record` cuối, được guard bởi `_shutting_down` để
        # no-op). Snapshot list trước khi cancel để tránh mutation race
        # khi handler exit chỉnh sửa `_jobs`.
        for record in list(self._jobs.values()):
            task = record.handler_task
            if task is not None and not task.done():
                task.cancel()
                tasks_to_await.append(task)

        # Chờ tất cả — `return_exceptions=True` để CancelledError của từng
        # task không làm `gather` raise; ta chỉ cần đảm bảo task đã kết
        # thúc (done) trước khi lifespan tiếp tục sang `db_engine.close()`.
        # Timeout 5s bảo vệ trường hợp task treo do bug (không muốn kéo
        # shutdown vô hạn).
        if tasks_to_await:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks_to_await, return_exceptions=True),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "shutdown: %d task(s) did not exit within 5s — "
                    "proceeding with db close anyway (_shutting_down guard "
                    "will make any late persist a no-op)",
                    sum(1 for t in tasks_to_await if not t.done()),
                )
