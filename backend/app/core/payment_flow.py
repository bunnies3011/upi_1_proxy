"""Interface `PaymentFlowHandler` + kiểu dữ liệu chung cho Job_Manager.

Đây là module lõi của `core/` — đảm bảo Payment_Module_Boundary (Requirement
13.2, 13.3): `core/` chỉ biết shape generic này, KHÔNG import bất kỳ gì từ
`app.payments.*`. Mỗi payment module (ví dụ `payments/ideal/`) implement
`PaymentFlowHandler` và tự đăng ký với `Job_Manager` (side-effect khi import
package của payment method đó).

`ProxyLease` thuộc `core/proxy_pool.py` (implement ở task 6.3, sau task này) —
dùng forward-reference string trong signature để tránh import lỗi lúc module
đó chưa tồn tại/circular import.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.core.proxy_pool import ProxyLease


class JobStatus(str, Enum):
    """Trạng thái chung của 1 Job, generic cho mọi payment method (R13.2)."""

    PENDING = "pending"
    RUNNING = "running"
    QR_READY = "qr_ready"
    ERROR = "error"
    STOPPED = "stopped"


@dataclass(frozen=True)
class Job:
    """Mô tả 1 job generic mà Job_Manager quản lý.

    `account_line` giữ nguyên dòng thô người dùng nhập (chưa parse) — việc
    parse thuộc về `PaymentFlowHandler.parse_account_line` của từng payment
    method cụ thể, `core/` không biết định dạng bên trong dòng này.
    """

    job_id: str
    payment_method: str
    account_line: str
    created_at: float
    cancellation_token: "CancellationToken"
    payment_link: str | None = None


class ProxyLeaseHealth(str, Enum):
    """Kết quả health của core `ProxyLease` do handler báo tường minh.

    Handler set field này trên `JobResult` khi đã exercise lease (VD login
    thành công → ALIVE; login transport fail qua proxy → DEAD). `None`
    (default) giữ legacy inference trong `JobManager` (QR_READY → alive,
    network ERROR → dead) cho các payment method cũ chưa migrate.
    """

    ALIVE = "alive"
    DEAD = "dead"


@dataclass(frozen=True)
class JobResult:
    """Kết quả trả về từ `PaymentFlowHandler.run` cho 1 job.

    Attributes:
        status: Trạng thái terminal (QR_READY / ERROR / STOPPED).
        artifact_path: Đường dẫn file PNG QR (chỉ có khi QR_READY).
        error_code: Mã lỗi domain (chỉ có khi ERROR).
        error_message: Chi tiết lỗi (đã redact secrets).
        payment_link: URL thanh toán user mở khi quét QR (chỉ có khi
            QR_READY). Optional — payment method KHÔNG có redirect URL
            (VD UPI native, chỉ QR image) sẽ để None.
        pause_requested: Cờ báo handler tự nguyện dừng SỚM vì
            Push_Success_Gate đã paused (không phải user cancel).
            `JobManager._run_handler` phát hiện flag này → chuyển
            `record.status = PENDING` (giữ chỗ trong queue chờ user
            resume) THAY VÌ terminal STOPPED. `status` vẫn là STOPPED
            khi handler set flag này (vì handler flow return từ mid-flow
            là hành vi "dừng sớm"), wrapper sẽ override sang PENDING.
            Cache side-effect (VD `AccountSessionCache.save()` sau khi
            login xong) đã persist TRƯỚC khi handler check pause →
            resume sẽ dùng lại cache, KHÔNG login lại. Handler PHẢI
            đảm bảo mọi state phụ trợ (cache session, artifact tạm) đã
            được commit trước khi trả pause_requested=True.
        plan: Optional plan hint from the handler when the run itself
            proves plan state without a separate check-plan call
            (e.g. checkout "User is already paid" → `"plus"`). JobManager
            persists `"plus"`/`"free"` onto the job row so Free export
            excludes known-Plus accounts. Other values ignored.
        proxy_lease_health: Optional explicit core-lease health. When set,
            JobManager drives `mark_alive`/`mark_dead` directly and skips
            legacy final-result inference. Default None preserves old
            handlers unchanged.
    """

    status: JobStatus
    artifact_path: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    payment_link: str | None = None
    qr_expires_at: float | None = None
    pause_requested: bool = False
    plan: str | None = None
    proxy_lease_health: ProxyLeaseHealth | None = None


@dataclass(frozen=True)
class ParsedAccount:
    """Marker base rỗng cho kết quả parse 1 dòng account hợp lệ.

    `core/` không biết cấu trúc cụ thể của 1 account theo từng payment
    method — mỗi payment module tự định nghĩa model cụ thể bằng cách kế
    thừa dataclass này (ví dụ `payments/ideal/models.py` sẽ định nghĩa
    `IdealParsedAccount(ParsedAccount)`).
    """

    raw_line: str


@dataclass(frozen=True)
class AccountLineError:
    """Lỗi parse 1 dòng account, dùng chung cho mọi payment method.

    Job_Manager tổng hợp danh sách dòng account bị bỏ qua kèm lý do dựa trên
    kiểu này (Requirement 8.2), không cần biết chi tiết payment method.
    """

    line: str
    reason: str


class CancellationToken(Protocol):
    """Token cho phép Job_Manager/HTTP handler yêu cầu dừng 1 job đang chạy.

    Có 2 cờ độc lập:
        - CANCEL (`is_cancelled`): user chủ động stop/delete → job kết
          thúc terminal STOPPED. Handler PHẢI check `is_cancelled()` định
          kỳ và trả `JobResult(status=STOPPED)` nếu set.
        - PAUSE (`is_paused`): Push_Success_Gate đạt ngưỡng → job đang
          RUNNING cần dừng SỚM SAU checkpoint an toàn (VD sau khi login
          xong, cache session đã lưu) và trả về pending queue chờ user
          resume. Handler PHẢI check `is_paused()` ở các bước sau checkpoint
          "đã có cache" và trả `JobResult(status=STOPPED, pause_requested=True)`.
          Wrapper `_run_handler` sẽ ưu tiên `pause_requested` → chuyển
          record.status = PENDING (giữ chỗ trong queue) thay vì terminal.

    Handler đối xử 2 cờ khác nhau về ngữ nghĩa:
        - Cancel = terminal, không chạy lại tự động.
        - Pause = tạm dừng, chạy lại từ đầu khi user bấm Tiếp tục
          (nhưng đã có cache session → step login được skip).

    `wait_cancelled()` cho phép handler race long await với cancel signal
    (thay vì chỉ poll `is_cancelled()` giữa các bước). Chỉ resolve khi
    `cancel()` — `pause()` KHÔNG unblock.
    """

    def is_cancelled(self) -> bool: ...

    def cancel(self) -> None: ...

    def is_paused(self) -> bool: ...

    def pause(self) -> None: ...

    async def wait_cancelled(self) -> None: ...


class SimpleCancellationToken:
    """Implementation cụ thể, đơn giản của `CancellationToken`.

    Dùng `asyncio.Event` vì toàn bộ hệ thống chạy trên 1 event loop async
    duy nhất (Job_Manager scheduler + FastAPI request handler cùng loop) —
    `cancel()` gọi từ 1 HTTP request handler (ví dụ `DELETE /api/jobs/{id}`)
    và `is_cancelled()` được `PaymentFlowHandler.run` poll định kỳ trong
    cùng event loop, không cần primitive thread-safe.

    2 event riêng biệt (`_event` cancel, `_pause_event`) để cancel và pause
    không đè lên nhau — user có thể cancel job đang pause (chuyển từ
    "chờ resume" → STOPPED) và ngược lại.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._pause_event = asyncio.Event()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    def pause(self) -> None:
        self._pause_event.set()

    async def wait_cancelled(self) -> None:
        """Chờ tới khi `cancel()` được gọi. Không resolve khi chỉ `pause()`."""
        await self._event.wait()


