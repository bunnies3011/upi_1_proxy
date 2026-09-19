"""Điểm tích hợp DUY NHẤT giữa `core/` và `payments/ideal/` (Payment_Module_Boundary — R13).

Module này export 2 hàm public để `main.py` (task 26.1) gọi tại startup:

    - `register_ideal_namespace(settings)`: đăng ký 9 key `ideal.*` vào
      `SettingsRepository` (whitelist Requirement 11.7) + init giá trị mặc
      định cho `ideal.known_issuers` (11 mã IssuerBank, Requirement 6.4) nếu
      key chưa từng được set.

    - `register_ideal_handler(job_manager, settings, session_cache,
      qr_output_dir, logger_factory)`: tạo `IdealFlowHandler` với đầy đủ
      sub-clients (`IdealProfileGenerator`, `DeviceProfileAllocator`,
      `IssuerSelector`, `QrRenderer`) và đăng ký vào `JobManager` qua
      `register_handler("ideal", handler)`.

Payment_Module_Boundary (R13.4, R13.5, R13.6): `core/` KHÔNG import module
này (không có side-effect toàn cục — cả 2 hàm đều nhận singleton `core/`
qua PARAMETER, không tự resolve). `main.py` là điểm duy nhất gọi 2 hàm này
sau khi khởi tạo xong `SettingsRepository` + `JobManager`. Việc chỉ import
package `app.payments.ideal` KHÔNG kích hoạt bất kỳ đăng ký nào — tránh
side-effect ngầm khó truy vết khi test.

Nếu tương lai thêm payment method khác (ví dụ `payments/paypal/`), module
đó tự phơi cặp hàm `register_paypal_namespace` + `register_paypal_handler`
theo cùng convention, và `main.py` chỉ cần import + gọi thêm — `core/`
KHÔNG cần sửa. Đây là cách hiện thực đăng ký theo Requirement 13.4-13.6
mà vẫn giữ Fail_Fast (nếu đăng ký thất bại — ví dụ namespace/handler đã
được đăng ký trước — exception raise ngay tại startup, KHÔNG bị "nuốt" bởi
import-time side-effect).

_Requirements: 6.1, 6.4, 6.5, 6.6, 6.7, 13.4, 13.5, 13.6_
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Final

from app.core.job_manager import JobManager
from app.core.session_cache import AccountSessionCache
from app.core.settings_store import SettingsRepository, TypeConstraint
from app.payments.ideal.device_profile import DeviceProfileAllocator
from app.payments.ideal.flow import IdealFlowHandler
from app.payments.ideal.issuer_selector import IssuerSelector
from app.payments.ideal.profile_generator import IdealProfileGenerator
from app.payments.ideal.qr_renderer import QrRenderer

__all__ = [
    "register_ideal_namespace",
    "register_ideal_handler",
]

# ---------------------------------------------------------------------------
# Hằng số nội bộ
# ---------------------------------------------------------------------------

#: Tên namespace `ideal` — dot-separated key format `ideal.<field>` (R11.7).
_NAMESPACE: Final[str] = "ideal"

#: Payment method key đăng ký trong `JobManager` registry — Requirement 13.4,
#: 13.5. Tên khớp Job.payment_method mà API layer dùng khi submit_batch.
_PAYMENT_METHOD: Final[str] = "ideal"

#: Danh sách 11 mã IssuerBank khởi tạo whitelist `ideal.known_issuers`
#: (Requirement 6.4). Chỉ ghi vào Settings_Store khi key chưa từng được set
#: — không ghi đè giá trị user đã sửa. Nếu tương lai user muốn bổ sung thêm
#: mã, họ tự cập nhật qua Settings API; module này KHÔNG merge default vào
#: giá trị hiện tại (dễ nhầm với reset ngầm).
_DEFAULT_KNOWN_ISSUERS: Final[tuple[str, ...]] = (
    "ASNBNL21",
    "RABONL2U",
    "ABNANL2A",
    "KNABNL2H",
    "BITSNL2A",
    "RBRBNL21",
    "BUNQNL2A",
    "TRIONL2U",
    "FVLBNL22",
    "NTSBDEB1",
    "REVOLT21",
)

#: Type constraint cho 9 key `ideal.*` — khớp bảng whitelist Requirement 11.7.
#:
#: Ghi chú thiết kế:
#:  - `ideal.default_issuer` dùng `type="string"` KHÔNG có `enum` cứng vì tập
#:    giá trị hợp lệ = whitelist `ideal.known_issuers` (mutable, user cấu
#:    hình được). Validate "thuộc `known_issuers`" nằm ở API layer khi ghi,
#:    KHÔNG dùng `TypeConstraint.enum` — nếu dùng enum sẽ đông cứng danh
#:    sách tại thời điểm register_namespace, phá vỡ khả năng user mở rộng
#:    `known_issuers` qua Settings API (R6.5).
#:  - `ideal.device_profiles` dùng `type="list_object"` KHÔNG khai
#:    `item_schema.required_keys` — validate shape đầy đủ + locale NL
#:    (`language == "nl-NL"`, `timeZone == "Europe/Amsterdam"`) do
#:    `DeviceProfileAllocator.validate_profile()` (R5.2) thực hiện tại API
#:    layer khi ghi. `TypeConstraint.item_schema` chỉ kiểm shape cơ bản, không
#:    đủ ngữ nghĩa locale — tách bạch trách nhiệm giúp validate-message rõ
#:    ràng hơn.
#:  - `min=0.001` cho các key `number > 0` (không dùng `min=0` vì Requirement
#:    yêu cầu "dương thực sự", `min=0` cho phép 0 gây chia-0/no-op khi làm
#:    delay/timeout).
_IDEAL_KEY_CONSTRAINTS: Final[dict[str, TypeConstraint]] = {
    "default_issuer": TypeConstraint(type="string"),
    "known_issuers": TypeConstraint(type="list_str"),
    "max_concurrent": TypeConstraint(type="int", min=1, max=200),
    "refresh_poll_max_attempts": TypeConstraint(type="int", min=1),
    "refresh_poll_delay_seconds": TypeConstraint(type="number", min=0.001),
    "stripe_request_timeout_seconds": TypeConstraint(type="number", min=0.001),
    "stripe_max_retry_attempts": TypeConstraint(type="int", min=1),
    "stripe_retry_backoff_seconds": TypeConstraint(type="number", min=0.001),
    "device_profiles": TypeConstraint(type="list_object"),
    # ---- Auto-retry cho job kết thúc với error_code "blocked" (tham khảo
    # gpt_signup_hybrid/web/manager.py::_maybe_auto_retry). Khi handler
    # trả về ERROR với error_code thuộc danh sách `auto_retry_blocked_codes`,
    # JobManager tự động lịch requeue job đó sau `delay * retry_count` giây
    # (backoff tuyến tính), tối đa `max` lần. Preserve `job_id` + `order`
    # nên FE thấy job "tự chạy lại" không đẻ dòng mới.
    # Bounds cho auto-retry — nới rộng hơn UPI (hardcap 10) để cho
    # phép retry hàng trăm lần trong trường hợp account bị chặn tạm
    # thời nhưng có thể unblock đột ngột (proxy rotation, IP cooldown).
    #
    # Formula backoff (xem `JobManager._maybe_auto_retry`):
    #   delay = min(base × retry_count, base × CAP_MULTIPLIER)
    # với CAP_MULTIPLIER=3 hardcoded — retry đầu tăng dần, đạt 3×base
    # thì cap giữ nguyên. Cho phép retry hàng trăm lần mà không leo
    # thời gian chờ vô hạn (khác `delay × retry_count` thuần của UPI).
    #
    # Ví dụ với base=15s, max=100 → tổng ~74 phút; base=10s, max=300
    # → tổng ~2.5h. Điều chỉnh 2 tham số theo tolerance thực tế của
    # anti-fraud (proxy rotation nhanh → base thấp, IP burn lâu → base cao).
    "auto_retry_blocked_enabled": TypeConstraint(type="bool"),
    "auto_retry_blocked_max": TypeConstraint(type="int", min=1, max=1000),
    "auto_retry_blocked_delay_seconds": TypeConstraint(
        type="number", min=5.0, max=300.0
    ),
    "auto_retry_blocked_codes": TypeConstraint(type="list_str", min=1),
    # Toggle "failed → pending" — xem seed default bên dưới cho semantics.
    "auto_retry_failed_to_pending": TypeConstraint(type="bool"),
    # Mode retry — quyết định thứ tự / cách requeue khi 1 job kết thúc
    # ERROR với code thuộc `auto_retry_blocked_codes`:
    #   - `"same_account"` (behavior gốc UPI-style): sleep(delay × count,
    #     cap 3×delay) rồi requeue CÙNG account → scheduler chạy lại
    #     ngay account đó khi có slot. Phù hợp anti-fraud cần cooldown
    #     rõ ràng cho từng account.
    #   - `"round_robin"` (default mới theo user request): KHÔNG sleep,
    #     append thẳng vào cuối `_pending_order` → scheduler chạy các
    #     account khác trước, đến khi hết list mới quay lại. Phù hợp khi
    #     user muốn "chạy hết list 1 vòng rồi retry các fail lại từ đầu".
    "auto_retry_mode": TypeConstraint(
        type="string", enum=("same_account", "round_robin")
    ),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def register_ideal_namespace(settings: SettingsRepository) -> None:
    """Đăng ký namespace `ideal.*` (9 key) vào `SettingsRepository`.

    Được `main.py` gọi ĐÚNG 1 LẦN tại startup, sau khi `SettingsRepository`
    đã khởi tạo (nhưng trước khi Backend nhận request hoặc `JobManager` bắt
    đầu dispatch job).

    Hai bước:

        1. `settings.register_namespace("ideal", _IDEAL_KEY_CONSTRAINTS)`
           — nếu namespace đã đăng ký, `SettingsRepository` sẽ raise
           `NamespaceAlreadyRegisteredError` (Fail_Fast, R13.6).

        2. Nếu `ideal.known_issuers` chưa từng được set (`settings.get()`
           trả về `None`) → ghi tuple default 11 mã (Requirement 6.4). Nếu
           đã có giá trị (kể cả list rỗng — user chủ động clear), KHÔNG
           ghi đè. `SettingsRepository.set()` sẽ tự validate list_str với
           `_IDEAL_KEY_CONSTRAINTS["known_issuers"]` trước khi ghi — nếu
           default đã hợp lệ theo constraint thì write luôn thành công.

    Args:
        settings: `SettingsRepository` singleton của Backend, đã khởi tạo
            xong (DB engine sẵn sàng, các namespace core `proxy`/`session_cache`/
            `web` đã tự đăng ký trong constructor của Repository).

    Raises:
        NamespaceAlreadyRegisteredError: `ideal` đã được đăng ký từ trước —
            gọi hàm này quá 1 lần (bug tại `main.py` hoặc test setup),
            KHÔNG suppress vì đây là dấu hiệu setup sai (R13.6).
        SettingsValidationError: Khi ghi default `known_issuers` mà 11 mã
            không khớp constraint — trường hợp không thể xảy ra với default
            tuple hiện tại, nhưng KHÔNG catch để giữ Fail_Fast nếu ai đó
            sửa `_DEFAULT_KNOWN_ISSUERS` thành giá trị sai kiểu.
    """
    settings.register_namespace(_NAMESPACE, _IDEAL_KEY_CONSTRAINTS)

    # Seed defaults cho toàn bộ key `ideal.*` — chỉ ghi khi `None` (chưa
    # từng set) để KHÔNG đè giá trị user chủ động thay đổi qua Settings UI
    # / API (kể cả giá trị `False` hoặc list rỗng — coi là user quyết
    # định đã set thành có chủ đích).
    #
    # Các giá trị dưới đây khớp cấu hình mặc định user muốn cho tool
    # (concurrency/retry/timeout tính từ throughput mong đợi + tolerance
    # với anti-fraud). Nếu tương lai đổi default, chỉ cần sửa dict này —
    # DB có sẵn giá trị cũ vẫn giữ nguyên qua restart.
    _ideal_defaults: dict[str, object] = {
        # ---- Issuer whitelist + issuer mặc định (R6.4, R6.2) ----------
        "ideal.known_issuers": list(_DEFAULT_KNOWN_ISSUERS),
        # `RABONL2U` (Rabobank) — issuer phổ biến nhất NL, chọn cho
        # QR có tỉ lệ chuyển đổi cao nhất theo empirical stats.
        "ideal.default_issuer": "RABONL2U",

        # ---- Concurrency + poll cadence -------------------------------
        # Job song song tối đa. 20 cho 5 proxy × 4 slot mỗi proxy (theo
        # `proxy.max_leases_per_proxy=10` mặc định, ~50 slot khả dụng
        # nếu user set) — tránh oversubscribe khi 1 proxy chết cả pool
        # đè lên số ít còn lại.
        "ideal.max_concurrent": 20,

        # `refresh_poll` — Stripe attach setup_intent.next_action async
        # sau confirm. `max_attempts=20 × delay=1s` = 20s wall-clock,
        # đủ margin cho Stripe queue chậm vẫn trong ngưỡng user chịu
        # được (job không kẹt vô hạn).
        "ideal.refresh_poll_max_attempts": 20,
        "ideal.refresh_poll_delay_seconds": 1.0,

        # ---- Stripe timeout + retry -----------------------------------
        # 60s cho mỗi HTTP request Stripe — Cloudflare edge có latency
        # spike đến ~30s trong giờ cao điểm, giữ ceiling 2× để không
        # bỏ cuộc sớm.
        "ideal.stripe_request_timeout_seconds": 60.0,
        # 100 lần retry cho status 5xx — kết hợp `backoff=1s` (linear
        # với retry_count) → worst-case wait ~50min cho 1 request khi
        # Stripe sập kéo dài. Hiếm khi tới attempt cuối vì transient
        # 5xx thường hồi sau vài giây.
        "ideal.stripe_max_retry_attempts": 100,
        "ideal.stripe_retry_backoff_seconds": 1.0,

        # ---- Device profile (R5.1, R5.2) — locale NL cố định ---------
        # 1 profile duy nhất để giữ fingerprint predictable. Nếu tương
        # lai muốn rotate resolution/timezone, user append thêm entry
        # qua Settings UI — allocator random pick 1 profile mỗi job.
        "ideal.device_profiles": [
            {
                "language": "nl-NL",
                "timeZone": "Europe/Amsterdam",
                "screenWidth": 1920,
                "screenHeight": 1080,
                "screenAvailableWidth": 1920,
                "screenAvailableHeight": 1055,
                "colorDepth": 24,
            }
        ],

        # ---- Auto-retry blocked -----------------------------------------
        # max=3: nếu retry 3 lần vẫn fail thì thường là lỗi bền vững
        # (account chết, IP burn, upstream down) — retry thêm chỉ đốt
        # proxy/thời gian. Muốn "cày mãi tới khi qua" thì bật
        # `auto_retry_failed_to_pending` (default True) — sau exhaust
        # sẽ đá về pending, reset counter, loop tiếp.
        "ideal.auto_retry_blocked_enabled": True,
        "ideal.auto_retry_blocked_max": 3,
        "ideal.auto_retry_blocked_delay_seconds": 10.0,
        # Sau khi hết `auto_retry_blocked_max` lượt vẫn fail: True (mặc
        # định) = job quay về PENDING + reset `retry_count=0` → scheduler
        # nhặt lại vòng mới; False = job kết thúc ERROR (hành vi cũ, user
        # tự bấm "Retry failed" bulk). Nguy cơ loop vô hạn nếu ON và lỗi
        # bền vững — user đã confirm chấp nhận trade-off.
        "ideal.auto_retry_failed_to_pending": True,
        # Mặc định "round_robin" theo yêu cầu user: fail thì chuyển
        # account tiếp theo, hết list mới quay lại — thay vì sleep tại
        # chỗ như mode "same_account" cũ.
        "ideal.auto_retry_mode": "round_robin",
        # Mặc định retry các error_code thuộc nhóm "transient anti-fraud
        # / infra flakiness" — retry có xác suất qua khi đổi proxy hoặc
        # chờ cooldown vài chục giây. Không retry các code permanent
        # (approve_needs_review — cần KYC, invalid_credential — sai
        # password lock account, cancelled — user chủ động dừng...).
        "ideal.auto_retry_blocked_codes": [
            # ChatGPT anti-fraud chặn account/IP tạm thời.
            "approve_blocked",
            # Stripe risk engine chặn confirm với `rejected_field=payment_method`
            # (không phải lỗi address xấu — profile_generator sinh random NL
            # address mỗi lần, cần retry đổi proxy/timing).
            "address_validation_rejected",
            # Stripe chậm attach `setup_intent.next_action.redirect_to_url`
            # sau khi approve — poll hết attempts vẫn chưa thấy → retry
            # có ích khi Stripe queue tắc.
            "refresh_poll_exhausted",
            "stripe_next_action_missing",
            # Stripe trả JSON hỏng / status không hợp lệ → nhiều khả năng
            # do network drop giữa chừng.
            "confirm_invalid_response",
            # Đã retry sẵn ở stripe layer 3 lần vẫn fail (5xx transient
            # hoặc timeout kéo dài) → retry job cho phép mạng hồi.
            "stripe_retry_exhausted",
            # Stripe confirm timeout/network fail — proxy die giữa chừng
            # hoặc api.stripe.com slow. `confirm` KHÔNG retry ngầm (R3.5
            # tránh double-post), nhưng auto-retry ở tầng job với proxy
            # mới có xác suất qua cao.
            "stripe_confirm_transport",
            # Stripe pm-redirects (bước 10 follow_redirect) timeout — cùng
            # nguyên nhân với `stripe_confirm_transport`.
            "stripe_follow_redirect_transport",
            # Stripe `POST /v1/payment_pages/{id}` (step 6b — update
            # tax_region) timeout/network fail — proxy chết giữa chừng
            # hoặc api.stripe.com slow. `update_billing` không idempotent
            # (commit state) nên KHÔNG retry ngầm, nhưng auto-retry ở
            # tầng job với proxy mới có xác suất qua cao.
            "stripe_update_billing_transport",
            # pay.ideal.nl trả 4xx/5xx khi lấy cookie tx_api_token — hiếm
            # nhưng chắc chắn transient.
            "pay_ideal_page_http_error",
            # Response `/api/v1/transactions/{id}/initiate` thiếu qrCodeUrl —
            # race condition rất hiếm, có thể tự khỏi sau vài giây.
            "qr_code_url_missing",
        ],
    }
    for key, default_value in _ideal_defaults.items():
        if await settings.get(key) is None:
            await settings.set(key, default_value)


def register_ideal_handler(
    job_manager: JobManager,
    settings: SettingsRepository,
    session_cache: AccountSessionCache,
    qr_output_dir: Path,
    logger_factory: Callable[[str], logging.Logger] = logging.getLogger,
) -> IdealFlowHandler:
    """Tạo `IdealFlowHandler` + đăng ký vào `JobManager` cho payment method `"ideal"`.

    Được `main.py` gọi ĐÚNG 1 LẦN tại startup, sau
    `register_ideal_namespace()` (vì `IdealFlowHandler.run()` sẽ đọc
    `ideal.*` từ Settings_Store — namespace phải đăng ký trước).

    Hàm khởi tạo:
        - `IdealProfileGenerator` (stateless, no arg)
        - `DeviceProfileAllocator(settings)` — cần `SettingsRepository` để
          đọc live `ideal.device_profiles` (R5.1)
        - `IssuerSelector` (stateless, no arg)
        - `QrRenderer` (stateless, no arg)
        - `IdealFlowHandler` (composition của tất cả sub-clients trên +
          `settings` + `session_cache` + `logger_factory` + `qr_output_dir`)

    Sau đó gọi `job_manager.register_handler("ideal", handler)` — nếu đã có
    handler cho `ideal`, `JobManager` sẽ raise `HandlerAlreadyRegisteredError`
    (Fail_Fast, R13.5).

    Trả về instance `IdealFlowHandler` đã đăng ký — hữu ích cho test/integration
    cần reference handler trực tiếp (ví dụ để inject fake HTTP session).
    `main.py` production thường bỏ qua giá trị trả về.

    Args:
        job_manager: `JobManager` singleton (thuộc `core/`, do `main.py`
            tạo trước).
        settings: `SettingsRepository` singleton — đã được
            `register_ideal_namespace()` gọi trước đó ở startup.
        session_cache: `AccountSessionCache` singleton (thuộc `core/`).
        qr_output_dir: Thư mục runtime ghi file QR PNG (`runtime/qr` theo
            convention project). `IdealFlowHandler.run()` sẽ ghi file
            `<job_id>.png` vào đây (R7.3). Không tự tạo dir ở đây — `QrRenderer`
            tự `mkdir(parents=True, exist_ok=True)` khi ghi file lần đầu.
        logger_factory: Callable `str -> logging.Logger` để tạo logger per-job.
            Mặc định `logging.getLogger` — inject để test dễ mock (ví dụ
            capture log qua `MemoryHandler`) mà không cần patch stdlib.

    Returns:
        Instance `IdealFlowHandler` đã đăng ký vào `JobManager` với key
        `"ideal"`.

    Raises:
        HandlerAlreadyRegisteredError: `job_manager` đã có handler cho
            payment method `"ideal"` — gọi hàm này quá 1 lần (bug setup),
            KHÔNG suppress.
        HandlerContractError: `IdealFlowHandler` thiếu 1 trong 3 method bắt
            buộc của `PaymentFlowHandler` (`run` / `parse_account_line` /
            `get_max_concurrent_key`) — chỉ xảy ra nếu ai đó refactor
            `IdealFlowHandler` bỏ method contract. `JobManager` duck-type
            check tại thời điểm đăng ký, Fail_Fast trước khi có job chạy.
    """
    profile_generator = IdealProfileGenerator()
    device_profile_allocator = DeviceProfileAllocator(settings)
    issuer_selector = IssuerSelector()
    qr_renderer = QrRenderer()

    handler = IdealFlowHandler(
        settings=settings,
        session_cache=session_cache,
        profile_generator=profile_generator,
        device_profile_allocator=device_profile_allocator,
        issuer_selector=issuer_selector,
        qr_renderer=qr_renderer,
        logger_factory=logger_factory,
        qr_output_dir=qr_output_dir,
    )

    job_manager.register_handler(_PAYMENT_METHOD, handler)
    return handler
