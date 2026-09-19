"""Exception hierarchy chung cho `core/`.

Theo Payment_Module_Boundary (design.md), `core/` KHÔNG được biết tới
`payments/ideal/` (và ngược lại, `payments/ideal/errors.py` có hierarchy
riêng bắt nguồn từ `IdealFlowError`, độc lập hoàn toàn với module này).

`CoreError` là base exception chung cho mọi lỗi phát sinh từ các thành phần
thuộc `core/` (ProxyPool, SettingsRepository, Job_Manager, AccountSessionCache,
DB engine...). Đây KHÔNG phải là base class của `IdealFlowError`.
"""

from __future__ import annotations


class CoreError(Exception):
    """Base exception chung cho mọi lỗi thuộc `core/`.

    KHÔNG kế thừa `IdealFlowError` (thuộc `payments/ideal/`) để giữ đúng
    Payment_Module_Boundary — `core/` không được import bất kỳ thứ gì từ
    `payments/ideal/`.
    """


class ProxyExhaustedError(CoreError):
    """Raise khi `ProxyPool.acquire()` không còn proxy khả dụng.

    Xảy ra khi `proxy.list` không rỗng nhưng toàn bộ proxy trong danh sách
    đã bị `mark_dead` hoặc đã đạt giới hạn `proxy.max_leases_per_proxy`
    (Requirement 9.4) — khác với Direct_Mode (danh sách rỗng, KHÔNG raise).

    Attributes:
        total_proxies: Tổng số proxy có trong `proxy.list` tại thời điểm acquire.
        dead_count: Số proxy đang bị `mark_dead`.
        leased_out_count: Số proxy còn "alive" nhưng đã đạt giới hạn lease
            đồng thời (`proxy.max_leases_per_proxy`).
    """

    def __init__(
        self,
        total_proxies: int,
        dead_count: int,
        leased_out_count: int,
    ) -> None:
        self.total_proxies = total_proxies
        self.dead_count = dead_count
        self.leased_out_count = leased_out_count
        message = (
            "No proxy available: "
            f"total_proxies={total_proxies}, dead_count={dead_count}, "
            f"leased_out_count={leased_out_count}"
        )
        super().__init__(message)


class SettingsValidationError(CoreError):
    """Raise khi `SettingsRepository.set()`/`bulk_set()` vi phạm whitelist hoặc
    type constraint (Requirement 11.3, 11.4).

    Attributes:
        key: Tên key bị từ chối.
        reason: Mô tả lý do vi phạm cụ thể (ví dụ "key không thuộc whitelist",
            "giá trị vượt range [1, 50]", "kiểu dữ liệu mong đợi int") —
            KHÔNG dùng message chung mơ hồ, theo Fail_Fast_Policy.
    """

    def __init__(self, key: str, reason: str) -> None:
        self.key = key
        self.reason = reason
        message = f"Settings key '{key}' is invalid: {reason}"
        super().__init__(message)


class HandlerAlreadyRegisteredError(CoreError):
    """Raise khi `JobManager.register_handler()` được gọi 2 lần cho cùng 1
    `payment_method` (Requirement 13.2, 13.5 — mỗi payment method chỉ đăng ký
    đúng 1 `PaymentFlowHandler` duy nhất trong registry của `Job_Manager`).

    Attributes:
        payment_method: Tên payment method đã bị đăng ký trùng.
    """

    def __init__(self, payment_method: str) -> None:
        self.payment_method = payment_method
        message = (
            f"Payment method '{payment_method}' đã được đăng ký handler trước đó — "
            "mỗi payment method chỉ được đăng ký 1 handler duy nhất."
        )
        super().__init__(message)


class NamespaceAlreadyRegisteredError(CoreError):
    """Raise khi `SettingsRepository.register_namespace()` được gọi 2 lần cho
    cùng 1 namespace (Requirement 13.6 — tránh 2 module tranh nhau đăng ký
    cùng 1 namespace, ví dụ `core/` và `payments/ideal/` cùng đăng ký `ideal`).

    Attributes:
        namespace: Tên namespace đã bị đăng ký trùng.
    """

    def __init__(self, namespace: str) -> None:
        self.namespace = namespace
        message = (
            f"Namespace '{namespace}' đã được đăng ký trước đó — mỗi namespace "
            "chỉ được đăng ký 1 lần duy nhất."
        )
        super().__init__(message)


class JobAlreadyResolvedError(CoreError):
    """Raise khi `JobManager.resolve_pull_outcome()` được gọi cho 1 job đã có
    `pull_outcome` terminal (`"success"`/`"fail"`) từ trước (Requirement 20.6
    — bảo vệ invariant "chỉ tính công đúng 1 lần cho mỗi job Pull_Mode",
    tham chiếu Requirement 22.4/22.7). Fail_Fast: KHÔNG cho phép caller vô
    tình gọi lại resolve trên job đã terminal mà không biết.

    Attributes:
        job_id: `job_id` của job đã ở trạng thái terminal.
    """

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        message = (
            f"Job '{job_id}' đã có pull_outcome terminal (success/fail) từ "
            "trước — không được resolve_pull_outcome lại lần thứ 2 (mỗi job "
            "chỉ được tính công đúng 1 lần)."
        )
        super().__init__(message)


class JobNotFoundError(CoreError):
    """Raise khi `JobManager.claim_pull_account()` / `resolve_pull_outcome()`
    / `record_plus_check_transition()` nhận `job_id` không tồn tại trong
    `_jobs` (Requirement 20.5 — Fail_Fast, KHÔNG silent no-op vì các method
    này quyết định tính công/lương, khác `record_telegram_notification`
    hiện có vốn no-op khi job không tồn tại).

    Attributes:
        job_id: `job_id` không tìm thấy trong `_jobs`.
    """

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        message = f"Job '{job_id}' không tồn tại trong JobManager."
        super().__init__(message)


class ModeSwitchBlockedError(CoreError):
    """Raise khi `JobManager.set_operating_mode()` bị chặn đổi mode vì còn
    job Pull_Mode đang `assigned`/`pending` chưa resolve VÀ request không
    có `force=true` (Requirement 3.1). Route layer bắt exception này để
    trả HTTP 409 kèm danh sách `job_id` đang treo cho Frontend_App hiển thị.

    Attributes:
        job_ids: Danh sách `job_id` đang `pull_assignment_state == "assigned"`
            VÀ `pull_outcome == "pending"` tại thời điểm chặn.
    """

    def __init__(self, job_ids: list[str]) -> None:
        self.job_ids = job_ids
        message = (
            f"Không thể đổi mode: còn {len(job_ids)} job Pull_Mode đang dở "
            f"dang (job_ids={job_ids}). Dùng force=true để tự động kết luận "
            "THẤT BẠI cho các job đó trước khi đổi mode."
        )
        super().__init__(message)