class PaymentFlowHandler(Protocol):
    """Mỗi payment module implement interface này và tự đăng ký với Job_Manager.

    Job_Manager KHÔNG import bất kỳ implementation cụ thể nào."""

    async def run(self, job: Job, proxy_lease: "ProxyLease | None") -> JobResult:
        """Thực thi toàn bộ flow cho 1 job. PHẢI tôn trọng cancellation_token
        (kiểm tra định kỳ, dừng sớm và trả JobResult(status=STOPPED) nếu bị cancel)."""
        ...

    def parse_account_line(self, line: str) -> "ParsedAccount | AccountLineError":
        """Parse 1 dòng account thô thành model cụ thể của payment method này,
        hoặc trả `AccountLineError` kèm lý do nếu dòng không hợp lệ."""
        ...

    def get_max_concurrent_key(self) -> str:
        """Trả về tên key trong Settings_Store quy định concurrency cho payment
        method này (ví dụ `"ideal.max_concurrent"`).

        Job_Manager (thuộc `core/`) đọc giá trị `max_concurrent` qua callback
        này thay vì hardcode tên key theo namespace payment method — đảm bảo
        Payment_Module_Boundary (Requirement 13.2, 13.5): `core/` không biết
        trước tên key thuộc namespace `ideal.*` (hay bất kỳ payment method
        nào khác trong tương lai)."""
        ...
