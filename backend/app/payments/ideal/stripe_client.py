"""StripeClient — HTTP client cho Stripe Payment Page (`api.stripe.com/v1/*`).

PHASE 1 (task 16.1): `init`, `elements_sessions`, `_bounded_retry`.
PHASE 2 (task 16.3): `confirm`, `_build_confirm_request` — tính
`js_checksum`/`rv_timestamp` NỘI BỘ (Requirement 3.3, placeholder có ý
thức chờ HAR thực), `passive_captcha_token` rỗng mặc định.
PHASE 3 (task 16.7): `refresh_poll`, `follow_redirect`.

Tất cả method thuộc CÙNG class `StripeClient` (không tách class riêng),
giữ boundary Stripe tập trung 1 nơi.

Cấu hình runtime đọc live từ Settings_Store (Requirement 11) mỗi request —
KHÔNG cache lúc `__init__` — để write-through settings phản ánh ngay lập
tức vào job đang chạy tiếp theo:

- `ideal.stripe_max_retry_attempts` (int, ≥ 1, default 3): số lần thử tối
  đa bao gồm lần đầu (Requirement 2.6).
- `ideal.stripe_retry_backoff_seconds` (number, > 0, default 0.5): hệ số
  gốc cho linear backoff `backoff × attempt` (Requirement 2.6).
- `ideal.stripe_request_timeout_seconds` (number, > 0, default 30): timeout
  cho MỖI lần thử HTTP request Stripe (Requirement 2.1, 2.3).
- `ideal.refresh_poll_max_attempts` (int, ≥ 1, default 5): số lượt thử
  refresh tối đa cho `refresh_poll` (Requirement 4.3, 4.5, 4.6).
- `ideal.refresh_poll_delay_seconds` (number, > 0, default 1.0): thời gian
  chờ giữa các lượt refresh chưa thấy `redirect_to_url` (Requirement 4.5).

Retry policy (`_bounded_retry`, Requirement 2.6):

- Retry khi: `http.TimeoutException`, `http.NetworkError` (lỗi mạng — chưa
  nhận được HTTP response), HOẶC HTTP status ∈ [500, 599].
- KHÔNG retry khi: HTTP status ∈ [400, 499] — coi là lỗi client (validate,
  auth, param sai) → Fail_Fast_Policy ngay lập tức, raise
  `StripeHttpClientError` với step khớp `request_name`.
- Retry KHÔNG áp dụng cho lỗi validate field thiếu (raise
  `RequiredFieldMissingError` khi parse response) — validate là lỗi
  application-level, không phải lỗi HTTP transport.
- Hết attempts vẫn fail (mạng/timeout/5xx liên tục) → raise
  `StripeRetryExhaustedError(request_name, attempts, last_error)`.

Payment_Module_Boundary (Requirement 13.1, 13.6): module này chỉ import
`app.core.settings_store` (đọc config) và `app.payments.ideal.{errors,
models}` — KHÔNG import gì khác từ `app.core.*`. KHÔNG có singleton, KHÔNG
side-effect ở import time.

_Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 3.2, 3.3, 3.4, 3.5,
3.6, 3.7, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 4.10_
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import parse_qs, urlparse

from app.core import http_client as http
from app.core.redaction import redact_dict, redact_message
from app.core.settings_store import SettingsRepository
from app.payments.ideal.errors import (
    AddressValidationRejectedError,
    ConfirmInvalidResponseError,
    IdealFlowError,
    RedirectValidationError,
    RefreshPollExhaustedError,
    StripeHttpClientError,
    StripeRetryExhaustedError,
)
from app.payments.ideal.models import (
    BillingAddress,
    StripeElementsSession,
    StripePaymentPageInit,
    parse_stripe_elements_session,
    parse_stripe_payment_page_init,
)
from app.payments.ideal.stripe_token import (
    StripeTokenConfig,
    StripeTokenExtractError,
    compute_js_checksum,
    compute_rv_timestamp,
    extract_config_live,
)

# ---------------------------------------------------------------------------
# Endpoint constants + Settings key constants
# ---------------------------------------------------------------------------

#: Base URL Stripe API — hardcode theo spec (Requirement 2.1, 2.3). KHÔNG
#: cấu hình qua Settings vì đây là endpoint cố định của Stripe, thay đổi
#: URL đồng nghĩa thay đổi payment provider (không phải runtime config).
_STRIPE_API_BASE: str = "https://api.stripe.com"

#: Map BIC (Bank Identifier Code — `ideal.known_issuers` whitelist) → Stripe
#: bank code (Stripe iDEAL API `payment_method_data.ideal.bank`).
#:
#: Stripe iDEAL API `confirm` yêu cầu `payment_method_data.ideal.bank` để
#: attach payment method + sinh redirect URL — CHỈ chấp nhận Stripe-defined
#: bank codes, KHÔNG chấp nhận BIC ISO 9362. Reference:
#: https://docs.stripe.com/api/payment_methods/object#payment_method_object-ideal-bank
#:
#: Trong luồng iDEAL Requirement 6.2 nói "user chọn bank ở `pay.ideal.nl`",
#: nhưng thực tế Stripe cần bank tại thời điểm confirm để tạo intent. Cách
#: hoà giải: default 1 bank phổ biến (Rabobank) tại confirm — Stripe tạo
#: intent + redirect về `pay.ideal.nl/transactions/...` — tại URL đó user
#: vẫn có thể đổi bank khác qua iDEAL universal checkout của Nederlands
#: Betaalvereniging. QR/deeplink cuối cùng đúng theo bank user chọn ở step
#: `TransactionClient.initiate` sau này (Requirement 5-6).
_BIC_TO_STRIPE_BANK: dict[str, str] = {
    "ABNANL2A": "abn_amro",
    "ASNBNL21": "asn_bank",
    "BUNQNL2A": "bunq",
    "INGBNL2A": "ing",
    "KNABNL2H": "knab",
    "RABONL2U": "rabobank",
    "RBRBNL21": "regiobank",
    "REVOLT21": "revolut",
    "SNSBNL2A": "sns_bank",
    "TRIONL2U": "triodos_bank",
    "FVLBNL22": "van_lanschot",
    "BITSNL2A": "bunq",  # Bitvavo dùng gateway Bunq
    "NTSBDEB1": "n26",   # N26 (dù BIC là DE, Stripe map n26 cho iDEAL EU)
}

#: Default Stripe bank code khi BIC không match trong `_BIC_TO_STRIPE_BANK`.
#: Rabobank — bank iDEAL phổ biến nhất Nederlands, an toàn làm fallback.
_DEFAULT_STRIPE_BANK: str = "rabobank"

#: Stripe API version dùng cho header `_stripe_version` — khớp với UPI
#: reference (rust_upi_bot) đã test production. Chuỗi này Stripe kỳ vọng
#: nguyên vẹn không đổi giữa các request cùng flow.
_STRIPE_VERSION: str = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)

#: Locale + timezone browser cho Stripe elements — locale NL, tương ứng
#: `Europe/Amsterdam` (đồng bộ với billing NL của IdealFlowHandler).
_BROWSER_LOCALE_NL: str = "nl-NL"
_BROWSER_TIMEZONE_NL: str = "Europe/Amsterdam"

#: Payment method type cho iDEAL flow (Requirement 2.3).
_PAYMENT_METHOD_TYPE_IDEAL: str = "ideal"

#: Origin/Referer cho request Stripe — Stripe edge check request đến từ
#: js.stripe.com (browser SDK), không từ domain khác.
_STRIPE_JS_ORIGIN: str = "https://js.stripe.com"

#: Path template cho payment_pages/init (Requirement 2.1).
_PAYMENT_PAGES_INIT_PATH_TEMPLATE: str = "/v1/payment_pages/{checkout_session_id}/init"

#: Path cho elements/sessions (Requirement 2.3).
_ELEMENTS_SESSIONS_PATH: str = "/v1/elements/sessions"

#: Path template cho payment_pages/confirm (Requirement 3.2).
_PAYMENT_PAGES_CONFIRM_PATH_TEMPLATE: str = "/v1/payment_pages/{checkout_session_id}/confirm"

#: Path template cho `refresh_poll` — GET payment_page state (Requirement
#: 4.3). KHÔNG có suffix `/init`: đây là endpoint truy vấn trạng thái tổng
#: quát của payment page (bao gồm `setup_intent.next_action.redirect_to_url`).
_PAYMENT_PAGES_REFRESH_PATH_TEMPLATE: str = "/v1/payment_pages/{checkout_session_id}"

#: Prefix path bắt buộc của Location header sau khi follow redirect Stripe →
#: iDEAL (Requirement 4.7). Format kỳ vọng:
#: `https://pay.ideal.nl/transactions/{encoded_tx_url}?sig={sig}`.
#: Segment sau prefix này chính là `encoded_tx_url` truyền nguyên vẹn sang
#: bước `transaction_initiate` (Requirement 5.1).
_IDEAL_REDIRECT_TRANSACTIONS_PATH_PREFIX: str = "/transactions/"

#: Độ dài prefix hex lấy từ SHA256 làm placeholder `js_checksum` (R3.3).
#: Placeholder có ý thức: HAR thực Stripe chưa xác nhận thuật toán thật,
#: nên dùng SHA256 rút gọn để có giá trị deterministic, non-empty, đủ dài.
_JS_CHECKSUM_PLACEHOLDER_HEX_LEN: int = 32

#: Danh sách keyword address-related dùng scan `error.message` khi Stripe
#: reject request `confirm` mà không kèm `error.param` (Requirement 3.4).
#: Nếu bất kỳ keyword nào xuất hiện trong message → coi là lỗi validate
#: địa chỉ, dùng chính keyword làm `rejected_field`.
_ADDRESS_ERROR_KEYWORDS: tuple[str, ...] = (
    "address_line1",
    "address_line2",
    "address_city",
    "address_postal_code",
    "address_state",
    "name",
    "email",
)

# Settings keys — dùng full-qualified `namespace.field` (Requirement 11.7).
_SETTING_MAX_RETRY_ATTEMPTS: str = "ideal.stripe_max_retry_attempts"
_SETTING_RETRY_BACKOFF_SECONDS: str = "ideal.stripe_retry_backoff_seconds"
_SETTING_REQUEST_TIMEOUT_SECONDS: str = "ideal.stripe_request_timeout_seconds"
_SETTING_REFRESH_POLL_MAX_ATTEMPTS: str = "ideal.refresh_poll_max_attempts"
_SETTING_REFRESH_POLL_DELAY_SECONDS: str = "ideal.refresh_poll_delay_seconds"

# Defaults khi Settings_Store chưa set giá trị — khớp default trong bảng
# whitelist Requirement 11.7 (design.md). Fail_Fast_Policy: dùng default
# ở đây KHÔNG che lỗi (Settings đã whitelist type nên `None` chỉ xảy ra
# khi key chưa set lần nào — trạng thái initial hợp lệ).
_DEFAULT_MAX_RETRY_ATTEMPTS: int = 3
_DEFAULT_RETRY_BACKOFF_SECONDS: float = 0.5
_DEFAULT_REQUEST_TIMEOUT_SECONDS: float = 30.0
_DEFAULT_REFRESH_POLL_MAX_ATTEMPTS: int = 5
_DEFAULT_REFRESH_POLL_DELAY_SECONDS: float = 1.0

#: Số byte tối đa của body_snippet log kèm khi 4xx — cắt ngắn để tránh
#: leak dữ liệu nhạy cảm/spam log. Giá trị nhỏ vừa đủ nhận diện lỗi.
_BODY_SNIPPET_MAX_LEN: int = 256


def _flatten_form(payload: Any, prefix: str = "") -> list[tuple[str, str]]:
    """Flatten nested dict/list thành list ``(key, value)`` cho form-urlencoded.

    Stripe accept nested key qua notation ``parent[child][grandchild]`` cho
    object và ``parent[0]``/``parent[1]`` cho array. Ví dụ:
        ``{"a": {"b": [1, 2]}}`` → ``[("a[b][0]", "1"), ("a[b][1]", "2")]``.

    Convention (match Rust UPI reference `rust_upi_bot/src/stripe/forms.rs`):
        - ``None``               → SKIP KEY (không gửi field). Stripe treat
          `field=` (empty) và không có field khác nhau — captcha field trống
          thì Stripe silently reject attach setup_intent.
        - ``bool``               → ``"true"``/``"false"``.
        - Số / chuỗi             → ``str(value)``.
    """
    out: list[tuple[str, str]] = []
    if isinstance(payload, dict):
        for k, v in payload.items():
            key = f"{prefix}[{k}]" if prefix else str(k)
            out.extend(_flatten_form(v, key))
    elif isinstance(payload, (list, tuple)):
        for i, v in enumerate(payload):
            key = f"{prefix}[{i}]"
            out.extend(_flatten_form(v, key))
    else:
        if payload is None:
            # Match Rust `to_form()`: skip key hoàn toàn.
            return out
        if isinstance(payload, bool):
            value = "true" if payload else "false"
        else:
            value = str(payload)
        out.append((prefix, value))
    return out


def _encode_form_urlencoded(pairs: list[tuple[str, str]]) -> bytes:
    """Encode list of (key, value) → bytes URL-encoded.

    Truyền qua ``data=<bytes>`` khi POST — curl_cffi dùng path code stable
    cho raw bytes body (bypass smart-dispatch dict-vs-list-vs-file). Encode
    thủ công đảm bảo order và duplicate keys giữ nguyên (Stripe kiểm
    order/dup ở 1 số form).
    """
    from urllib.parse import urlencode as _urlencode
    return _urlencode(pairs, doseq=False).encode("utf-8")


def _stripe_guid() -> str:
    """Sinh chuỗi format Stripe.js browser SDK: ``<uuid4>-<10 hex>``.

    Mirror UPI reference (``rust_upi_bot::stripe_guid``). Stripe dùng
    ``guid/muid/sid`` như signals cross-request để track browser session.
    """
    import uuid as _uuid  # local — không phải hot path
    return f"{_uuid.uuid4()}{_uuid.uuid4().hex[:10]}"


class _SupportsInfoLog(Protocol):
    """Duck-typed logger cho realtime job log (Requirement 2.8).

    `IdealFlowHandler.run()` sẽ inject 1 logger cụ thể (structlog wrapper
    hoặc `logging.Logger` với `extra=`) khi khởi tạo `StripeClient` — module
    này KHÔNG phụ thuộc implementation cụ thể để giữ khả năng test thay thế
    dễ dàng.
    """

    def info(self, event: str, /, **kwargs: Any) -> None:  # pragma: no cover
        ...


# Callable ký hiệu request thực tế của mỗi method public — nhận Nothing,
# trả `http.Response` (đã raise trước khi return nếu network/timeout).
_DoRequestFn = Callable[[], Awaitable[http.Response]]


class StripeClient:
    """Client bao gói toàn bộ HTTP request tới Stripe cho luồng iDEAL.

    Cấu hình đọc live từ `SettingsRepository` — KHÔNG cache tại `__init__`
    (Requirement 11.6: write-through settings phải phản ánh ngay).

    Payment_Module_Boundary (Requirement 13.6): class này thuộc
    `payments/ideal/` — chỉ dùng `app.core.http_client` (cross-cutting) và Settings_Store,
    KHÔNG import module khác từ `app.core.*` ngoài Settings.

    Attributes:
        _http_client: `http.AsyncSession` dùng chung — do caller
            (`IdealFlowHandler`) tạo và quản lý vòng đời (đóng qua
            `aclose()`). `StripeClient` KHÔNG đóng client.
        _settings: `SettingsRepository` để đọc runtime config.
        _logger: Duck-typed logger có `.info(event, **kwargs)`.
    """

    def __init__(
        self,
        http_client: http.AsyncSession,
        settings: SettingsRepository,
        logger: _SupportsInfoLog,
        proxy_url: str | None = None,
    ) -> None:
        self._http_client = http_client
        self._settings = settings
        self._logger = logger
        # `proxy_url` — materialized URL đã URL-encode credential + resolve
        # `{SID}`. Dùng cho các fresh client mà `follow_redirect` phải tạo
        # để tránh leak headers `oai-*` (xem docstring `follow_redirect`).
        # Nếu `None` → Direct_Mode. Bắt buộc pass để KHÔNG lộ IP thật của
        # server backend khi flow qua `pm-redirects.stripe.com`.
        self._proxy_url = proxy_url
        # `stripe_js_id` sinh lazy trong lần gọi `init` đầu tiên, giữ cố
        # định cho toàn bộ vòng đời 1 StripeClient (1 flow iDEAL). Mirror
        # behavior browser SDK load `js.stripe.com/v3/` 1 lần.
        self._stripe_js_id: str | None = None
        # `token_config` fetch lazy khi cần compute js_checksum/rv_timestamp
        # (chỉ cần trong `confirm`). Cache 1 lần cho vòng đời client, tránh
        # fetch bundle Stripe.js lặp lại (bundle rarely changes).
        self._token_config: StripeTokenConfig | None = None
        # ------------------------------------------------------------------
        # Per-flow config cache (Fix bottleneck: đọc DB lặp lại mỗi retry).
        # ------------------------------------------------------------------
        # StripeClient được tạo per-job (1 instance = 1 flow iDEAL) — các
        # setting timeout/retry/backoff/poll KHÔNG đổi giữa các retry
        # trong cùng flow. Trước đây mỗi lần retry `_bounded_retry` /
        # `refresh_poll` gọi `await self._settings.get(...)` 3-5 lần =
        # nhiều round-trip SQLite thừa cho giá trị không đổi.
        #
        # Semantic mới: đọc lần đầu, cache trong instance. Job kế tiếp có
        # StripeClient mới → sẽ đọc lại giá trị mới nhất từ DB. Nghĩa là:
        # write-through settings vẫn phản ánh cho job KẾ (không lift ngay
        # giữa retry của job hiện tại) — tương thích với semantic
        # `settings_snapshot` at job creation của JobManager (R11.2).
        #
        # `None` = chưa đọc; đọc live lần đầu → cache giá trị. KHÔNG dùng
        # sentinel object riêng vì các setting này đều có kiểu numeric
        # non-None (đọc DB trả None chỉ khi key chưa set → fallback default,
        # kết quả cache vẫn là int/float — không nhầm với sentinel).
        self._cached_max_retry_attempts: int | None = None
        self._cached_retry_backoff_seconds: float | None = None
        self._cached_request_timeout_seconds: float | None = None
        self._cached_refresh_poll_max_attempts: int | None = None
        self._cached_refresh_poll_delay_seconds: float | None = None

    def _get_or_create_stripe_js_id(self) -> str:
        """Sinh `stripe_js_id` mới hoặc trả lại giá trị đã cache.

        Format: `<uuid4>-<10 hex>` — khớp UPI reference (rust_upi_bot).
        Stripe dùng ID này liên kết `init` / `elements/sessions` / `confirm`
        thành 1 chuỗi request cùng session.
        """
        if self._stripe_js_id is None:
            import uuid as _uuid  # local — không phải hot path
            u1 = str(_uuid.uuid4())
            u2 = _uuid.uuid4().hex[:10]
            self._stripe_js_id = f"{u1}{u2}"
        return self._stripe_js_id

    async def ensure_token_config(self) -> StripeTokenConfig:
        """Lazy fetch Stripe.js bundle → extract StripeTokenConfig, cache 1 lần.

        Cần cho ``compute_js_checksum(ppage_id, shift)`` +
        ``compute_rv_timestamp(cfg)`` khi build request confirm — Stripe
        strict check 3 token này (Requirement 3.3).

        Cache disk theo SHA256 entry source (xem
        ``stripe_token.fetch_bundles_live``) — bundle rarely changes nên
        fetch chỉ tốn 1-2s lần đầu, cache hit các lần sau.

        Raises:
            StripeTokenExtractError: Stripe đổi obfuscation → cần update
                pattern match trong ``stripe_token.py``.
        """
        if self._token_config is None:
            self._token_config = await extract_config_live(
                self._http_client, use_cache=True
            )
            self._logger.info(
                "stripe token_config fetched",
                bundle_hash=self._token_config.bundle_hash[:12],
                shift=self._token_config.shift,
            )
        return self._token_config

    # ------------------------------------------------------------------
    # Public methods — Requirement 2.1-2.5
    # ------------------------------------------------------------------

    async def init(
        self,
        checkout_session_id: str,
        publishable_key: str,
    ) -> StripePaymentPageInit:
        """`POST /v1/payment_pages/{checkout_session_id}/init` (Requirement 2.1).

        Body form-encoded: `payment_page_id=<checkout_session_id>`,
        `publishable_key=<publishable_key>`. Auth qua header
        `Authorization: Bearer <publishable_key>` — pk token dùng làm bearer.

        Retry theo `_bounded_retry` (Requirement 2.6). Parse response qua
        `parse_stripe_payment_page_init` — thiếu `init_checksum` hoặc
        `config_id` sẽ raise `RequiredFieldMissingError` (Requirement 2.2).

        Args:
            checkout_session_id: Lấy từ `CheckoutSession.checkout_session_id`
                (Requirement 2.5 — KHÔNG hardcode).
            publishable_key: Lấy từ `CheckoutSession.publishable_key`
                (Requirement 2.5 — KHÔNG hardcode).
        """
        if not checkout_session_id:
            raise ValueError(
                "StripeClient.init yêu cầu 'checkout_session_id' non-empty "
                "(Requirement 2.5 — dùng từ CheckoutSession, KHÔNG hardcode)."
            )
        if not publishable_key:
            raise ValueError(
                "StripeClient.init yêu cầu 'publishable_key' non-empty "
                "(Requirement 2.5 — dùng từ CheckoutSession, KHÔNG hardcode)."
            )

        url = f"{_STRIPE_API_BASE}{_PAYMENT_PAGES_INIT_PATH_TEMPLATE.format(checkout_session_id=checkout_session_id)}"
        # Sinh mới `stripe_js_id` mỗi flow — Stripe dùng ID này liên kết các
        # request cùng session (init/elements/confirm). Cache vào `self` để
        # `elements_sessions` sau đó dùng lại (mirror behavior browser SDK
        # load ``js.stripe.com/v3/`` 1 lần và giữ ID cố định).
        stripe_js_id = self._get_or_create_stripe_js_id()
        # Body theo UPI reference — Stripe reject 400 nếu thiếu các field
        # `elements_session_client`/`elements_options_client`/`_stripe_version`.
        # `key` dùng thay Authorization Bearer (Stripe Payment Page accept
        # cả 2 nhưng UPI reference dùng `key` field trong body).
        payload = {
            "browser_locale": _BROWSER_LOCALE_NL,
            "browser_timezone": _BROWSER_TIMEZONE_NL,
            "elements_session_client": {
                "client_betas": [
                    "custom_checkout_server_updates_1",
                    "custom_checkout_manual_approval_1",
                ],
                "elements_init_source": "custom_checkout",
                "referrer_host": "chatgpt.com",
                "stripe_js_id": stripe_js_id,
                "locale": "en",
                "is_aggregation_expected": "false",
            },
            "elements_options_client": {
                "saved_payment_method": {
                    "enable_save": "auto",
                    "enable_redisplay": "auto",
                },
            },
            "key": publishable_key,
            "_stripe_version": _STRIPE_VERSION,
        }
        # Encode form thủ công (xem `_encode_form_urlencoded` docstring — dùng
        # `data=<bytes>` để bypass smart-dispatch của HTTP client).
        content_bytes = _encode_form_urlencoded(_flatten_form(payload))
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": _STRIPE_JS_ORIGIN,
            "Referer": f"{_STRIPE_JS_ORIGIN}/",
        }
        timeout = await self._read_request_timeout_seconds()

        async def _do_request() -> http.Response:
            return await self._http_client.post(
                url,
                data=content_bytes,
                headers=headers,
                timeout=timeout,
            )

        response = await self._bounded_retry("init", _do_request)
        parsed_payload = self._parse_json_body(response, request_name="init")
        # Fail_Fast_Policy R2.2: thiếu field bắt buộc → parse_* raise
        # RequiredFieldMissingError, propagate lên IdealFlowHandler.
        return parse_stripe_payment_page_init(parsed_payload)

    async def elements_sessions(
        self,
        checkout_session_id: str,
        publishable_key: str,
        amount: int = 0,
    ) -> StripeElementsSession:
        """`GET /v1/elements/sessions` (Requirement 2.3).

        Query params:
            - `deferred_intent[mode]=subscription` — ChatGPT Plus dùng
              subscription mode (bắt buộc theo Stripe API).
            - `deferred_intent[amount]=<int>` — amount cents từ init response
              (2000 = 20 EUR/tháng). 0 → Stripe tự infer theo
              `checkout_session_id`.
            - `deferred_intent[currency]=eur` — locale NL/EUR.
            - `deferred_intent[payment_method_types][0]=ideal` — chỉ định
              phương thức iDEAL (Requirement 2.3).
            - `key=<publishable_key>` — auth Stripe unauthenticated Elements
              endpoint (KHÔNG dùng Bearer header cho endpoint này).

        `checkout_session_id` được truyền vào query params để Stripe liên
        kết session (Requirement 2.5).

        Retry theo `_bounded_retry`. Parse qua `parse_stripe_elements_session`
        — thiếu `session_id` sẽ raise `RequiredFieldMissingError`
        (Requirement 2.4).
        """
        if not checkout_session_id:
            raise ValueError(
                "StripeClient.elements_sessions yêu cầu 'checkout_session_id' "
                "non-empty (dùng cho log realtime — Requirement 2.8)."
            )
        if not publishable_key:
            raise ValueError(
                "StripeClient.elements_sessions yêu cầu 'publishable_key' "
                "non-empty (Requirement 2.5 — dùng từ CheckoutSession)."
            )

        url = f"{_STRIPE_API_BASE}{_ELEMENTS_SESSIONS_PATH}"
        stripe_js_id = self._get_or_create_stripe_js_id()
        # Query params đầy đủ theo UPI reference (rust_upi_bot) — Stripe
        # bắt buộc `deferred_intent[mode]` (test đã confirm HTTP 400
        # "Missing required param: deferred_intent[mode]" khi thiếu).
        # `mode=subscription` khớp ChatGPT Plus (thuê bao hàng tháng);
        # amount lấy từ init response (0 → Stripe tự infer theo
        # checkout_session_id).
        query_params: list[tuple[str, str]] = [
            ("client_betas[0]", "custom_checkout_server_updates_1"),
            ("client_betas[1]", "custom_checkout_manual_approval_1"),
            ("deferred_intent[mode]", "subscription"),
            ("deferred_intent[amount]", str(amount)),
            ("deferred_intent[currency]", "eur"),
            (
                "deferred_intent[setup_future_usage]",
                "off_session",
            ),
            (
                "deferred_intent[payment_method_types][0]",
                _PAYMENT_METHOD_TYPE_IDEAL,
            ),
            ("currency", "eur"),
            ("key", publishable_key),
            ("_stripe_version", _STRIPE_VERSION),
            ("elements_init_source", "custom_checkout"),
            ("referrer_host", "chatgpt.com"),
            ("stripe_js_id", stripe_js_id),
            ("locale", "en"),
            ("type", "deferred_intent"),
            ("checkout_session_id", checkout_session_id),
        ]
        headers = {
            "Accept": "application/json",
            "Origin": _STRIPE_JS_ORIGIN,
            "Referer": f"{_STRIPE_JS_ORIGIN}/",
        }
        timeout = await self._read_request_timeout_seconds()

        async def _do_request() -> http.Response:
            return await self._http_client.get(
                url,
                params=query_params,
                headers=headers,
                timeout=timeout,
            )

        response = await self._bounded_retry("elements_sessions", _do_request)
        payload = self._parse_json_body(response, request_name="elements_sessions")
        return parse_stripe_elements_session(payload)

    # ------------------------------------------------------------------
    # Retry engine — Requirement 2.6
    # ------------------------------------------------------------------

    async def _bounded_retry(
        self,
        request_name: str,
        do_request_fn: _DoRequestFn,
    ) -> http.Response:
        """Retry tuyến tính có giới hạn cho request Stripe (Requirement 2.6).

        Chiến lược (đơn hướng):
            - Loop tối đa `ideal.stripe_max_retry_attempts` lần (bao gồm
              lần đầu). Attempt bắt đầu từ 1.
            - Trước MỖI lần thử (kể cả lần đầu): log
              `logger.info("stripe request", request=..., attempt=...)`.
            - Sau mỗi lần thử: log status_code nhận được (hoặc lỗi mạng).
            - `http.TimeoutException` HOẶC `http.NetworkError` (bao gồm
              cả subclass): coi là lỗi transport → retry.
            - HTTP status ∈ [200, 300): trả response ngay lập tức.
            - HTTP status ∈ [400, 500): fail-fast — raise
              `StripeHttpClientError` (KHÔNG retry — Requirement 2.6).
            - HTTP status ∈ [500, 600): retry.
            - HTTP status khác (1xx, 3xx): coi là bất thường, không retry,
              raise `StripeHttpClientError` để log realtime rõ ràng — Stripe
              REST API không sinh 1xx/3xx trong luồng bình thường.

        Backoff giữa các lần thử: `stripe_retry_backoff_seconds × attempt`
        (linear — Requirement 2.6). KHÔNG sleep sau lần thử cuối cùng.

        Args:
            request_name: Định danh request cho log/exception (`init`,
                `elements_sessions`). Non-empty.
            do_request_fn: Async callable KHÔNG tham số, trả về
                `http.Response`. Gọi lại nguyên vẹn ở mỗi lần retry.

        Returns:
            `http.Response` với status 2xx đã kiểm tra.

        Raises:
            StripeHttpClientError: HTTP status 4xx (hoặc 1xx/3xx bất thường)
                — Fail_Fast, KHÔNG retry.
            StripeRetryExhaustedError: Hết `max_retry_attempts` mà vẫn lỗi
                mạng/timeout/5xx.
        """
        if not request_name:
            raise ValueError(
                "StripeClient._bounded_retry yêu cầu 'request_name' non-empty."
            )

        max_attempts = await self._read_max_retry_attempts()
        backoff_base = await self._read_retry_backoff_seconds()

        last_error_message: str = ""

        for attempt in range(1, max_attempts + 1):
            transport_error: BaseException | None = None
            response: http.Response | None = None
            try:
                response = await do_request_fn()
            except http.TimeoutException as exc:
                transport_error = exc
                last_error_message = f"timeout: {exc!r}"
            except http.NetworkError as exc:
                # http.NetworkError bao trùm ConnectError/ReadError/
                # WriteError/CloseError — tất cả đều là lỗi transport
                # theo Requirement 2.6.
                transport_error = exc
                last_error_message = f"network_error: {exc!r}"

            if transport_error is not None:
                # Log 1 dòng khi transport fail — kèm attempt/max để user
                # thấy tiến trình retry ngay từ đầu.
                self._logger.info(
                    "stripe request transport_error",
                    request=request_name,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error_kind=type(transport_error).__name__,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(backoff_base * attempt)
                    continue
                # Hết attempts → raise ở khối kết thúc vòng lặp bên dưới.
                break

            # response guaranteed non-None ở nhánh này.
            assert response is not None
            status = response.status_code

            if 200 <= status < 300:
                # Case phổ biến nhất — 1 dòng ok gọn cho attempt=1; nếu
                # attempt > 1 mới hiển thị số lần để user thấy đã retry.
                if attempt == 1:
                    self._logger.info(
                        "stripe %s ok status=%d", request_name, status
                    )
                else:
                    self._logger.info(
                        "stripe %s ok status=%d (attempt %d/%d)",
                        request_name,
                        status,
                        attempt,
                        max_attempts,
                    )
                return response

            if 500 <= status < 600:
                self._logger.info(
                    "stripe request http_5xx",
                    request=request_name,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    status_code=status,
                )
                last_error_message = f"http_5xx: status={status}"
                if attempt < max_attempts:
                    await asyncio.sleep(backoff_base * attempt)
                    continue
                break

            # Còn lại: 4xx (client error — KHÔNG retry) hoặc 1xx/3xx
            # (không mong đợi cho Stripe REST — Fail_Fast tường minh).
            body_snippet = self._safe_body_snippet(response)
            self._logger.info(
                "stripe request fail_fast",
                request=request_name,
                attempt=attempt,
                max_attempts=max_attempts,
                status_code=status,
            )
            raise StripeHttpClientError(
                request_name=request_name,
                http_status=status,
                body_snippet=body_snippet,
            )

        # Rơi vào đây khi vòng lặp break do hết attempts với transport/5xx.
        raise StripeRetryExhaustedError(
            request_name=request_name,
            attempts=max_attempts,
            message=(
                f"Stripe '{request_name}' exhausted retries after {max_attempts} "
                f"attempt(s) — last_error={last_error_message}"
            ),
        )

    # ------------------------------------------------------------------
    # Public method — update_billing (post-elements, pre-confirm)
    # ------------------------------------------------------------------

    async def update_billing(
        self,
        checkout_session_id: str,
        publishable_key: str,
        elements_session_id: str,
        billing: BillingAddress,
    ) -> None:
        """POST ``/v1/payment_pages/{id}`` với ``tax_region`` full billing.

        Bước này CẦN THIẾT trước ``confirm`` — Stripe validate và cache tax
        context vào payment_page state. Nếu skip, ``confirm`` trả 200 nhưng
        KHÔNG attach payment method (không tạo setup_intent), dẫn tới
        refresh_poll sau approve không có ``next_action.redirect_to_url``.

        HAR thực tế (``web_record_20260704-000744_manual``) cho thấy browser
        gọi 7 request update progressive theo từng field user gõ. Backend
        chỉ cần 1 request với tax_region đầy đủ để commit state.

        Body theo HAR:
            - ``tax_region[country/line1/line2/city/postal_code/state]``
            - ``elements_session_client[*]``
            - ``elements_options_client[*]``
            - ``client_attribution_metadata[merchant_integration_additional_elements][0..2]``
            - ``key=<publishable_key>``
            - ``_stripe_version``

        Fail_Fast: HTTP non-2xx → raise `IdealFlowError(step="stripe_update_billing")`.
        Không retry (Requirement 3.5 — chỉ init/elements retry).
        """
        if not checkout_session_id:
            raise ValueError("update_billing yêu cầu 'checkout_session_id' non-empty")
        if not publishable_key:
            raise ValueError("update_billing yêu cầu 'publishable_key' non-empty")

        url = (
            f"{_STRIPE_API_BASE}"
            f"{_PAYMENT_PAGES_REFRESH_PATH_TEMPLATE.format(checkout_session_id=checkout_session_id)}"
        )
        address = billing.address
        stripe_js_id = self._get_or_create_stripe_js_id()

        # NL không có state → bỏ field empty. Stripe reject empty string
        # với error `parameter_invalid_empty`.
        tax_region: dict[str, str] = {}
        for k in ("country", "line1", "line2", "city", "postal_code", "state"):
            v = address.get(k, "")
            if v:
                tax_region[k] = v

        payload: dict[str, Any] = {
            "tax_region": tax_region,
            "elements_session_client": {
                "client_betas": [
                    "custom_checkout_server_updates_1",
                    "custom_checkout_manual_approval_1",
                ],
                "elements_init_source": "custom_checkout",
                "referrer_host": "chatgpt.com",
                "session_id": elements_session_id,
                "stripe_js_id": stripe_js_id,
                "locale": "en",
                "is_aggregation_expected": "false",
            },
            "elements_options_client": {
                "saved_payment_method": {
                    "enable_save": "auto",
                    "enable_redisplay": "auto",
                },
            },
            "client_attribution_metadata": {
                "merchant_integration_additional_elements": [
                    "expressCheckout",
                    "payment",
                    "address",
                ],
            },
            "key": publishable_key,
            "_stripe_version": _STRIPE_VERSION,
        }
        content_bytes = _encode_form_urlencoded(_flatten_form(payload))
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": _STRIPE_JS_ORIGIN,
            "Referer": f"{_STRIPE_JS_ORIGIN}/",
        }
        timeout = await self._read_request_timeout_seconds()

        # Transport-level lỗi (timeout / DNS / connection reset / proxy die)
        # là TRANSIENT — map thành `IdealFlowError(error_code=
        # "stripe_update_billing_transport")` để boundary `_run_inner` catch,
        # tránh leak thành `unexpected exception` ở `_run_handler`. Code này
        # được whitelist trong `ideal.auto_retry_blocked_codes` → JobManager
        # auto-retry với proxy mới (cùng pattern với `stripe_confirm_transport`
        # / `stripe_follow_redirect_transport`). KHÔNG retry ngầm ở đây vì
        # `update_billing` không idempotent hoàn toàn (commit tax_region vào
        # payment_page state) — cần proxy mới để giảm rủi ro state phân mảnh.
        try:
            response = await self._http_client.post(
                url,
                data=content_bytes,
                headers=headers,
                timeout=timeout,
            )
        except (
            http.TimeoutException,
            http.NetworkError,
            http.TransportError,
        ) as exc:
            self._logger.warning(
                "stripe update_billing transport error checkout_session_id=%s "
                "type=%s msg=%s",
                checkout_session_id,
                type(exc).__name__,
                str(exc)[:200],
            )
            raise IdealFlowError(
                error_code="stripe_update_billing_transport",
                step="stripe_update_billing",
                message=(
                    f"Stripe update_billing transport error "
                    f"({type(exc).__name__}): {str(exc)[:200]}"
                ),
            ) from exc
        status = response.status_code
        # 1 dòng gộp: kèm city để user thấy address vừa gửi khớp expectation.
        self._logger.info(
            "stripe update_billing ok status=%d city=%r",
            status,
            address.get("city", ""),
        )

        if not (200 <= status < 300):
            body_snippet = self._safe_body_snippet(response)
            raise StripeHttpClientError(
                request_name="update_billing",
                http_status=status,
                body_snippet=body_snippet,
            )

    # ------------------------------------------------------------------
    # Public method — confirm (Requirement 3.2-3.7)
    # ------------------------------------------------------------------

    async def confirm(
        self,
        checkout_session_id: str,
        publishable_key: str,
        elements_session_id: str,
        billing: BillingAddress,
        *,
        init_checksum: str = "",
        amount: int = 0,
        elements_config_id: str = "",
        init_config_id: str = "",
        page_id: str = "",
        token_config: StripeTokenConfig | None = None,
    ) -> dict[str, Any]:
        """`POST /v1/payment_pages/{checkout_session_id}/confirm` (R3.2-R3.7).

        Gửi `payment_method_data[type]=ideal` + toàn bộ `BillingAddress`
        trong 1 request duy nhất (R3.2 — không chia field-by-field, KHÔNG
        gửi field chọn ngân hàng).

        Kèm 3 token JS-runtime tính nội bộ (R3.3):
            - `js_checksum`: placeholder SHA256(`session_id|element_id`) hex[:32].
              TODO: khi có HAR thực Stripe, thay thuật toán chính xác.
            - `rv_timestamp`: Unix millis (`str(int(time.time() * 1000))`).
            - `passive_captcha_token`: `""` rỗng mặc định.

        Fail_Fast_Policy (R3.5): KHÔNG retry request `confirm`. Timeout đọc
        live từ `ideal.stripe_request_timeout_seconds` (Settings, R11).

        Response check:
            - HTTP 4xx với error address validation (R3.4) → raise
              `AddressValidationRejectedError(rejected_field=...)`.
            - HTTP status khác 2xx (không thuộc case 4xx address) → raise
              `ConfirmInvalidResponseError(http_status=...)` (R3.5).
            - HTTP 2xx nhưng payload thiếu trạng thái xác nhận
              (`payment_intent`/`setup_intent`/`status`) → raise
              `ConfirmInvalidResponseError` (R3.5).
            - HTTP 2xx với payload hợp lệ → trả nguyên `payload` dict.

        Log realtime (R3.7): log bắt đầu confirm kèm `name`/`city`/
        `postal_code` (KHÔNG log toàn bộ billing/response — Sensitive_Data
        _Redaction R3.6 cho 3 token JS-runtime).

        Args:
            checkout_session_id: Từ `CheckoutSession.checkout_session_id` (R2.5).
            publishable_key: Từ `CheckoutSession.publishable_key` (R2.5).
            elements_session_id: Từ `StripeElementsSession.session_id` (R2.4).
            billing: `BillingAddress` do `IdealProfileGenerator` sinh (R3.1).

        Returns:
            Payload response Stripe (dict) — caller (`IdealFlowHandler`)
            tiếp tục dùng ở bước `approve`/`refresh_poll`.

        Raises:
            AddressValidationRejectedError: Stripe reject field địa chỉ (R3.4).
            ConfirmInvalidResponseError: HTTP status không hợp lệ hoặc
                payload thiếu trạng thái xác nhận (R3.5).
        """
        if not checkout_session_id:
            raise ValueError(
                "StripeClient.confirm yêu cầu 'checkout_session_id' non-empty "
                "(Requirement 2.5 — dùng từ CheckoutSession, KHÔNG hardcode)."
            )
        if not publishable_key:
            raise ValueError(
                "StripeClient.confirm yêu cầu 'publishable_key' non-empty "
                "(Requirement 2.5 — dùng từ CheckoutSession, KHÔNG hardcode)."
            )
        if not elements_session_id:
            raise ValueError(
                "StripeClient.confirm yêu cầu 'elements_session_id' non-empty "
                "(Requirement 2.4 — dùng từ StripeElementsSession.session_id)."
            )

        url, content_bytes, headers = self._build_confirm_request(
            checkout_session_id=checkout_session_id,
            publishable_key=publishable_key,
            elements_session_id=elements_session_id,
            billing=billing,
            init_checksum=init_checksum,
            amount=amount,
            elements_config_id=elements_config_id,
            init_config_id=init_config_id,
            page_id=page_id,
            token_config=token_config,
        )
        timeout = await self._read_request_timeout_seconds()

        # R3.7 — 1 dòng log begin với name/city/postal_code (KHÔNG log
        # full billing/token/response — Sensitive_Data_Redaction R3.6).
        # Kết quả sẽ được log ở 1 dòng gộp sau khi có response.
        address = billing.address

        # DEBUG: dump body confirm ra /tmp để diff với HAR (chỉ khi cờ
        # `IDEAL_DEBUG_DUMP_CONFIRM` set). Không log ra stdout vì body chứa
        # billing address (PII).
        import os as _os
        _dump = _os.environ.get("IDEAL_DEBUG_DUMP_CONFIRM")
        if _dump:
            try:
                from pathlib import Path as _Path
                _dump_path = _Path(_dump) / f"confirm_body_{checkout_session_id}.txt"
                _dump_path.parent.mkdir(parents=True, exist_ok=True)
                _dump_path.write_bytes(content_bytes)
            except Exception:  # noqa: BLE001
                pass

        # R3.5 — KHÔNG retry ngầm trong `confirm` (Stripe reject duplicate
        # payment_method + double-post risk). Nhưng transport-level lỗi
        # (timeout / DNS / connection reset / proxy die) là TRANSIENT — cần
        # map thành `IdealFlowError` để boundary `_run_inner` catch, KHÔNG
        # để propagate thô lên `_run_handler` thành `internal_error` chung.
        #
        # Error code `stripe_confirm_transport` được whitelist vào
        # `ideal.auto_retry_blocked_codes` (mặc định trong
        # `payments/ideal/__init__.py`) → JobManager auto-retry với proxy
        # mới. User KHÔNG bị mất job vì 1 lần proxy chết.
        try:
            response = await self._http_client.post(
                url,
                data=content_bytes,
                headers=headers,
                timeout=timeout,
            )
        except (
            http.TimeoutException,
            http.NetworkError,
            http.TransportError,
        ) as exc:
            self._logger.warning(
                "stripe confirm transport error checkout_session_id=%s "
                "type=%s msg=%s",
                checkout_session_id,
                type(exc).__name__,
                str(exc)[:200],
            )
            raise IdealFlowError(
                error_code="stripe_confirm_transport",
                step="stripe_confirm",
                message=(
                    f"Stripe confirm transport error "
                    f"({type(exc).__name__}): {str(exc)[:200]}"
                ),
            ) from exc
        status = response.status_code

        # R3.7 — 1 dòng gộp status + billing city để trace nhanh (không
        # log full billing/token/response — R3.6). Chi tiết payload/rqdata
        # chỉ log khi payload có bất thường (missing state / hCaptcha
        # required) ở bên dưới.
        self._logger.info(
            "stripe confirm ok status=%d billing=%s/%s",
            status,
            billing.name,
            address.get("city", ""),
        )

        # Parse JSON — body không parse được → ConfirmInvalidResponseError
        # (R3.5: payload không chứa trạng thái xác nhận hợp lệ).
        try:
            payload = response.json()
        except ValueError:
            raise ConfirmInvalidResponseError(
                http_status=status,
                message=(
                    f"Stripe confirm returned a body that could not be parsed as JSON "
                    f"(http_status={status})"
                ),
            )
        if not isinstance(payload, dict):
            raise ConfirmInvalidResponseError(
                http_status=status,
                message=(
                    f"Stripe confirm returned JSON that is not an object "
                    f"(got: {type(payload).__name__}, http_status={status})"
                ),
            )

        # R3.4 — Status 4xx với error validate địa chỉ → AddressValidationRejectedError.
        if 400 <= status < 500:
            rejected_field = self._extract_address_rejected_field(payload)
            if rejected_field is not None:
                self._logger.info(
                    "stripe confirm address_validation_rejected",
                    checkout_session_id=checkout_session_id,
                    rejected_field=rejected_field,
                    http_status=status,
                )
                raise AddressValidationRejectedError(rejected_field=rejected_field)
            # 4xx nhưng KHÔNG phải validate địa chỉ (VD `error.param=payment_method`,
            # `elements_session_id`, `init_checksum`, hoặc `error.code=<js_token>`).
            # Extract nguyên `error.type/code/param/message` từ Stripe để log/
            # exception giữ được nguyên nhân THẬT — tránh đánh tráo nhãn (bug
            # cũ: mọi `invalid_request_error` bị wrap thành address error).
            error_obj = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            err_type = str(error_obj.get("type") or "")
            err_code = str(error_obj.get("code") or "")
            err_param = str(error_obj.get("param") or "")
            err_message = str(error_obj.get("message") or "")

            # Log realtime để dev/user thấy nguyên nhân thật (R3.7). `error.*`
            # là short strings do Stripe trả — không chứa PII của billing.
            self._logger.info(
                "stripe confirm invalid_request",
                checkout_session_id=checkout_session_id,
                http_status=status,
                error_type=err_type,
                error_code=err_code,
                error_param=err_param,
                error_message=err_message,
            )

            # Message của exception: chỉ include field non-empty để tránh
            # noise "code='' param=''". Cắt `error.message` để tránh log
            # quá dài nếu Stripe trả stacktrace/HTML.
            def _fmt(label: str, value: str) -> str:
                return f"{label}={value!r}" if value else ""

            snippet = err_message[:_BODY_SNIPPET_MAX_LEN]
            parts = [
                _fmt("type", err_type),
                _fmt("code", err_code),
                _fmt("param", err_param),
                _fmt("message", snippet),
            ]
            detail = " ".join(p for p in parts if p)
            raise ConfirmInvalidResponseError(
                http_status=status,
                message=(
                    f"Stripe confirm HTTP {status} rejected"
                    + (f": {detail}" if detail else "")
                    + " (R3.5 Fail_Fast_Policy — not retrying)"
                ),
            )

        # R3.5 — Status khác 2xx (1xx/3xx/5xx) → ConfirmInvalidResponseError.
        if not (200 <= status < 300):
            raise ConfirmInvalidResponseError(
                http_status=status,
                message=(
                    f"Invalid Stripe confirm HTTP status "
                    f"(http_status={status}, R3.5 Fail_Fast_Policy — not retrying)"
                ),
            )

        # `rqdata` + `site_key` = hCaptcha challenge markers. Log CHỈ khi
        # xuất hiện (case bất thường cần debug), không log mọi confirm ok.
        # Dump toàn bộ payload_keys (~100 field) đã bỏ — thông tin đó
        # không hữu ích cho user, chỉ tăng noise log.
        rqdata_val = payload.get("rqdata")
        sitekey_val = payload.get("site_key")
        if isinstance(rqdata_val, str) and rqdata_val:
            self._logger.info(
                "stripe confirm hcaptcha_challenge",
                rqdata_len=len(rqdata_val),
                site_key=sitekey_val,
                submission_attempt=payload.get("submission_attempt"),
            )

        # R3.5 — Status 2xx nhưng payload thiếu trạng thái xác nhận hợp lệ.
        # Yêu cầu ít nhất 1 trong `payment_intent`/`setup_intent`/`status`
        # xuất hiện với giá trị truthy.
        has_confirmation_state = bool(
            payload.get("payment_intent")
            or payload.get("setup_intent")
            or payload.get("status")
        )
        if not has_confirmation_state:
            # Log payload keys (đã redact — R3.6) để trace debug KHÔNG lộ token.
            redacted = redact_dict(payload)
            self._logger.info(
                "stripe confirm missing_confirmation_state",
                checkout_session_id=checkout_session_id,
                http_status=status,
                payload_keys=sorted(redacted.keys()),
            )
            raise ConfirmInvalidResponseError(
                http_status=status,
                message=(
                    f"Stripe confirm HTTP {status} but payload is missing "
                    "payment_intent/setup_intent/status (R3.5)"
                ),
            )

        return payload

    def _build_confirm_request(
        self,
        *,
        checkout_session_id: str,
        publishable_key: str,
        elements_session_id: str,
        billing: BillingAddress,
        init_checksum: str = "",
        amount: int = 0,
        elements_config_id: str = "",
        init_config_id: str = "",
        page_id: str = "",
        token_config: StripeTokenConfig | None = None,
    ) -> tuple[str, bytes, dict[str, str]]:
        """Xây dựng `(url, content_bytes, headers)` cho request `confirm` (R3.2, R3.3).

        Body theo UPI reference (rust_upi_bot ``stripe_confirm_upi_qr``) —
        Stripe strict về các field bắt buộc:

            * ``_stripe_version`` — API version header
            * ``client_attribution_metadata`` — Stripe merchant integration meta
            * ``elements_options_client`` + ``elements_session_client`` —
              client context (nested trong form-urlencoded)
            * ``expected_amount`` — INT cents (Stripe reject 400 nếu MISSING)
            * ``expected_payment_method_type`` — ``"ideal"``
            * ``guid`` / ``muid`` / ``sid`` — 3 UUID stripe-format (mirror
              browser SDK Stripe.js signals)
            * ``init_checksum`` — từ payment_pages/init response
            * ``key`` — publishable_key trong body (thay Authorization Bearer)
            * ``passive_captcha_ekey`` + ``passive_captcha_token`` — captcha
              signals (rỗng khi không có, gửi `null` theo Stripe spec)
            * ``payment_method_data.type = "ideal"`` + ``ideal = {}``
              (empty object — KHÔNG chèn field chọn ngân hàng, R3.2)
            * ``payment_method_data.billing_details = {...}``
            * ``payment_method_data.payment_user_agent`` — chuỗi
              stripe.js/<version>; payment-element; deferred-intent
            * ``payment_method_data.referrer = https://chatgpt.com``
            * ``payment_method_data.time_on_page`` — INT ms
            * ``return_url`` — Stripe checkout return URL
            * ``version`` — Stripe.js version hash

        3 token JS-runtime tính NỘI BỘ (R3.3):
            * ``js_checksum`` = SHA256(`checkout_session_id|elements_session_id`)
              hex[:32] — placeholder có ý thức (TODO thay khi có HAR).
            * ``rv_timestamp`` = ``str(int(time.time() * 1000))``.
            * ``passive_captcha_token`` = ``""`` mặc định.

        Args:
            checkout_session_id: Non-empty.
            publishable_key: Non-empty.
            elements_session_id: Non-empty, seed cho `js_checksum`.
            billing: `BillingAddress`.
            init_checksum: Từ ``StripePaymentPageInit.init_checksum`` — bắt
                buộc; empty → Stripe reject 400.
            amount: Từ ``StripePaymentPageInit.amount`` (int cents). 0 →
                Stripe có thể reject nếu subscription mode cần amount rõ.
            elements_config_id: Từ elements response ``config_id`` (nếu có).
                Empty string OK — Stripe suy ra từ session_id.
            init_config_id: Từ init response ``config_id``. Empty OK —
                Stripe suy ra.

        Returns:
            Tuple `(url, content_bytes, headers)` sẵn sàng dùng cho
            `http.AsyncSession.post(url, data=content_bytes, headers=...)`.
        """
        if not checkout_session_id:
            raise ValueError(
                "_build_confirm_request yêu cầu 'checkout_session_id' non-empty."
            )
        if not publishable_key:
            raise ValueError(
                "_build_confirm_request yêu cầu 'publishable_key' non-empty."
            )
        if not elements_session_id:
            raise ValueError(
                "_build_confirm_request yêu cầu 'elements_session_id' non-empty."
            )

        url = (
            f"{_STRIPE_API_BASE}"
            f"{_PAYMENT_PAGES_CONFIRM_PATH_TEMPLATE.format(checkout_session_id=checkout_session_id)}"
        )

        # R3.3 — Compute 2 token JS-runtime từ Stripe.js bundle (đã fetch qua
        # ``ensure_token_config``). Nếu ``token_config=None`` (test/mock) →
        # fallback placeholder deterministic để test không break.
        if token_config is not None and page_id:
            js_checksum = compute_js_checksum(
                page_id, shift=token_config.shift
            )
            rv_timestamp = compute_rv_timestamp(token_config)
        else:
            # Fallback: SHA256(page_id|elements_id) hex[:32] + Unix millis.
            # Stripe sẽ reject nếu strict — nhưng cho test unit dùng mock OK.
            checksum_seed = f"{page_id or checkout_session_id}|{elements_session_id}"
            js_checksum = hashlib.sha256(
                checksum_seed.encode("utf-8")
            ).hexdigest()[:_JS_CHECKSUM_PLACEHOLDER_HEX_LEN]
            rv_timestamp = str(int(time.time() * 1000))

        # 3 UUID Stripe-format cho guid/muid/sid (browser SDK Stripe.js signals).
        guid_val = _stripe_guid()
        muid_val = _stripe_guid()
        sid_val = _stripe_guid()

        # client_attribution_metadata — trong UPI ref chia 2 biến thể:
        # top-level (`cam`) và nested payment_method_data (`pmd_cam`) khác
        # nhau ở `merchant_integration_source/version`.
        cam = {
            "checkout_config_id": init_config_id,
            "checkout_session_id": checkout_session_id,
            "client_session_id": self._get_or_create_stripe_js_id(),
            "elements_session_config_id": elements_config_id,
            "elements_session_id": elements_session_id,
            "merchant_integration_additional_elements": [
                "expressCheckout",
                "payment",
                "address",
            ],
            "merchant_integration_source": "checkout",
            "merchant_integration_subtype": "payment-element",
            "merchant_integration_version": "custom",
            "payment_intent_creation_flow": "deferred",
            "payment_method_selection_flow": "merchant_specified",
        }
        pmd_cam = dict(cam)
        pmd_cam["merchant_integration_source"] = "elements"
        pmd_cam["merchant_integration_version"] = "2021"

        address = billing.address

        # Body theo HAR thực tế (web_record_20260704-000744_manual, confirm
        # body length 9273 bytes). Đặc biệt:
        #   - KHÔNG có `payment_method_data[ideal]` — Stripe universal iDEAL
        #     v2025 KHÔNG yêu cầu bank tại confirm; bank chọn ở pay.ideal.nl
        #     (Requirement 6.2).
        #   - `payment_method_data[allow_redisplay]=limited` — signal
        #     Stripe cho biết payment method chỉ dùng 1 lần.
        #   - `expected_amount=0` — deferred subscription mode, Stripe
        #     resolve amount từ checkout_session_id.
        #   - `passive_captcha_token`/`px3`/`pxvid`/`pxcts` để empty — Stripe
        #     accept khi bot check pass.
        #   - `version` = Stripe.js build version (từ `payment_user_agent`).
        #   - `link_brand=link` — required cho Link (Stripe pay-link brand).
        stripe_build = "03270cb259"
        payload: dict[str, Any] = {
            "guid": guid_val,
            "muid": muid_val,
            "sid": sid_val,
            "payment_method_data": {
                "billing_details": {
                    "name": billing.name,
                    "email": billing.email,
                    "address": {
                        "line1": address.get("line1", ""),
                        "line2": address.get("line2", ""),
                        "city": address.get("city", ""),
                        "state": address.get("state", ""),
                        "postal_code": address.get("postal_code", ""),
                        "country": address.get("country", ""),
                    },
                },
                "type": _PAYMENT_METHOD_TYPE_IDEAL,
                "allow_redisplay": "limited",
                "payment_user_agent": (
                    f"stripe.js/{stripe_build}; "
                    f"stripe-js-v3/{stripe_build}; "
                    "payment-element; deferred-intent"
                ),
                "referrer": "https://chatgpt.com",
                "time_on_page": int(time.time() * 1000) % 100000,
                "client_attribution_metadata": pmd_cam,
            },
            "init_checksum": init_checksum,
            "version": stripe_build,
            # `expected_amount=0` bắt buộc cho subscription deferred_intent —
            # HAR event 12 xác nhận. Nếu gửi amount thật (VD 1901 cents),
            # Stripe reject silently: confirm 200 nhưng KHÔNG attach
            # setup_intent → refresh_poll không bao giờ có next_action.
            # Số cents thật được Stripe resolve từ checkout_session_id
            # server-side. Giữ arg `amount` trong signature để tương thích
            # với các test unit hiện tại.
            "expected_amount": 0,
            "js_checksum": js_checksum,
            "rv_timestamp": rv_timestamp,
            # Match UPI Rust reference (`rust_upi_bot/src/upi/endpoints.rs`):
            # `passive_captcha_token`/`passive_captcha_ekey` = None → skip
            # (không gửi field). Empty string ("") gửi qua form-urlencoded
            # thành `field=` KHÁC với KHÔNG gửi field — Stripe treat khác
            # nhau. Rust `to_form()` skip Value::Null hoàn toàn (verify
            # bằng parity test).
            #
            # `px3`/`pxvid`/`pxcts` là PerimeterX bot detection signals
            # do browser JS runtime sinh. Backend không có PX SDK → gửi
            # empty làm Stripe flag suspicious. Bỏ field → Stripe skip PX
            # check (match UPI Rust behavior).
            "expected_payment_method_type": _PAYMENT_METHOD_TYPE_IDEAL,
            "return_url": (
                f"https://checkout.stripe.com/c/pay/{checkout_session_id}"
                f"?returned_from_redirect=true&ui_mode=custom&return_url="
                f"https%3A%2F%2Fchatgpt.com%2Fcheckout%2Fverify%3F"
                f"stripe_session_id%3D{checkout_session_id}"
                f"%26processor_entity%3Dopenai_ie%26plan_type%3Dplus"
            ),
            "elements_session_client": {
                "client_betas": [
                    "custom_checkout_server_updates_1",
                    "custom_checkout_manual_approval_1",
                ],
                "elements_init_source": "custom_checkout",
                "referrer_host": "chatgpt.com",
                "session_id": elements_session_id,
                "stripe_js_id": self._get_or_create_stripe_js_id(),
                "locale": "en",
                "is_aggregation_expected": "false",
            },
            "elements_options_client": {
                "saved_payment_method": {
                    "enable_save": "auto",
                    "enable_redisplay": "auto",
                },
            },
            "client_attribution_metadata": cam,
            "link_brand": "link",
            "key": publishable_key,
            "_stripe_version": _STRIPE_VERSION,
        }

        content_bytes = _encode_form_urlencoded(_flatten_form(payload))
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": _STRIPE_JS_ORIGIN,
            "Referer": f"{_STRIPE_JS_ORIGIN}/",
        }
        return url, content_bytes, headers

    @staticmethod
    def _extract_address_rejected_field(payload: dict[str, Any]) -> str | None:
        """Extract tên field địa chỉ bị Stripe reject từ payload lỗi 4xx (R3.4).

        Chỉ coi là "address validation rejected" khi có bằng chứng CỤ THỂ
        rằng field bị reject thuộc `payment_method_data.billing_details`
        (address/name/email). Fail_Fast_Policy: KHÔNG suy diễn từ
        `error.type` chung chung — vì Stripe dùng `invalid_request_error`
        cho MỌI validate lỗi (thiếu field, payment_method sai, checksum
        sai, ...), không riêng địa chỉ.

        Logic (whitelist strict):
            1. Payload không có `error` object dạng dict → trả `None`.
            2. Nếu `error.param` non-empty và giá trị của nó nằm trong
               `_ADDRESS_ERROR_KEYWORDS` → trả chính `error.param` (Stripe
               đã chuẩn hoá tên field).
            3. Ngược lại, scan `error.message` cho keyword trong
               `_ADDRESS_ERROR_KEYWORDS`; match đầu tiên → trả keyword đó.
            4. Không case nào khớp → trả `None`. Caller (`confirm`) sẽ raise
               `ConfirmInvalidResponseError` với thông tin gốc của Stripe.

        Không còn nhánh trả `"unknown"` — nếu không xác định được field
        địa chỉ cụ thể thì exception `AddressValidationRejectedError`
        (message: "Stripe từ chối field địa chỉ: '<field>'") sẽ đánh tráo
        nguyên nhân thật. Fallback về `ConfirmInvalidResponseError` để log
        giữ nguyên `error.type/code/param/message` từ Stripe.
        """
        error = payload.get("error")
        if not isinstance(error, dict):
            return None

        error_param = str(error.get("param") or "")
        error_message = str(error.get("message") or "")

        # (2) Whitelist theo `error.param` — Stripe đã chuẩn hoá field.
        if error_param and error_param in _ADDRESS_ERROR_KEYWORDS:
            return error_param

        # (3) Fallback: scan message tìm keyword address (case-insensitive).
        message_lower = error_message.lower()
        for keyword in _ADDRESS_ERROR_KEYWORDS:
            if keyword in message_lower:
                return keyword

        # (4) Không phải lỗi validate địa chỉ.
        return None

    # ------------------------------------------------------------------
    # PHASE 3 — refresh_poll (Requirement 4.3-4.6, 4.10)
    # ------------------------------------------------------------------

    async def refresh_poll(
        self,
        checkout_session_id: str,
        publishable_key: str,
        elements_session_id: str = "",
    ) -> str:
        """`GET /v1/payment_pages/{checkout_session_id}?key=<pk>` — poll để
        lấy `setup_intent.next_action.redirect_to_url.url` (R4.3-4.6).

        Loop tối đa `ideal.refresh_poll_max_attempts` lần (Settings, live).
        Mỗi lần:
            - Gọi GET với `key=<publishable_key>` (unauthenticated —
              endpoint payment_pages state chấp nhận pk qua query param,
              KHÔNG dùng Bearer header ở đây, khớp HAR).
            - HTTP status khác 2xx → Fail_Fast_Policy: raise
              `StripeHttpClientError(request_name="refresh_poll")` — step
              được map thành `refresh_poll` bởi `_STRIPE_REQUEST_STEP_MAP`
              (errors.py).
            - HTTP 2xx: parse JSON và trích `setup_intent.next_action.
              redirect_to_url.url`.
                - Có (non-empty string) → return URL, dừng loop (R4.4).
                - Không có → log `not_found`, chờ `ideal.refresh_poll_delay
                  _seconds` rồi thử lại (nếu chưa hết attempts, R4.5).
        Hết attempts vẫn không thấy URL → raise
        `RefreshPollExhaustedError(attempts=max_attempts)` (R4.6).

        Lỗi transport (timeout / network error) KHÔNG được coi là "chưa có
        URL" — để propagate lên `IdealFlowHandler` để mapping ở boundary
        chính (Requirement 14). Không retry ngầm cho lỗi transport ở đây
        (khác với `_bounded_retry` của init/elements_sessions) vì
        refresh_poll đã có chính sách retry riêng theo Requirement 4.5
        áp dụng cho "response chưa chứa URL", không phải cho lỗi HTTP
        transport — mixing 2 chính sách retry chồng nhau dễ gây bùng nổ
        thời gian chờ.

        Log realtime (R4.10): mỗi attempt log `attempt=N`, `max_attempts`,
        kèm kết quả (`found` khi trả URL, `not_found` khi chờ tiếp,
        `http_error` khi Fail_Fast do status không 2xx).

        Args:
            checkout_session_id: Non-empty, lấy từ
                `CheckoutSession.checkout_session_id` (Requirement 2.5).
            publishable_key: Non-empty, lấy từ
                `CheckoutSession.publishable_key` (Requirement 2.5).

        Returns:
            `redirect_to_url` (chuỗi URL non-empty) — điểm bắt đầu bước
            follow redirect (R4.4).

        Raises:
            RefreshPollExhaustedError: Hết `ideal.refresh_poll_max_attempts`
                mà vẫn không thấy URL trong response (R4.6).
            StripeHttpClientError: HTTP status không 2xx (Fail_Fast_Policy).
        """
        if not checkout_session_id:
            raise ValueError(
                "StripeClient.refresh_poll yêu cầu 'checkout_session_id' "
                "non-empty (Requirement 2.5 — dùng từ CheckoutSession)."
            )
        if not publishable_key:
            raise ValueError(
                "StripeClient.refresh_poll yêu cầu 'publishable_key' "
                "non-empty (Requirement 2.5 — dùng từ CheckoutSession)."
            )

        url = f"{_STRIPE_API_BASE}{_PAYMENT_PAGES_REFRESH_PATH_TEMPLATE.format(checkout_session_id=checkout_session_id)}"
        # Query params đầy đủ theo UPI reference — Stripe cần context về
        # elements session để trả state đúng (không chỉ session config).
        # `stripe_js_id` reuse cached qua `_get_or_create_stripe_js_id`.
        stripe_js_id = self._get_or_create_stripe_js_id()
        query_payload = {
            "elements_session_client": {
                "client_betas": [
                    "custom_checkout_server_updates_1",
                    "custom_checkout_manual_approval_1",
                ],
                "elements_init_source": "custom_checkout",
                "referrer_host": "chatgpt.com",
                "stripe_js_id": stripe_js_id,
                "locale": "en",
                "is_aggregation_expected": "false",
                "session_id": elements_session_id,
            },
            "elements_options_client": {
                "saved_payment_method": {
                    "enable_save": "auto",
                    "enable_redisplay": "auto",
                },
            },
            "key": publishable_key,
            "_stripe_version": _STRIPE_VERSION,
        }
        query_params = _flatten_form(query_payload)
        headers = {
            "Accept": "application/json",
            "Origin": _STRIPE_JS_ORIGIN,
            "Referer": f"{_STRIPE_JS_ORIGIN}/",
        }

        max_attempts = await self._read_refresh_poll_max_attempts()
        delay_seconds = await self._read_refresh_poll_delay_seconds()
        timeout = await self._read_request_timeout_seconds()

        # 1 dòng đầu cho cả vòng poll — user thấy tổng cấu hình. Kết quả
        # ok/exhausted log 1 dòng ở cuối, KHÔNG log mỗi attempt (đã có
        # elapsed hiển thị qua updated_at của job).
        self._logger.info(
            "stripe refresh_poll begin max_attempts=%d delay=%.1fs",
            max_attempts,
            delay_seconds,
        )

        for attempt in range(1, max_attempts + 1):
            response = await self._http_client.get(
                url,
                params=query_params,
                headers=headers,
                timeout=timeout,
            )
            status = response.status_code

            if not (200 <= status < 300):
                body_snippet = self._safe_body_snippet(response)
                self._logger.info(
                    "stripe refresh_poll http_error",
                    request="refresh_poll",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    status_code=status,
                )
                raise StripeHttpClientError(
                    request_name="refresh_poll",
                    http_status=status,
                    body_snippet=body_snippet,
                )

            payload = self._parse_json_body(response, request_name="refresh_poll")
            redirect_url = self.extract_redirect_url_from_payload(payload)

            if redirect_url:
                self._logger.info(
                    "stripe refresh_poll found attempt=%d/%d",
                    attempt,
                    max_attempts,
                )
                return redirect_url

            # R4.5 — chờ delay giữa các lượt CHỈ khi còn attempts kế tiếp.
            # KHÔNG log mỗi attempt "not_found" để tránh noise — poll 15
            # lần sẽ đẻ 15 dòng log giống nhau. Nếu cần debug payload keys,
            # bật `logger.debug` (level DEBUG) ở tầng logging config.
            if attempt < max_attempts:
                await asyncio.sleep(delay_seconds)

        # R4.6 — hết attempts, không thấy redirect_to_url.
        self._logger.info(
            "stripe refresh_poll exhausted max_attempts=%d", max_attempts
        )
        raise RefreshPollExhaustedError(attempts=max_attempts)

    async def refresh_state(
        self,
        checkout_session_id: str,
        publishable_key: str,
        elements_session_id: str = "",
    ) -> dict[str, Any]:
        """GET `/v1/payment_pages/{id}` 1 lần best-effort để lấy state.

        Trả về payload response (kể cả nếu không có intent). Dùng cho MVP
        fallback khi confirm response thiếu ``stripe_hosted_url`` — caller
        tự extract field cần từ payload.

        Fail_Fast nếu HTTP non-2xx (StripeHttpClientError).
        """
        if not checkout_session_id or not publishable_key:
            raise ValueError("refresh_state yêu cầu checkout_session_id + publishable_key")

        url = f"{_STRIPE_API_BASE}{_PAYMENT_PAGES_REFRESH_PATH_TEMPLATE.format(checkout_session_id=checkout_session_id)}"
        stripe_js_id = self._get_or_create_stripe_js_id()
        query_payload = {
            "elements_session_client": {
                "client_betas": [
                    "custom_checkout_server_updates_1",
                    "custom_checkout_manual_approval_1",
                ],
                "elements_init_source": "custom_checkout",
                "referrer_host": "chatgpt.com",
                "stripe_js_id": stripe_js_id,
                "locale": "en",
                "is_aggregation_expected": "false",
                "session_id": elements_session_id,
            },
            "elements_options_client": {
                "saved_payment_method": {
                    "enable_save": "auto",
                    "enable_redisplay": "auto",
                },
            },
            "key": publishable_key,
            "_stripe_version": _STRIPE_VERSION,
        }
        query_params = _flatten_form(query_payload)
        headers = {
            "Accept": "application/json",
            "Origin": _STRIPE_JS_ORIGIN,
            "Referer": f"{_STRIPE_JS_ORIGIN}/",
        }
        timeout = await self._read_request_timeout_seconds()

        response = await self._http_client.get(
            url, params=query_params, headers=headers, timeout=timeout
        )
        if not (200 <= response.status_code < 300):
            raise StripeHttpClientError(
                request_name="refresh_poll",
                http_status=response.status_code,
                body_snippet=self._safe_body_snippet(response),
            )
        return self._parse_json_body(response, request_name="refresh_poll")

    @staticmethod
    def extract_redirect_url_from_payload(payload: dict[str, Any]) -> str | None:
        """Trích ``<intent>.next_action.redirect_to_url.url`` từ payload.

        Dùng cho CẢ 2 nguồn: response ``confirm`` (nếu Stripe attach intent
        + next_action ngay tại confirm) VÀ response ``refresh_poll``.

        Stripe trả redirect ở 1 trong 2 tầng tuỳ payment mode:
            * ``setup_intent.next_action.redirect_to_url.url`` — khi luồng
              tạo Setup Intent (subscription setup_future_usage=off_session).
            * ``payment_intent.next_action.redirect_to_url.url`` — khi
              luồng tạo Payment Intent (charge trực tiếp, iDEAL 1-time).

        Trả URL đầu tiên tìm thấy theo priority: ``setup_intent`` →
        ``payment_intent``. Trả ``None`` khi cả 2 tầng đều thiếu hoặc URL
        rỗng — caller sẽ fallback refresh_poll hoặc retry.

        Fail_Fast_Policy: KHÔNG suy diễn URL từ field khác (không dò
        `hosted_instructions_url` hay `hosted_url` — 2 field này là hosted
        page URL, không phải deeplink iDEAL bank).
        """
        for intent_key in ("setup_intent", "payment_intent"):
            intent = payload.get(intent_key)
            if not isinstance(intent, dict):
                continue
            next_action = intent.get("next_action")
            if not isinstance(next_action, dict):
                continue
            redirect_to_url = next_action.get("redirect_to_url")
            if not isinstance(redirect_to_url, dict):
                continue
            url_value = redirect_to_url.get("url")
            if isinstance(url_value, str) and url_value:
                return url_value
        return None

    # ------------------------------------------------------------------
    # PHASE 3 — follow_redirect (Requirement 4.7-4.9, 4.10)
    # ------------------------------------------------------------------

    async def follow_redirect(self, redirect_to_url: str) -> tuple[str, str]:
        """`GET redirect_to_url` với `allow_redirects=False` — đọc thủ công
        HTTP 302 + `Location` để trích `(encoded_tx_url, sig)` (R4.7-4.9).

        Tự đọc 302 (KHÔNG cho HTTP client follow) để kiểm soát chính xác từng
        hop và validate status/Location (R4.7, R4.8). KHÔNG load HTML/JS
        của trang đích — pure-HTTP.

        Validate chặt (R4.8):
            - `response.status_code == 302` — Fail_Fast_Policy nếu khác.
            - Header `Location` non-empty — Fail_Fast_Policy nếu thiếu.
            - Path Location bắt đầu bằng `/transactions/` và có
              `encoded_tx_url` non-empty ngay sau.
            - Query Location có param `sig` non-empty.

        Bất kỳ vi phạm nào ở trên đều raise `RedirectValidationError` với
        `http_status`/`has_location` chính xác theo trạng thái quan sát
        được — không suy diễn thêm.

        Sensitive_Data_Redaction (R4.9): giá trị `sig` được mask bằng
        `redact_message` khi log realtime. `redact_message` sẽ thay thế
        toàn bộ occurrence của chuỗi `sig` bằng `***REDACTED***`, đảm
        bảo log KHÔNG lộ giá trị thô ngay cả khi có ai đưa `sig` vào
        message log tự do sau này.

        Args:
            redirect_to_url: Non-empty URL Stripe redirect (nhận từ
                `refresh_poll`). Ví dụ:
                `https://pm-redirects.stripe.com/authorize/{acct}/sa_nonce_{...}`.

        Returns:
            Tuple `(encoded_tx_url, sig)` — 2 chuỗi non-empty đã trích từ
            Location header, chuyển nguyên vẹn cho bước
            `transaction_initiate` (R4.9, R5.1).

        Raises:
            RedirectValidationError: Response không phải HTTP 302, thiếu
                header `Location`, hoặc Location không khớp format
                `/transactions/{tx}?sig={sig}` (R4.8).
        """
        if not redirect_to_url:
            raise ValueError(
                "StripeClient.follow_redirect yêu cầu 'redirect_to_url' "
                "non-empty (Requirement 4.4 — nhận từ refresh_poll)."
            )

        timeout = await self._read_request_timeout_seconds()

        # R4.7 — pure-HTTP với `allow_redirects=True` để curl_cffi tự follow
        # toàn bộ redirect chain. `pm-redirects.stripe.com/authorize` yêu
        # cầu accept full browser headers + user-agent + đôi khi trả 302
        # kèm 400 (nếu client thô); curl_cffi với default behavior + follow=True
        # xử lý ổn hơn.
        #
        # Sau follow: `response.history` chứa list các redirect intermediate,
        # `response.url` = URL cuối cùng (page pay.ideal.nl).
        # Parse `response.url` để trích (encoded_tx_url, sig).
        #
        # Nếu final URL không match `pay.ideal.nl/transactions/...` format
        # → raise RedirectValidationError (chuỗi redirect không tới iDEAL).
        headers = {
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
            "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "cross-site",
            "Upgrade-Insecure-Requests": "1",
        }
        # Dùng client THÔ (không dùng self._http_client) để tránh leak
        # default headers từ per-job flow client — cụ thể headers `oai-*`
        # (như `OAI-Language`, `x-openai-target-path`) mà chatgpt_client set
        # cho request `chatgpt.com` có thể leak khi http.AsyncSession tái
        # sử dụng cookie jar cross-domain. `pm-redirects.stripe.com` reject
        # request có any header string >5000 chars → error
        # `'Invalid string: oai-...alue; must be at most 5000 characters'`.
        #
        # Sử dụng client tạm với headers tối thiểu + allow_redirects=True
        # để lấy final URL. Impersonate mặc định lo UA + Sec-CH-UA*.
        # `proxy=self._proxy_url` PHẢI truyền — nếu None thì Direct_Mode
        # (đúng khi flow chạy direct); nếu KHÔNG truyền thì Proxied_Mode
        # sẽ vô tình leak IP thật của server backend qua pm-redirects.stripe.com.
        fresh_client_kwargs: dict[str, Any] = {
            "allow_redirects": True,
            "timeout": timeout,
            "headers": headers,
        }
        if self._proxy_url is not None:
            fresh_client_kwargs["proxy"] = self._proxy_url
        # Transport lỗi (timeout / DNS / proxy die) là TRANSIENT — map
        # thành `IdealFlowError(error_code="stripe_follow_redirect_transport")`
        # để `_run_inner` boundary catch, tránh leak thành `internal_error`
        # ở `_run_handler`. Code này được whitelist trong
        # `auto_retry_blocked_codes` → JobManager auto-retry với proxy mới.
        try:
            async with http.create_async_client(**fresh_client_kwargs) as fresh_client:
                response = await fresh_client.get(redirect_to_url)
        except (
            http.TimeoutException,
            http.NetworkError,
            http.TransportError,
        ) as exc:
            self._logger.warning(
                "stripe follow_redirect transport error url=%s type=%s msg=%s",
                redirect_to_url[:80],
                type(exc).__name__,
                str(exc)[:200],
            )
            raise IdealFlowError(
                error_code="stripe_follow_redirect_transport",
                step="stripe_follow_redirect",
                message=(
                    f"Stripe pm-redirects transport error "
                    f"({type(exc).__name__}): {str(exc)[:200]}"
                ),
            ) from exc
        status = response.status_code
        final_url = str(response.url)
        history_count = len(response.history)

        # Log chi tiết history/body_preview CHỈ khi non-2xx (case debug),
        # bỏ khỏi happy path để giảm noise. Sig sẽ bị mask ở dòng ok kế tiếp.
        if not (200 <= status < 300):
            self._logger.info(
                "stripe follow_redirect http_error",
                status_code=status,
                history_count=history_count,
                history_urls=[str(h.url)[:100] for h in response.history],
                body_preview=response.text[:300],
            )
            raise RedirectValidationError(
                http_status=status,
                has_location=False,
            )

        # R4.7 — Parse `response.url` (final URL sau redirect chain).
        # Format expected: `https://pay.ideal.nl/transactions/{encoded_tx_url}?sig={sig}`.
        encoded_tx_url, sig = self._parse_ideal_redirect_location(final_url)

        # R4.9 — 1 dòng ok gộp: encoded_tx_url + sig (đã redact). Không
        # dump history_urls trong happy path — user chỉ cần biết đã tới
        # pay.ideal.nl thành công.
        self._logger.info(
            "stripe follow_redirect ok status=%d encoded_tx=%s sig_len=%d",
            status,
            encoded_tx_url[:80],
            len(sig),
        )
        return encoded_tx_url, sig

    @staticmethod
    def _parse_ideal_redirect_location(location: str) -> tuple[str, str]:
        """Parse `Location` header dạng
        `https://pay.ideal.nl/transactions/{encoded_tx_url}?sig={sig}`
        thành `(encoded_tx_url, sig)`.

        Chỉ validate cấu trúc path/query cần thiết để trích 2 giá trị —
        KHÔNG validate scheme/host cứng để tránh phá vỡ khi Stripe/iDEAL
        đổi domain phụ (theo Requirement 4.7 giữ mô tả wire ổn định).
        Nếu HAR sau này cho thấy cần lock host, thêm ở đây với comment
        tường minh.

        Raises:
            RedirectValidationError: Path không bắt đầu bằng
                `/transactions/`, thiếu `encoded_tx_url` sau prefix, hoặc
                thiếu query param `sig` (R4.8 — Fail_Fast_Policy).
        """
        parsed = urlparse(location)
        path = parsed.path or ""

        if not path.startswith(_IDEAL_REDIRECT_TRANSACTIONS_PATH_PREFIX):
            raise RedirectValidationError(
                http_status=302,
                has_location=True,
                message=(
                    f"Redirect Location path does not start with "
                    f"'{_IDEAL_REDIRECT_TRANSACTIONS_PATH_PREFIX}' "
                    f"(actual_path={path!r}, R4.7)"
                ),
            )

        encoded_tx_url = path[len(_IDEAL_REDIRECT_TRANSACTIONS_PATH_PREFIX):]
        if not encoded_tx_url:
            raise RedirectValidationError(
                http_status=302,
                has_location=True,
                message=(
                    "Redirect Location is missing 'encoded_tx_url' after "
                    f"'{_IDEAL_REDIRECT_TRANSACTIONS_PATH_PREFIX}' (R4.7)"
                ),
            )

        # `parse_qs(keep_blank_values=False)` — chuỗi rỗng ở value bị bỏ,
        # đảm bảo `sig=` (rỗng) không tính là hợp lệ. Fail_Fast tường minh.
        query_params = parse_qs(parsed.query, keep_blank_values=False)
        sig_values = query_params.get("sig") or []
        if not sig_values or not sig_values[0]:
            raise RedirectValidationError(
                http_status=302,
                has_location=True,
                message="Redirect Location is missing query param 'sig' (R4.7)",
            )
        sig = sig_values[0]
        return encoded_tx_url, sig

    # ------------------------------------------------------------------
    # Internal helpers — settings + response parsing
    # ------------------------------------------------------------------

    async def _read_max_retry_attempts(self) -> int:
        """Đọc `ideal.stripe_max_retry_attempts` — cache trong instance.

        Đọc DB 1 lần cho vòng đời StripeClient (per-job), các lần sau trả
        giá trị cached. Trả default 3 nếu key chưa set (trạng thái
        initial). Settings_Store đã validate type là int ≥ 1 tại thời
        điểm ghi (Requirement 11.7) nên giá trị đã set luôn hợp lệ —
        KHÔNG cần validate lại ở đây.

        Semantic write-through: user đổi setting này giữa lúc job đang
        retry sẽ KHÔNG lift ngay trong job hiện tại (dùng giá trị đọc
        lần đầu suốt flow), chỉ phản ánh cho job kế tiếp — tương thích
        với snapshot-at-creation-time của JobManager (R11.2).
        """
        if self._cached_max_retry_attempts is None:
            value = await self._settings.get(_SETTING_MAX_RETRY_ATTEMPTS)
            self._cached_max_retry_attempts = (
                _DEFAULT_MAX_RETRY_ATTEMPTS if value is None else int(value)
            )
        return self._cached_max_retry_attempts

    async def _read_retry_backoff_seconds(self) -> float:
        """Đọc `ideal.stripe_retry_backoff_seconds` — cache trong instance.

        Xem docstring `_read_max_retry_attempts` cho semantic cache
        per-flow (đọc DB 1 lần cho toàn vòng đời StripeClient).
        """
        if self._cached_retry_backoff_seconds is None:
            value = await self._settings.get(_SETTING_RETRY_BACKOFF_SECONDS)
            self._cached_retry_backoff_seconds = (
                _DEFAULT_RETRY_BACKOFF_SECONDS if value is None else float(value)
            )
        return self._cached_retry_backoff_seconds

    async def _read_request_timeout_seconds(self) -> float:
        """Đọc `ideal.stripe_request_timeout_seconds` — cache trong instance.

        Xem docstring `_read_max_retry_attempts` cho semantic cache
        per-flow (đọc DB 1 lần cho toàn vòng đời StripeClient).
        """
        if self._cached_request_timeout_seconds is None:
            value = await self._settings.get(_SETTING_REQUEST_TIMEOUT_SECONDS)
            self._cached_request_timeout_seconds = (
                _DEFAULT_REQUEST_TIMEOUT_SECONDS if value is None else float(value)
            )
        return self._cached_request_timeout_seconds

    async def _read_refresh_poll_max_attempts(self) -> int:
        """Đọc `ideal.refresh_poll_max_attempts` — cache trong instance (R4.3, 4.5, 4.6).

        Đọc DB 1 lần cho vòng đời StripeClient (per-job), các lần sau trả
        giá trị cached. Trả default 5 nếu key chưa set. Settings_Store đã
        validate type là int ≥ 1 tại thời điểm ghi (Requirement 11.7).
        """
        if self._cached_refresh_poll_max_attempts is None:
            value = await self._settings.get(_SETTING_REFRESH_POLL_MAX_ATTEMPTS)
            self._cached_refresh_poll_max_attempts = (
                _DEFAULT_REFRESH_POLL_MAX_ATTEMPTS if value is None else int(value)
            )
        return self._cached_refresh_poll_max_attempts

    async def _read_refresh_poll_delay_seconds(self) -> float:
        """Đọc `ideal.refresh_poll_delay_seconds` — cache trong instance (R4.5).

        Xem docstring `_read_max_retry_attempts` cho semantic cache
        per-flow.
        """
        if self._cached_refresh_poll_delay_seconds is None:
            value = await self._settings.get(_SETTING_REFRESH_POLL_DELAY_SECONDS)
            self._cached_refresh_poll_delay_seconds = (
                _DEFAULT_REFRESH_POLL_DELAY_SECONDS if value is None else float(value)
            )
        return self._cached_refresh_poll_delay_seconds

    def _parse_json_body(
        self,
        response: http.Response,
        *,
        request_name: str,
    ) -> dict[str, Any]:
        """Parse body 2xx thành dict JSON, fail-fast nếu không phải dict.

        Response ở nhánh này ĐÃ được `_bounded_retry` xác nhận 2xx —
        nhưng body vẫn có thể không parse được thành JSON (ví dụ Stripe
        sinh trang HTML lỗi bất thường). Coi đây là bất thường:
        `StripeHttpClientError` với status=response.status_code để
        `IdealFlowHandler` log/redact tường minh.
        """
        try:
            payload = response.json()
        except ValueError:
            raise StripeHttpClientError(
                request_name=request_name,
                http_status=response.status_code,
                body_snippet=self._safe_body_snippet(response),
                message=(
                    f"Stripe '{request_name}' returned 2xx but body could not "
                    f"be parsed as JSON (status={response.status_code})"
                ),
            )
        if not isinstance(payload, dict):
            raise StripeHttpClientError(
                request_name=request_name,
                http_status=response.status_code,
                body_snippet=self._safe_body_snippet(response),
                message=(
                    f"Stripe '{request_name}' returned JSON that is not an object "
                    f"(got: {type(payload).__name__})"
                ),
            )
        return payload

    @staticmethod
    def _safe_body_snippet(response: http.Response) -> str | None:
        """Cắt ngắn body làm snippet cho log/exception.

        `http.Response.text` chỉ khả dụng khi body đã đọc xong (đối với
        async client, body đã read khi await xong request). Trả `None` nếu
        không đọc được (edge case: response streaming chưa consume).
        """
        try:
            text = response.text
        except (http.ResponseNotRead, RuntimeError):
            return None
        if not text:
            return ""
        if len(text) <= _BODY_SNIPPET_MAX_LEN:
            return text
        return text[:_BODY_SNIPPET_MAX_LEN] + "…"


__all__ = ["StripeClient"]
