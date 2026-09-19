"""Domain models cho luồng nghiệp vụ iDEAL (`payments/ideal/`).

Toàn bộ model là `@dataclass(frozen=True)` — data holder thuần, KHÔNG chứa
logic parse bên trong. Mọi logic parse/validate response nằm ở các hàm
`parse_*` module-level ngay dưới definition của model tương ứng, để:

1. Dễ test độc lập (function thuần vs method).
2. Dễ tái sử dụng khi có nhiều nguồn dữ liệu tương đương cho cùng 1 model.
3. Fail_Fast_Policy tường minh (R1.7, R2.2, R2.4, R5.4, R5.6, R5.7): thiếu
   field bắt buộc → raise `RequiredFieldMissingError` với `field=` và
   `model=` cụ thể để log realtime định danh CHÍNH XÁC field bị thiếu.

Payment_Module_Boundary (Requirement 13.1, 13.6): chỉ import
`app.core.payment_flow` (interface + base type) và `app.payments.ideal.errors`
(exception hierarchy iDEAL). KHÔNG import gì khác từ `app.core.*` (không
cần Settings_Store, ProxyPool, DB engine — models thuần data).

_Requirements: 1.6, 1.7, 2.2, 2.4, 5.4, 5.7_
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from app.payments._chatgpt.models import (
    ChatgptAccount,
    SessionBundle,
    parse_account_line,
)
from app.payments.ideal.errors import RequiredFieldMissingError

# ---------------------------------------------------------------------------
# Requirement 1 — ParsedAccount + parser (re-exported from shared module)
# ---------------------------------------------------------------------------
#
# `SessionBundle`, `parse_account_line`, and the parsed-account model now live
# in the shared ChatGPT module (`payments/_chatgpt/models.py`) so the UPI flow
# can reuse them without importing `payments/ideal/`. Kept re-exported here so
# every existing iDEAL call site/test that imports them from `ideal.models`
# stays green. `IdealParsedAccount` is an alias for the single shared
# `ChatgptAccount` — one source of truth, no duplicate model.

IdealParsedAccount = ChatgptAccount


# ---------------------------------------------------------------------------
# Requirement 1 — CheckoutSession + parser
# ---------------------------------------------------------------------------

_CHECKOUT_SESSION_MODEL_NAME: Final[str] = "CheckoutSession"

#: 3 field bắt buộc theo R1.7 — thiếu 1 trong 3 → Fail_Fast_Policy.
_CHECKOUT_SESSION_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "checkout_session_id",
    "publishable_key",
    "processor_entity",
)


@dataclass(frozen=True)
class CheckoutSession:
    """Kết quả `POST chatgpt.com/backend-api/payments/checkout` (Requirement
    1.5, 1.6). Các field ngoài 3 field bắt buộc (R1.7) có default an toàn —
    thiếu chúng KHÔNG chặn flow.
    """

    checkout_session_id: str
    publishable_key: str
    processor_entity: str
    client_secret: str = ""
    status: str = ""
    payment_status: str = ""
    requires_manual_approval: bool = False


def parse_checkout_session(payload: dict[str, Any]) -> CheckoutSession:
    """Parse response `checkout` thành `CheckoutSession`.

    Fail_Fast_Policy (Requirement 1.7): thiếu 1 trong 3 field bắt buộc
    (`checkout_session_id`, `publishable_key`, `processor_entity`) → raise
    `RequiredFieldMissingError` với `field=<tên field>` để log realtime
    định danh chính xác (Property 3).

    Args:
        payload: Dict JSON đã parse từ response HTTP thành công. Non-None.
    """
    for required in _CHECKOUT_SESSION_REQUIRED_FIELDS:
        if not payload.get(required):
            raise RequiredFieldMissingError(
                field=required,
                model=_CHECKOUT_SESSION_MODEL_NAME,
            )

    return CheckoutSession(
        checkout_session_id=payload["checkout_session_id"],
        publishable_key=payload["publishable_key"],
        processor_entity=payload["processor_entity"],
        client_secret=payload.get("client_secret", "") or "",
        status=payload.get("status", "") or "",
        payment_status=payload.get("payment_status", "") or "",
        requires_manual_approval=bool(payload.get("requires_manual_approval", False)),
    )


# ---------------------------------------------------------------------------
# Requirement 2 — StripePaymentPageInit + parser
# ---------------------------------------------------------------------------

_STRIPE_PAYMENT_PAGE_INIT_MODEL_NAME: Final[str] = "StripePaymentPageInit"

_STRIPE_PAYMENT_PAGE_INIT_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "init_checksum",
    "config_id",
)


@dataclass(frozen=True)
class StripePaymentPageInit:
    """Kết quả `POST api.stripe.com/v1/payment_pages/{id}/init` (Requirement
    2.1). ``init_checksum`` + ``config_id`` bắt buộc theo R2.2 — thiếu 1
    field → Fail_Fast.

    Attributes:
        init_checksum: Bắt buộc R2.2 — dùng làm field ``init_checksum`` trong
            request ``confirm`` kế tiếp.
        config_id: Bắt buộc R2.2 — checkout config UUID.
        amount: Optional, default 0. Extract theo priority
            ``elements_options.amount`` → ``total_summary.due`` →
            ``total_summary.total`` → ``invoice.amount_due`` →
            ``invoice.total`` → ``amount_total``. Nếu không có → 0.
        page_id: Optional, default "". Value của ``id`` field top-level trong
            response ``init`` (format ``ppage_<hex>``). Dùng làm input
            ``compute_js_checksum(page_id, shift)`` cho request ``confirm``
            (Stripe.js obfuscated token — Requirement 3.3).
    """

    init_checksum: str
    config_id: str
    amount: int = 0
    page_id: str = ""


def _extract_amount(payload: dict[str, Any]) -> int:
    """Extract ``amount`` từ Stripe init response — port từ UPI reference.

    Amount là integer cents. Nếu không tìm thấy ở bất kỳ path nào → 0
    (safe default, Stripe tự infer theo checkout_session_id).
    """
    # Priority 1: elements_options.amount
    eo = payload.get("elements_options")
    if isinstance(eo, dict):
        amt = eo.get("amount")
        if isinstance(amt, int):
            return amt

    # Priority 2: total_summary.due / total
    ts = payload.get("total_summary")
    if isinstance(ts, dict):
        for k in ("due", "total"):
            amt = ts.get(k)
            if isinstance(amt, int):
                return amt

    # Priority 3: invoice.amount_due / total
    inv = payload.get("invoice")
    if isinstance(inv, dict):
        for k in ("amount_due", "total"):
            amt = inv.get(k)
            if isinstance(amt, int):
                return amt

    # Priority 4: amount_total top-level
    amt = payload.get("amount_total")
    if isinstance(amt, int):
        return amt

    return 0


def parse_stripe_payment_page_init(payload: dict[str, Any]) -> StripePaymentPageInit:
    """Parse response `init` thành `StripePaymentPageInit` (Requirement 2.2)."""
    for required in _STRIPE_PAYMENT_PAGE_INIT_REQUIRED_FIELDS:
        if not payload.get(required):
            raise RequiredFieldMissingError(
                field=required,
                model=_STRIPE_PAYMENT_PAGE_INIT_MODEL_NAME,
            )

    page_id_raw = payload.get("id")
    page_id = page_id_raw if isinstance(page_id_raw, str) else ""
    return StripePaymentPageInit(
        init_checksum=payload["init_checksum"],
        config_id=payload["config_id"],
        amount=_extract_amount(payload),
        page_id=page_id,
    )


# ---------------------------------------------------------------------------
# Requirement 2 — StripeElementsSession + parser
# ---------------------------------------------------------------------------

_STRIPE_ELEMENTS_SESSION_MODEL_NAME: Final[str] = "StripeElementsSession"

_STRIPE_ELEMENTS_SESSION_REQUIRED_FIELDS: Final[tuple[str, ...]] = ("session_id",)


@dataclass(frozen=True)
class StripeElementsSession:
    """Kết quả `GET api.stripe.com/v1/elements/sessions` (Requirement 2.3).
    Chỉ có `session_id` là bắt buộc theo R2.4.

    ``config_id`` (optional) — Stripe response thường có sẵn, cần cho
    ``payment_method_data[client_attribution_metadata][elements_session_config_id]``
    ở request ``confirm`` kế tiếp. Nếu Stripe không trả → empty string
    (Stripe accept empty ở 1 vài luồng, nhưng thường raise 400 —
    ``_extract_amount``-style priority không áp dụng ở đây, chỉ 1 nguồn).
    """

    session_id: str
    config_id: str = ""


def parse_stripe_elements_session(payload: dict[str, Any]) -> StripeElementsSession:
    """Parse response `elements/sessions` (Requirement 2.4)."""
    for required in _STRIPE_ELEMENTS_SESSION_REQUIRED_FIELDS:
        if not payload.get(required):
            raise RequiredFieldMissingError(
                field=required,
                model=_STRIPE_ELEMENTS_SESSION_MODEL_NAME,
            )

    config_id = payload.get("config_id") or ""
    return StripeElementsSession(
        session_id=payload["session_id"],
        config_id=config_id if isinstance(config_id, str) else "",
    )


# ---------------------------------------------------------------------------
# Requirement 3 — BillingAddress (Glossary — sinh bởi IdealProfileGenerator)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BillingAddress:
    """Thông tin billing gửi kèm request `confirm` (Requirement 3.2).

    Cấu trúc khớp Glossary + payload thực tế của Stripe:
    `{name, email, address: {country: "NL", line1, line2, city,
    postal_code, state}}`. Sub-dict `address` là nested, KHÔNG flatten
    thành `address_country`/`address_line1`/... — giữ nguyên shape wire để
    `stripe_client.confirm()` không phải remap trước khi serialize.

    KHÔNG có parser — đây là model do IdealProfileGenerator tự build từ
    email + locale NL (Requirement 3.1), không parse từ response external.
    """

    name: str
    email: str
    address: dict[str, str]


# ---------------------------------------------------------------------------
# Requirement 5 — DeviceProfile + validator (locale NL)
# ---------------------------------------------------------------------------

#: Locale bắt buộc cho DeviceProfile theo R5.2 — nhất quán với billing NL.
_REQUIRED_LANGUAGE: Final[str] = "nl-NL"
_REQUIRED_TIME_ZONE: Final[str] = "Europe/Amsterdam"


@dataclass(frozen=True)
class DeviceProfile:
    """Bộ giá trị `deviceInfo` gửi kèm request `initiate` tới `pay.ideal.nl`
    (Requirement 5.1). Cấu trúc field khớp CHÍNH XÁC wire format camelCase
    của endpoint (không snake_case) — `TransactionClient` serialize trực
    tiếp không cần remap.
    """

    language: str
    timeZone: str
    screenWidth: int
    screenHeight: int
    screenAvailableWidth: int
    screenAvailableHeight: int
    colorDepth: int


def validate_device_profile(profile: dict[str, Any]) -> None:
    """Validate 1 DeviceProfile hợp lệ theo Requirement 5.2 tại thời điểm
    LƯU vào Settings_Store — chạy khi user cập nhật `ideal.device_profiles`
    qua Frontend_App, KHÔNG chạy lại mỗi lần `pick_for_job()`.

    Raise `ValueError` (KHÔNG phải `IdealFlowError`) vì đây là lỗi VALIDATE
    SETTINGS ở boundary API/Settings_Store, không phải lỗi trong luồng flow
    đang chạy — `IdealFlowError` dành riêng cho lỗi phát sinh khi 1 IdealJob
    thực thi (Requirement 14.1, 14.3).

    Args:
        profile: Dict candidate (chưa nhất thiết là `DeviceProfile` đã
            dataclass hoá) — Settings_Store nhận từ user dưới dạng dict/JSON.

    Raises:
        ValueError: Nếu `language != "nl-NL"` hoặc
            `timeZone != "Europe/Amsterdam"`.
    """
    language = profile.get("language")
    if language != _REQUIRED_LANGUAGE:
        raise ValueError(
            f"DeviceProfile.language must be '{_REQUIRED_LANGUAGE}' to stay "
            f"consistent with NL locale (Requirement 5.2), got: {language!r}."
        )
    time_zone = profile.get("timeZone")
    if time_zone != _REQUIRED_TIME_ZONE:
        raise ValueError(
            f"DeviceProfile.timeZone must be '{_REQUIRED_TIME_ZONE}' to stay "
            f"consistent with NL locale (Requirement 5.2), got: {time_zone!r}."
        )


# ---------------------------------------------------------------------------
# Requirement 5 — IssuerBank + parser
# ---------------------------------------------------------------------------

_ISSUER_BANK_MODEL_NAME: Final[str] = "IssuerBank"

_ISSUER_BANK_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "deeplinkType",
    "deeplink",
    "availabilityStatus",
)


@dataclass(frozen=True)
class IssuerBank:
    """1 ngân hàng phát hành iDEAL trong `supportedIssuers[]` (Glossary +
    Requirement 5.7). Field theo wire format camelCase của `pay.ideal.nl`.
    """

    id: str
    deeplinkType: str
    deeplink: str
    availabilityStatus: str


def parse_issuer_bank(payload: dict[str, Any]) -> IssuerBank:
    """Parse 1 phần tử `supportedIssuers[]` thành `IssuerBank` với đầy đủ
    4 field bắt buộc (Requirement 5.7).

    Fail_Fast_Policy: thiếu 1 field → raise `RequiredFieldMissingError` với
    `model="IssuerBank"` — caller (parse_ideal_transaction_initiate) để
    exception propagate ra `IdealFlowHandler` xử lý ở boundary duy nhất.
    """
    for required in _ISSUER_BANK_REQUIRED_FIELDS:
        # `availabilityStatus` có thể là string non-empty như "UNAVAILABLE" —
        # falsy check bằng `not payload.get()` sẽ đúng vì chỉ "" mới coi là
        # thiếu; string non-empty (dù nội dung "UNAVAILABLE") vẫn pass.
        if not payload.get(required):
            raise RequiredFieldMissingError(
                field=required,
                model=_ISSUER_BANK_MODEL_NAME,
                step="initiate",  # R5.7 nằm trong bước initiate.
            )

    return IssuerBank(
        id=payload["id"],
        deeplinkType=payload["deeplinkType"],
        deeplink=payload["deeplink"],
        availabilityStatus=payload["availabilityStatus"],
    )


# ---------------------------------------------------------------------------
# Requirement 5 — IdealTransactionInitiate + parser
# ---------------------------------------------------------------------------

_IDEAL_TRANSACTION_INITIATE_MODEL_NAME: Final[str] = "IdealTransactionInitiate"


@dataclass(frozen=True)
class IdealTransactionInitiate:
    """Kết quả `POST pay.ideal.nl/api/v1/transactions/{encoded_tx_url}/initiate`
    (Requirement 5.4).

    `amount` giữ union `str | int` vì response `pay.ideal.nl` có thể trả
    string dạng thập phân ("10.99") hoặc int (cents) — Backend_Service
    không parse thành số ở tầng model (giữ nguyên wire format, chỉ log/
    hiển thị lên UI).

    `supportedIssuers` là list các `IssuerBank` đã parse — validate
    non-empty theo Requirement 5.6.

    `qrCodeUrl` là URL iDEAL 2.0 universal (`https://tx.ideal.nl/2/{tx_id}
    ?sig=<sig>`) được backend `pay.ideal.nl` trả trong response
    `/initiate`. Đây LÀ NỘI DUNG QR code hiển thị trên trang thanh toán —
    encode chuỗi này thành QR PNG rồi user quét bằng banking app trên
    điện thoại để hoàn tất iDEAL. Optional trong wire (`""` khi thiếu)
    để tương thích với response cũ hoặc account_setup flow, nhưng
    payment flow thực tế LUÔN có field này (verify từ HAR
    `web_record_20260705-155711_manual` event 035).

    `payloadUri` là URL-encoded của `qrCodeUrl` (không có `sig` param) —
    dùng cho JS mobile bank picker deep-link. Tool này KHÔNG sử dụng
    trực tiếp, giữ trong model để đầy đủ theo wire (R14.5 — không im
    lặng drop field response).
    """

    view: str
    amount: str | int
    creditorName: str
    supportedIssuers: list[IssuerBank] = field(default_factory=list)
    qrCodeUrl: str = ""
    payloadUri: str = ""


def parse_ideal_transaction_initiate(
    payload: dict[str, Any],
) -> IdealTransactionInitiate:
    """Parse response `initiate` thành `IdealTransactionInitiate`
    (Requirement 5.4, 5.5, 5.6, 5.7).

    Fail_Fast_Policy:
        - Thiếu `view` (R5.5 sẽ validate giá trị) → `RequiredFieldMissingError`.
        - `supportedIssuers` thiếu hoặc là list rỗng (R5.6) →
          `RequiredFieldMissingError` với `field="supportedIssuers"`
          (chuỗi thống nhất — log realtime dùng field name để định danh).
        - Mỗi phần tử `supportedIssuers` propagate lỗi từ
          `parse_issuer_bank()` (R5.7).

    Ghi chú: hàm này KHÔNG validate `view == "INITIAL_VIEW"` (đó là R5.5, do
    `IdealFlowHandler`/`TransactionClient.initiate()` kiểm tra sau khi parse
    xong, để dùng exception riêng `InvalidTransactionViewError` mang giá trị
    `actual_view` — không gộp vào lỗi thiếu field).
    """
    if "view" not in payload or not payload.get("view"):
        raise RequiredFieldMissingError(
            field="view",
            model=_IDEAL_TRANSACTION_INITIATE_MODEL_NAME,
        )

    raw_issuers = payload.get("supportedIssuers")
    if not raw_issuers:
        # Bao trùm cả 2 trường hợp R5.6: thiếu field hoàn toàn HOẶC list rỗng.
        raise RequiredFieldMissingError(
            field="supportedIssuers",
            model=_IDEAL_TRANSACTION_INITIATE_MODEL_NAME,
        )
    if not isinstance(raw_issuers, list):
        raise RequiredFieldMissingError(
            field="supportedIssuers",
            model=_IDEAL_TRANSACTION_INITIATE_MODEL_NAME,
            message=(
                "IdealTransactionInitiate.supportedIssuers must be a list, "
                f"got: {type(raw_issuers).__name__}."
            ),
        )

    parsed_issuers: list[IssuerBank] = [
        parse_issuer_bank(item) for item in raw_issuers
    ]

    qr_code_url = payload.get("qrCodeUrl") or ""
    payload_uri = payload.get("payloadUri") or ""
    if qr_code_url and not isinstance(qr_code_url, str):
        raise RequiredFieldMissingError(
            field="qrCodeUrl",
            model=_IDEAL_TRANSACTION_INITIATE_MODEL_NAME,
            message=(
                "IdealTransactionInitiate.qrCodeUrl must be a str, "
                f"got: {type(qr_code_url).__name__}."
            ),
        )
    if payload_uri and not isinstance(payload_uri, str):
        raise RequiredFieldMissingError(
            field="payloadUri",
            model=_IDEAL_TRANSACTION_INITIATE_MODEL_NAME,
            message=(
                "IdealTransactionInitiate.payloadUri must be a str, "
                f"got: {type(payload_uri).__name__}."
            ),
        )

    return IdealTransactionInitiate(
        view=payload["view"],
        amount=payload.get("amount", ""),
        creditorName=payload.get("creditorName", ""),
        supportedIssuers=parsed_issuers,
        qrCodeUrl=qr_code_url,
        payloadUri=payload_uri,
    )


__all__ = [
    # Requirement 1 — Account.
    "IdealParsedAccount",
    "parse_account_line",
    # Requirement 1 — Session.
    "SessionBundle",
    # Requirement 1 — Checkout.
    "CheckoutSession",
    "parse_checkout_session",
    # Requirement 2 — Stripe init/elements.
    "StripePaymentPageInit",
    "parse_stripe_payment_page_init",
    "StripeElementsSession",
    "parse_stripe_elements_session",
    # Requirement 3 — Billing.
    "BillingAddress",
    # Requirement 5 — Device profile.
    "DeviceProfile",
    "validate_device_profile",
    # Requirement 5 — Issuer bank.
    "IssuerBank",
    "parse_issuer_bank",
    # Requirement 5 — Transaction initiate.
    "IdealTransactionInitiate",
    "parse_ideal_transaction_initiate",
]
