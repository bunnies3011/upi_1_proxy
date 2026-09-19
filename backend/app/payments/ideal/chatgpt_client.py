"""ChatgptClient — pure-HTTP login/checkout/approve cho ChatGPT (`payments/ideal/`).

PHASE 1 (task 15.1, module này): `parse_account_line`, `classify_login_error`,
`login`, `revalidate` — Requirements 1.1, 1.2, 1.3, 1.4, 10.4.

PHASE 2 (task 15.4): `create_checkout`, `approve` — Requirements 1.5-1.9, 4.1-4.2.

Payment_Module_Boundary (Requirement 13.6): module này chỉ import từ
`app.core.*` (session_cache, redaction, payment_flow) và `app.payments.ideal.*`
(models, errors, chatgpt_login). KHÔNG import bất kỳ payment method khác.

Sensitive_Data_Redaction (Requirement 1.9): mọi log entry của module này áp
`redact_dict()` cho payload dict và `redact_message()` cho chuỗi tự do khi có
`known_secrets` (password/totp_secret/access_token). Payload gốc chứa
credential KHÔNG BAO GIỜ được log thô.

Login flow (2026-07 refactor):
- `login()` delegate sang `chatgpt_login.login_pure_request()` — port từ
  Rust UPI bot (``rust_upi_bot/src/auth/mod.rs``) đã production-tested.
  Endpoint thực tế:
    * `GET  chatgpt.com/auth/login` (prime Cloudflare cookie)
    * `GET  chatgpt.com/api/auth/csrf` (CSRF token)
    * `POST chatgpt.com/api/auth/signin/openai` (authorize URL)
    * `GET  auth.openai.com/authorize?…` (landing → device_id)
    * `POST sentinel.openai.com/backend-api/sentinel/req` (PoW challenge)
    * `POST auth.openai.com/api/accounts/password/verify` (password)
    * `POST auth.openai.com/api/accounts/mfa/verify` (TOTP nếu có)
    * follow-redirect → `chatgpt.com/api/auth/callback/openai?code=…`
    * `GET  chatgpt.com/api/auth/session` (accessToken)
- `revalidate()` giữ nguyên logic (GET `/api/auth/session` với cookies restore).
- Các endpoint NextAuth cũ (``/api/auth/callback/credentials``,
  ``/api/auth/callback/totp``) đã bị loại bỏ — ChatGPT dùng OAuth flow qua
  ``auth.openai.com``, KHÔNG dùng NextAuth credentials provider.

Cache integration (Requirement 1.2, 10.4):
- Module này KHÔNG tự cache. Caller (`IdealFlowHandler`) có trách nhiệm:
    1. Gọi `session_cache.get(account_key)` trước `login`.
    2. Nếu cache hit → gọi `revalidate(session)`.
    3. Nếu `revalidate` trả False → `session_cache.clear(account_key)` +
       fallback sang `login(...)`.
    4. Sau khi `login` thành công → `session_cache.save(account_key, payload)`.
- `session_cache` được inject vào constructor để mở rộng trong tương lai
  (ví dụ cache internal của bước intermediate trong login flow — hiện chưa
  cần).
"""

from __future__ import annotations

from typing import Any

from app.core import http_client as http
from app.core.payment_flow import AccountLineError
from app.core.redaction import redact_dict
from app.payments._chatgpt.login_client import (
    ChatgptLoginClient,
    _CHATGPT_BASE_URL,
    _ENDPOINT_SESSION,
    _restore_cookies_scoped,
)
from app.payments.ideal.errors import (
    ApproveFailedError,
    IdealFlowError,
)
from app.payments.ideal.models import (
    BillingAddress,
    CheckoutSession,
    IdealParsedAccount,
    SessionBundle,
    parse_account_line as _parse_account_line,
    parse_checkout_session,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: `_CHATGPT_BASE_URL`, `_ENDPOINT_SESSION`, and the cookie helper
#: `_restore_cookies_scoped` are imported from the shared login client
#: (`payments/_chatgpt/login_client.py`) — single source of truth, reused by the
#: login base and these checkout methods.

#: Endpoint tạo checkout session (Requirement 1.5).
_ENDPOINT_CHECKOUT = "/backend-api/payments/checkout"

#: Endpoint approve checkout (Requirement 4.1).
_ENDPOINT_APPROVE = "/backend-api/payments/checkout/approve"

#: Endpoint gửi snapshot billing address (HAR
#: `web_record_20260705-155711_manual` events 002-004 — HTTP 204 No Content).
#: Trước khi gọi `/approve`, ChatGPT cần biết billing_address user gõ ở UI
#: custom-checkout — snapshot progressive send mỗi lần user gõ 1 field.
#: Backend HTTP flow chỉ cần gọi 1 lần với billing_address ĐẦY ĐỦ trước
#: approve. Nếu skip: approve trả 200 với `result='approved'` nhưng Stripe
#: KHÔNG transition submission_attempt.state → setup_intent không attach.
_ENDPOINT_SNAPSHOT = "/backend-api/payments/checkout/snapshot"

#: Endpoint entitlement live — đọc plan hiện tại trực tiếp từ backend
#: (KHÔNG lệ thuộc JWT session cache có thể lag sau khi user upgrade). Dùng
#: cho `check_plan_status` — port từ gpt_signup_hybrid (UPI flow) đã verify
#: reflect Plus ngay sau khi thanh toán xong. Bearer accessToken bắt buộc,
#: kèm header recipe `x-openai-target-*` + `OAI-Language` — recipe rút gọn
#: (chỉ Bearer + UA) sẽ bị Cloudflare chặn 403.
_ENDPOINT_ENTITLEMENT = "/backend-api/accounts/check/v4-2023-04-27"

#: Billing_details cố định theo Requirement 1.5 — locale NL/EUR.
_BILLING_COUNTRY_NL = "NL"
_BILLING_CURRENCY_EUR = "EUR"

#: Plan name theo Requirement 1.5.
_PLAN_NAME_CHATGPT_PLUS = "chatgptplusplan"

#: Giá trị `processor_entity` mong đợi theo Requirement 1.8 — nếu response
#: trả giá trị khác, chỉ log warning, KHÔNG raise (flow tiếp tục với giá
#: trị thực tế nhận được).
_EXPECTED_PROCESSOR_ENTITY = "openai_ie"

#: Giá trị `result` payload approve tính là thành công (Requirement 4.1, 4.2).
_APPROVE_RESULT_APPROVED = "approved"

#: Số ký tự tối đa của body snippet cắt ngắn khi log lỗi HTTP (tránh spam
#: log/leak dữ liệu).
_HTTP_ERROR_BODY_SNIPPET_MAX_CHARS = 500

#: 4 nhóm phân loại lỗi login theo Requirement 1.4.
LOGIN_ERROR_INVALID_CREDENTIAL = "invalid_credential"
LOGIN_ERROR_MFA_REQUIRED = "mfa_required"
LOGIN_ERROR_ACCOUNT_LOCKED = "account_locked"
LOGIN_ERROR_NETWORK = "network_error"

#: Keyword heuristics cho `classify_login_error`. Match case-insensitive trong
#: JSON payload / plain body. Khi có HAR, thay bằng field cụ thể của response.
_KEYWORDS_INVALID_CREDENTIAL: tuple[str, ...] = (
    "invalid_credential",
    "invalidcredentials",
    "wrong password",
    "incorrect password",
    "password_incorrect",
    "email_or_password_incorrect",
)
_KEYWORDS_MFA_REQUIRED: tuple[str, ...] = (
    "mfa_required",
    "totp_required",
    "two_factor_required",
    "mfa",
    "otp_required",
)
_KEYWORDS_ACCOUNT_LOCKED: tuple[str, ...] = (
    "account_locked",
    "account_disabled",
    "account_suspended",
    "user_disabled",
    "banned",
    "locked",
    "disabled",
)


# ---------------------------------------------------------------------------
# Module-level helper (re-export theo yêu cầu task 15.1)
# ---------------------------------------------------------------------------


def parse_account_line(raw_line: str) -> IdealParsedAccount | AccountLineError:
    """Re-export thin wrapper của `app.payments.ideal.models.parse_account_line`.

    KHÔNG duplicate logic — mọi rule parse (định dạng 2/3 phần, email validation,
    Requirement 1.1) nằm 1 chỗ duy nhất trong `models.py`. Hàm này chỉ để caller
    import từ `chatgpt_client` như 1 group API tự nhiên (tương tự pattern các
    module đi cùng `parse_*` trong `models.py`).

    Xem docstring gốc: `app.payments.ideal.models.parse_account_line`.
    """
    return _parse_account_line(raw_line)


def _parse_entitlement_plan(data: dict[str, Any]) -> dict[str, Any]:
    """Parse entitlement block từ `/backend-api/accounts/check/v4` → plan dict.

    Pure function (không network) để dễ unit-test. Shape thực tế:
        accounts.default.entitlement.{subscription_plan, has_active_subscription,
                                      expires_at, subscription_id}

    Nếu `accounts` không có key "default" (edge case) → lấy account đầu tiên
    là dict.

    `subscription_plan` (vd `chatgptplusplan`) rút gọn: bỏ prefix `chatgpt` +
    suffix `plan` → `plus` / `free` / `team` / `pro` / ...

    Trả blank khi shape thiếu/sai (KHÔNG raise) để caller fail-soft.
    """
    blank: dict[str, Any] = {
        "plan": None,
        "has_active_subscription": False,
        "expires_at": None,
    }
    if not isinstance(data, dict):
        return blank
    accounts = data.get("accounts")
    if not isinstance(accounts, dict) or not accounts:
        return blank
    acct = accounts.get("default")
    if not isinstance(acct, dict):
        acct = next((v for v in accounts.values() if isinstance(v, dict)), None)
    if not isinstance(acct, dict):
        return blank
    ent = acct.get("entitlement")
    if not isinstance(ent, dict):
        return blank

    raw_plan = ent.get("subscription_plan")
    label: str | None = None
    if isinstance(raw_plan, str) and raw_plan.strip():
        s = raw_plan.strip().lower()
        if s.startswith("chatgpt"):
            s = s[len("chatgpt") :]
        if s.endswith("plan"):
            s = s[: -len("plan")]
        label = s or None

    return {
        "plan": label,
        "has_active_subscription": bool(ent.get("has_active_subscription")),
        "expires_at": ent.get("expires_at"),
    }


def _classify_plan_from_session(payload: dict[str, Any]) -> tuple[str, str]:
    """Heuristic classify plan từ payload `/api/auth/session` — fallback.

    Chỉ được `check_plan_status` gọi khi entitlement live fail. Sử dụng
    shared `classify_plan_from_session` hỗ trợ account.planType và JWT claims.
    """
    from app.payments._chatgpt.plan_status import classify_plan_from_session

    return classify_plan_from_session(payload)



# ---------------------------------------------------------------------------
# ChatgptClient
# ---------------------------------------------------------------------------


class ChatgptClient(ChatgptLoginClient):
    """iDEAL checkout client — extends `ChatgptLoginClient` with the ChatGPT
    Stripe-checkout surface (`create_checkout`, `snapshot_billing`, `approve`,
    `check_plan_status`, `_fetch_entitlement`).

    The login/session surface (`login`, `revalidate`, `hydrate_from_cache`,
    `reset_openai_cookies`) and the constructor
    (`__init__(http_client, session_cache, logger)`) are inherited from
    `ChatgptLoginClient`. `classify_login_error` (a pure static classifier) and
    the module-level `parse_account_line` re-export stay here so existing call
    sites/tests importing them from `chatgpt_client` remain unchanged.
    """

    # ---- classify_login_error (R1.4) ------------------------------------

    @staticmethod
    def classify_login_error(
        response: http.Response | None,
        exception: Exception | None,
    ) -> str:
        """Phân loại nguyên nhân login thất bại thành 1 trong 4 nhóm R1.4.

        Trả về 1 trong: `LOGIN_ERROR_INVALID_CREDENTIAL`,
        `LOGIN_ERROR_MFA_REQUIRED`, `LOGIN_ERROR_ACCOUNT_LOCKED`,
        `LOGIN_ERROR_NETWORK`.

        Thứ tự phân loại (deterministic, không đảo được):
            1. `exception` là `http.TimeoutException` / `http.NetworkError`
               / `http.TransportError` → `network_error`.
            2. `response` None (không có HTTP response) → `network_error`.
            3. Body chứa keyword `mfa_required` / `totp_required` / ... →
               `mfa_required` (ưu tiên trước credential vì MFA có thể xuất
               hiện ở status 401 giống credential fail).
            4. `response.status_code == 403` với keyword `account_locked`/
               `disabled`/... → `account_locked`.
            5. `response.status_code in (401, 403)` với keyword
               `invalid_credential`/`password`/... → `invalid_credential`.
            6. Fallback (status 5xx, status không xác định, body không match
               keyword nào) → `network_error`.

        Args:
            response: HTTP response nhận được, nếu có. `None` khi không lấy
                được response (transport-level failure).
            exception: Exception bắt được trong quá trình gọi HTTP, nếu có.

        Note:
            Heuristic keyword-matching là placeholder — khi có HAR thực tế
            của chatgpt.com login flow, thay heuristic bằng field cụ thể của
            payload để chính xác 100%. Interface method KHÔNG đổi.
        """
        # (1) Transport-level errors.
        if exception is not None and isinstance(
            exception, (http.TimeoutException, http.NetworkError, http.TransportError)
        ):
            return LOGIN_ERROR_NETWORK

        # (2) Không có response → coi như network error.
        if response is None:
            return LOGIN_ERROR_NETWORK

        # Chuẩn hoá body để match keyword (case-insensitive, JSON stringify nếu cần).
        body_text = ChatgptClient._extract_response_body_text(response).lower()
        status = response.status_code

        # (3) MFA required — check trước credential vì MFA response có thể là 401.
        if ChatgptClient._contains_any_keyword(body_text, _KEYWORDS_MFA_REQUIRED):
            return LOGIN_ERROR_MFA_REQUIRED

        # (4) Account locked — chỉ áp dụng cho 403 để tránh false positive.
        if status == 403 and ChatgptClient._contains_any_keyword(
            body_text, _KEYWORDS_ACCOUNT_LOCKED
        ):
            return LOGIN_ERROR_ACCOUNT_LOCKED

        # (5) Invalid credential — 401/403 với keyword.
        if status in (401, 403) and ChatgptClient._contains_any_keyword(
            body_text, _KEYWORDS_INVALID_CREDENTIAL
        ):
            return LOGIN_ERROR_INVALID_CREDENTIAL

        # (6) Fallback — không xác định rõ nguyên nhân phía account.
        return LOGIN_ERROR_NETWORK

    @staticmethod
    def _extract_response_body_text(response: http.Response) -> str:
        """Lấy body text an toàn từ response — không raise nếu decode fail.

        Ưu tiên JSON (khi Content-Type là application/json) để keyword match
        trên cả field name/value; fallback về `response.text` nếu parse JSON
        fail. Trả empty string nếu cả 2 đều fail (KHÔNG raise — classify
        method phải luôn deterministic).
        """
        try:
            if "application/json" in response.headers.get("content-type", "").lower():
                # Dump lại JSON để chắc chắn key/value đều nằm trong string
                # để keyword match — tránh miss keyword ở nested key.
                import json  # local import — không phải hot path.

                return json.dumps(response.json(), separators=(",", ":"))
        except (ValueError, http.DecodingError):
            pass
        try:
            return response.text
        except (UnicodeDecodeError, http.DecodingError):
            return ""

    @staticmethod
    def _contains_any_keyword(text: str, keywords: tuple[str, ...]) -> bool:
        """Trả True nếu `text` chứa BẤT KỲ keyword nào (đã lowercase)."""
        return any(keyword in text for keyword in keywords)

    # ---- create_checkout (R1.5, R1.6, R1.7, R1.8, R1.9) -----------------

    async def create_checkout(self, session: SessionBundle) -> CheckoutSession:
        """Tạo checkout session với billing NL/EUR — Requirement 1.5-1.9.

        Flow:
            1. Restore cookies từ `session.cookies` vào AsyncSession cookie jar
               (giống `revalidate`) để request kế thừa trạng thái session.
            2. `POST chatgpt.com/backend-api/payments/checkout` với body
               `{"billing_details": {"country": "NL", "currency": "EUR"},
               "plan_name": "chatgptplusplan"}` và header
               `Authorization: Bearer <access_token>` +
               `Content-Type: application/json` (R1.5).
            3. Nếu HTTP status không 2xx → raise `IdealFlowError(step="checkout",
               error_code="checkout_http_error")` (Fail_Fast_Policy theo R14.1).
            4. Parse response bằng `parse_checkout_session()` — hàm này raise
               `RequiredFieldMissingError` nếu thiếu 1 trong 3 field bắt buộc
               `checkout_session_id`/`publishable_key`/`processor_entity` (R1.7).
            5. Nếu `processor_entity != "openai_ie"` → log warning, KHÔNG raise
               (R1.8). Tiếp tục flow với giá trị thực tế nhận được để bước
               `approve` (R4) truyền lại nhất quán.

        Args:
            session: `SessionBundle` từ `login()` HOẶC từ cache đã revalidate.
                `access_token` non-empty, `cookies` snapshot còn hiệu lực.

        Returns:
            `CheckoutSession` đã parse — non-None. `processor_entity` có thể
            khác giá trị mặc định `openai_ie` (log warning nhưng không raise).

        Raises:
            IdealFlowError: `step="checkout"`, `error_code="checkout_http_error"`
                khi transport-level failure (timeout/network/JSON invalid) hoặc
                HTTP status không 2xx (R14.1).
            RequiredFieldMissingError: Response thiếu 1 trong 3 field bắt buộc
                của `CheckoutSession` (R1.7) — propagate từ
                `parse_checkout_session()`. Caller (`IdealFlowHandler`) bắt
                `IdealFlowError` boundary duy nhất (RequiredFieldMissingError
                cũng là subclass của `IdealFlowError`).

        Sensitive_Data_Redaction (R1.9): mọi log entry gọi `redact_dict()`
        cho payload dict — `access_token`/`cookies` được mask tự động theo
        `_SENSITIVE_FIELD_NAMES`.
        """
        # (1) Restore cookies vào jar với domain=".chatgpt.com" explicit.
        # Idempotent nếu `revalidate()` đã restore trước đó — set lại value
        # không tạo entry trùng, jar dùng key (domain, path, name).
        _restore_cookies_scoped(self._client, session.cookies)

        url = f"{_CHATGPT_BASE_URL}{_ENDPOINT_CHECKOUT}"
        # Body theo HAR `web_record_20260705-155711_manual` event 001 (POST
        # /backend-api/payments/checkout, response HTTP 200 với setup_intent
        # attached đúng flow). HAR body cụ thể:
        #   {"entry_point":"all_plans_pricing_modal",
        #    "plan_name":"chatgptplusplan",
        #    "billing_details":{"country":"NL","currency":"EUR"},
        #    "promo_campaign":{"promo_campaign_id":"plus-1-month-free",
        #                      "is_coupon_from_query_param":false}}
        #
        # KHÔNG có `checkout_ui_mode` — thêm field này khiến ChatGPT tạo
        # session `requires_manual_approval=true` nhưng flow custom-checkout
        # bị lock ở `submission_attempt.state='requires_approval'` không
        # bao giờ chuyển sang `processing` (verified 15 lần poll = 44s).
        #
        # `promo_campaign` là kích hoạt ưu đãi "plus-1-month-free" — CHATGPT
        # tự attach setup_intent với `expected_amount=0` (1 tháng free)
        # thay vì charge ngay. Đây là điều kiện tiên quyết để iDEAL flow
        # transition state đúng.
        request_body: dict[str, Any] = {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": _PLAN_NAME_CHATGPT_PLUS,
            "billing_details": {
                "country": _BILLING_COUNTRY_NL,
                "currency": _BILLING_CURRENCY_EUR,
            },
            "promo_campaign": {
                "promo_campaign_id": "plus-1-month-free",
                "is_coupon_from_query_param": False,
            },
        }
        # Headers khớp browser thật (Chrome desktop, locale NL). `x-openai-target-*`
        # là anti-bot signal — ChatGPT edge check header này khớp path/route
        # để lọc traffic bot. `OAI-Language` = ngôn ngữ UI, đồng bộ với
        # billing NL (nl-NL).
        headers = {
            "Authorization": f"Bearer {session.access_token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
            "Origin": _CHATGPT_BASE_URL,
            "Referer": f"{_CHATGPT_BASE_URL}/",
            "OAI-Language": "nl-NL",
            "x-openai-target-path": _ENDPOINT_CHECKOUT,
            "x-openai-target-route": _ENDPOINT_CHECKOUT,
        }

        # Bỏ log `begin` verbose — kết quả sẽ được log 1 dòng gộp sau khi
        # có response (ok / non-2xx / transport error).

        # (2) Gọi endpoint checkout.
        try:
            response = await self._client.post(url, json=request_body, headers=headers)
        except (
            http.TimeoutException,
            http.NetworkError,
            http.TransportError,
        ) as exc:
            self._logger.warning(
                "chatgpt_checkout transport error: email=%s exc=%s",
                session.email,
                exc,
            )
            raise IdealFlowError(
                error_code="checkout_http_error",
                step="checkout",
                message=(
                    f"Checkout transport error: {exc.__class__.__name__}: {exc}"
                ),
            ) from exc

        # (3) Non-2xx → Fail_Fast_Policy (R14.1). Log kèm body snippet đã cắt
        # ngắn để hỗ trợ debug, KHÔNG log full body.
        if not (200 <= response.status_code < 300):
            body_snippet = self._extract_body_snippet(response)
            self._logger.warning(
                "chatgpt_checkout non-2xx: email=%s status=%s body_snippet=%s",
                session.email,
                response.status_code,
                body_snippet,
            )
            raise IdealFlowError(
                error_code="checkout_http_error",
                step="checkout",
                message=(
                    f"Checkout HTTP {response.status_code}: {body_snippet}"
                ),
            )

        # Parse JSON body — non-JSON coi như checkout_http_error (Fail_Fast).
        try:
            response_payload = response.json()
        except ValueError as exc:
            self._logger.warning(
                "chatgpt_checkout invalid JSON: email=%s exc=%s",
                session.email,
                exc,
            )
            raise IdealFlowError(
                error_code="checkout_http_error",
                step="checkout",
                message=f"Checkout response is not JSON: {exc}",
            ) from exc

        if not isinstance(response_payload, dict):
            self._logger.warning(
                "chatgpt_checkout payload is not a dict: email=%s type=%s",
                session.email,
                type(response_payload).__name__,
            )
            raise IdealFlowError(
                error_code="checkout_http_error",
                step="checkout",
                message=(
                    f"Checkout response is not a dict: "
                    f"type={type(response_payload).__name__}"
                ),
            )

        # (4) Parse thành CheckoutSession — raise RequiredFieldMissingError
        # nếu thiếu field bắt buộc (R1.7) — propagate lên caller.
        checkout_session = parse_checkout_session(response_payload)

        # (5) R1.8: processor_entity khác `openai_ie` → warning, KHÔNG raise.
        if checkout_session.processor_entity != _EXPECTED_PROCESSOR_ENTITY:
            self._logger.warning(
                "chatgpt_checkout: processor_entity differs from expected — email=%s "
                "expected=%s actual=%s (NOT fatal, continuing flow per R1.8)",
                session.email,
                _EXPECTED_PROCESSOR_ENTITY,
                checkout_session.processor_entity,
            )

        # 1 dòng ok — checkout_session_id là identity của flow, `status`
        # thường là "open" (Stripe checkout mới tạo). Bỏ publishable_key
        # (public, dài, không giúp debug) và payment_status (redundant).
        self._logger.info(
            "chatgpt_checkout ok cs_id=%s status=%s",
            checkout_session.checkout_session_id,
            checkout_session.status,
        )
        return checkout_session

    async def snapshot_billing(
        self,
        session: SessionBundle,
        billing: BillingAddress,
    ) -> None:
        """POST `/backend-api/payments/checkout/snapshot` với billing_address
        đầy đủ trước khi gọi approve.

        HAR `web_record_20260705-155711_manual` events 002-004 chứng minh
        ChatGPT UI gọi endpoint này 3 lần progressive (mỗi field billing
        user gõ). Backend HTTP flow chỉ cần gọi 1 lần với address complete.

        Response HTTP 204 No Content — không có body. Fail_Fast_Policy:
        bất kỳ non-2xx nào cũng raise IdealFlowError để rõ lý do
        approve fail sau đó.

        Body format (HAR):
            {"snapshot":{
                "billing_address":{
                    "name":"cip hdfj",
                    "address":{
                        "line1":"hanoi","line2":"hanoi","city":"hanoi",
                        "country":"VN","postal_code":"100000",
                        "state":"Gia Lai Province"
                    }
                }
            }}

        Args:
            session: SessionBundle với access_token + cookies.
            billing: BillingAddress đã generate theo locale NL. Field mapping:
                - name → snapshot.billing_address.name
                - address → snapshot.billing_address.address (dict as-is)

        Raises:
            http.HTTPError: transport error.
            (chưa raise domain error — snapshot best-effort, không critical
            như approve; nếu 4xx/5xx thì log warning và tiếp tục để không
            block flow).
        """
        # Restore cookies với domain scoped (fix HTTP 431 — xem `_restore_cookies_scoped`).
        _restore_cookies_scoped(self._client, session.cookies)

        url = f"{_CHATGPT_BASE_URL}{_ENDPOINT_SNAPSHOT}"
        # billing.address là dict với 6 field theo `BillingAddress` model.
        # HAR body wrap `snapshot.billing_address` — 2 lớp lồng.
        request_body: dict[str, Any] = {
            "snapshot": {
                "billing_address": {
                    "name": billing.name,
                    "address": dict(billing.address),
                }
            }
        }
        headers = {
            "Authorization": f"Bearer {session.access_token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
            "Origin": _CHATGPT_BASE_URL,
            "Referer": f"{_CHATGPT_BASE_URL}/",
            "OAI-Language": "nl-NL",
            "x-openai-target-path": _ENDPOINT_SNAPSHOT,
            "x-openai-target-route": _ENDPOINT_SNAPSHOT,
        }

        # Bỏ log `begin` — kết quả sẽ được log 1 dòng gộp sau response.

        try:
            response = await self._client.post(
                url, json=request_body, headers=headers
            )
        except http.HTTPError as exc:
            # Snapshot best-effort — log warning, KHÔNG raise. Approve
            # sau đó có thể vẫn work nếu ChatGPT có snapshot cũ trong DB.
            self._logger.warning(
                "chatgpt_snapshot transport error (ignored): %s",
                exc,
            )
            return

        if not (200 <= response.status_code < 300):
            # 4xx/5xx: log nhưng không fail flow.
            self._logger.warning(
                "chatgpt_snapshot non-2xx: status=%s body=%s",
                response.status_code,
                response.text[:200],
            )
            return

        self._logger.info(
            "chatgpt_snapshot ok status=%d city=%s",
            response.status_code,
            billing.address.get("city", ""),
        )

    # ---- approve (R4.1, R4.2) -------------------------------------------

    async def approve(
        self,
        checkout_session_id: str,
        processor_entity: str,
        session: SessionBundle,
    ) -> None:
        """Approve checkout — gọi ĐÚNG 1 LẦN, KHÔNG retry (R4.1, R4.2).

        Flow (Requirement 4.1, 4.2 — cấm retry để tránh approve trùng giao dịch):
            1. Restore cookies từ `session.cookies` vào AsyncSession cookie jar.
            2. `POST chatgpt.com/backend-api/payments/checkout/approve` với body
               `{"checkout_session_id": ..., "processor_entity": ...}` và
               header `Authorization: Bearer <access_token>`.
            3. Response check:
                - HTTP 2xx (200/201/204/...) + payload dict có
                  `{"result": "approved"}` → return None (thành công).
                - Mọi trường hợp khác (transport error, HTTP 4xx/5xx, HTTP 2xx
                  nhưng thiếu `result=approved`, payload không phải JSON/dict)
                  → raise `ApproveFailedError(http_status, detail)` — KHÔNG
                  bao giờ gọi lại `approve` cho cùng checkout (caller cấm
                  retry theo R4.2).

        Args:
            checkout_session_id: Từ `CheckoutSession.checkout_session_id`
                (Requirement 1.6). Non-empty.
            processor_entity: Từ `CheckoutSession.processor_entity` — truyền
                nguyên vẹn kể cả khi khác `openai_ie` (R1.8).
            session: `SessionBundle` để lấy `access_token` cho header
                Authorization và cookies restore.

        Raises:
            ApproveFailedError: Approve thất bại — bất kỳ lý do gì (R4.2).
                `http_status` là `None` khi transport-level failure (không
                nhận được response).

        Note (R4.10): Log realtime kết quả approve — module này chỉ log 1
        dòng ở result cuối; log các bước refresh/redirect sau approve sẽ do
        module tương ứng phụ trách (không thuộc scope method này).
        """
        # (1) Restore cookies — approve có thể được gọi trên client đã
        # dùng cho request Stripe/pay.ideal.nl khác domain, cần đảm bảo
        # cookie chatgpt.com còn nguyên. `_restore_cookies_scoped` set
        # ``domain=".chatgpt.com"`` explicit → jar không match nhầm khi
        # gửi request Stripe (fix HTTP 431).
        _restore_cookies_scoped(self._client, session.cookies)

        url = f"{_CHATGPT_BASE_URL}{_ENDPOINT_APPROVE}"
        request_body: dict[str, Any] = {
            "checkout_session_id": checkout_session_id,
            "processor_entity": processor_entity,
        }
        # Referer khớp trang custom checkout: `/checkout/<processor_entity>/<session_id>`.
        # Anti-bot heuristic của ChatGPT edge check request từ trang custom
        # checkout (không phải từ pricing modal).
        checkout_referer = (
            f"{_CHATGPT_BASE_URL}/checkout/{processor_entity}/{checkout_session_id}"
        )
        headers = {
            "Authorization": f"Bearer {session.access_token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
            "Origin": _CHATGPT_BASE_URL,
            "Referer": checkout_referer,
            "OAI-Language": "nl-NL",
            "x-openai-target-path": _ENDPOINT_APPROVE,
            "x-openai-target-route": _ENDPOINT_APPROVE,
        }

        # Bỏ log `begin` — kết quả log 1 dòng ok/fail sau response.

        # (2) Gọi ĐÚNG 1 LẦN — KHÔNG retry (R4.2 cấm retry để tránh approve
        # trùng lặp). Transport error → ApproveFailedError với
        # http_status=None (không có response).
        try:
            response = await self._client.post(url, json=request_body, headers=headers)
        except (
            http.TimeoutException,
            http.NetworkError,
            http.TransportError,
        ) as exc:
            detail = f"transport_error: {exc.__class__.__name__}: {exc}"
            self._logger.warning(
                "chatgpt_approve transport error: email=%s "
                "checkout_session_id=%s exc=%s",
                session.email,
                checkout_session_id,
                exc,
            )
            raise ApproveFailedError(http_status=None, detail=detail) from exc

        status = response.status_code

        # Parse JSON payload nếu có — kể cả 204 no-content vẫn thử decode
        # (payload rỗng → ValueError → approve_payload None → fail nhánh
        # thiếu `result=approved`).
        try:
            approve_payload: Any = response.json()
        except ValueError:
            approve_payload = None

        # (3a) HTTP 4xx/5xx → ApproveFailedError (R4.2). Redact payload dict
        # phòng trường hợp Stripe/ChatGPT nhét access_token/cookie vào error
        # payload (defensive).
        if not (200 <= response.status_code < 300):
            detail = f"http_{status}"
            if isinstance(approve_payload, dict) and approve_payload:
                detail = f"{detail}: {redact_dict(approve_payload)}"
            elif isinstance(approve_payload, list):
                detail = f"{detail}: {approve_payload!r}"
            self._logger.warning(
                "chatgpt_approve non-2xx: email=%s status=%s "
                "checkout_session_id=%s",
                session.email,
                status,
                checkout_session_id,
            )
            raise ApproveFailedError(http_status=status, detail=detail)

        # (3b) HTTP 2xx nhưng payload không phải dict hoặc thiếu
        # `result=approved` → ApproveFailedError. Note R4.2: chấp nhận
        # 201/204 nếu vẫn parse ra `result=approved` — logic này bao trùm.
        #
        # Dùng `ApproveFailedError.from_payload` để phân loại chi tiết:
        # - `result="blocked"` → error_code="approve_blocked" (anti-fraud,
        #   không phải bug tool — user cần đổi proxy/account).
        # - `result="needs_review"` → error_code="approve_needs_review".
        # - Còn lại → error_code="approve_failed" (generic).
        if (
            not isinstance(approve_payload, dict)
            or approve_payload.get("result") != _APPROVE_RESULT_APPROVED
        ):
            payload_type = (
                type(approve_payload).__name__ if approve_payload is not None else "None"
            )
            detail = f"http_{status}_missing_result_approved (payload_type={payload_type})"
            if isinstance(approve_payload, dict):
                detail = f"{detail}: {redact_dict(approve_payload)}"
            result_val = (
                approve_payload.get("result")
                if isinstance(approve_payload, dict)
                else None
            )
            self._logger.warning(
                "chatgpt_approve 2xx but missing result=approved: email=%s "
                "status=%s checkout_session_id=%s payload_type=%s result=%s",
                session.email,
                status,
                checkout_session_id,
                payload_type,
                result_val,
            )
            raise ApproveFailedError.from_payload(
                http_status=status,
                payload=approve_payload if isinstance(approve_payload, dict) else None,
                detail=detail,
            )

        # (3c) Success — 1 dòng gọn (R4.10). cs_id ngắn hơn checkout_session_id
        # đầy đủ nhưng vẫn đủ để cross-reference với log Stripe.
        self._logger.info("chatgpt_approve ok status=%d", status)

    # ---- check_plan_status (Task 5 — verify Plus subscription) ----------

    async def check_plan_status(self, session: SessionBundle) -> dict[str, Any]:
        """Kiểm tra tài khoản đã lên Plus chưa — pipeline 2 bước.

        Port pattern từ `gpt_signup_hybrid.web.manager.UpiJobManager.check_plan`:

            Bước 1 — fetch fresh session (`GET /api/auth/session` với cookies +
                Bearer accessToken cache). Endpoint này refresh accessToken
                nếu JWT cũ đã expire nhưng cookies còn hiệu lực. Nếu 200 →
                lấy `accessToken` mới cho bước 2 + parse `planType` dự phòng.

            Bước 2 — fetch entitlement live (`GET /backend-api/accounts/check/v4`
                với `Authorization: Bearer <fresh_token>`). Endpoint này đọc
                subscription state trực tiếp từ backend, KHÔNG lệ thuộc JWT
                cache lag → reflect Plus ngay sau khi user thanh toán xong.
                Nếu 200 + có `entitlement.subscription_plan` → dùng làm plan
                chính (ưu tiên hơn bước 1).

            Fallback: nếu bước 2 fail (403/network/parse) → dùng
                `planType` từ bước 1. Nếu cả 2 bước đều fail → trả
                `{plan: 'unknown', error: <reason>}`.

        Chiến lược này KHỚP UPI: dùng cookie + token đã cache → lấy lại
        session → check entitlement — không cần login lại.

        KHÔNG raise. Trả dict với key bắt buộc `plan`, các key optional:
            - `plan`: "plus" | "free" | "unknown"
            - `raw_plan`: chuỗi source=value dùng cho tooltip
            - `expires_at`: ISO timestamp subscription hết hạn (nếu backend
              entitlement trả về `expires_at`).
            - `email`: user.email từ session
            - `error`: lý do khi `plan="unknown"`
        """
        # Restore cookies + Bearer — scope ".chatgpt.com" để tránh jar
        # pollution khi client được share với các request Stripe khác domain.
        _restore_cookies_scoped(self._client, session.cookies)

        # ---- Bước 1: fetch fresh session ---------------------------------
        session_url = f"{_CHATGPT_BASE_URL}{_ENDPOINT_SESSION}"
        session_headers = {
            "Authorization": f"Bearer {session.access_token}",
            "Accept": "application/json",
            "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
            "Referer": f"{_CHATGPT_BASE_URL}/",
        }

        try:
            session_resp = await self._client.get(session_url, headers=session_headers)
        except (
            http.TimeoutException,
            http.NetworkError,
            http.TransportError,
        ) as exc:
            self._logger.warning(
                "chatgpt_check_plan session transport error: email=%s exc=%s",
                session.email,
                exc,
            )
            return {"plan": "unknown", "error": f"transport: {exc.__class__.__name__}"}

        if not (200 <= session_resp.status_code < 300):
            self._logger.warning(
                "chatgpt_check_plan session non-2xx: email=%s status=%s",
                session.email,
                session_resp.status_code,
            )
            return {"plan": "unknown", "error": f"session_http_{session_resp.status_code}"}

        try:
            session_payload = session_resp.json()
        except ValueError:
            return {"plan": "unknown", "error": "session_invalid_json"}

        if not isinstance(session_payload, dict):
            return {"plan": "unknown", "error": "session_not_dict"}

        result: dict[str, Any] = {"plan": "unknown"}

        # Email + accessToken tươi từ session response.
        user = session_payload.get("user")
        if isinstance(user, dict):
            email = user.get("email")
            if isinstance(email, str):
                result["email"] = email

        fresh_token = session_payload.get("accessToken")
        if not isinstance(fresh_token, str) or not fresh_token:
            # Session response không có accessToken → dùng token cũ từ cache
            # (edge case, vẫn thử bước 2). Nếu cả 2 đều rỗng thì bước 2 tự
            # skip xuống fallback.
            fresh_token = session.access_token

        # ---- Bước 2: fetch entitlement live (LIVE plan) ------------------
        entitlement = await self._fetch_entitlement(
            access_token=fresh_token,
            referer_email=session.email,
        )

        if entitlement.get("ok") is True:
            plan_label = entitlement.get("plan")
            has_active = bool(entitlement.get("has_active_subscription"))
            # Định nghĩa "plus": subscription active + label chứa "plus".
            # Nới hơn UPI (UPI strict `label == "plus"`) vì iDEAL market
            # cũng có subscription plans "chatgpt-plus-2y", "chatgpt-plus-nl"
            # → bao gồm bằng `startswith("plus")`.
            if isinstance(plan_label, str) and plan_label:
                if has_active and "plus" in plan_label.lower():
                    result["plan"] = "plus"
                elif "free" in plan_label.lower() or not has_active:
                    result["plan"] = "free"
                else:
                    # Plan lạ (VD "team"/"pro"/"enterprise") — coi là free
                    # cho tool này (chỉ quan tâm Plus).
                    result["plan"] = "free"
                result["raw_plan"] = f"entitlement.subscription_plan={plan_label}"
            expires_at = entitlement.get("expires_at")
            if expires_at:
                result["expires_at"] = expires_at

            self._logger.info(
                "chatgpt_check_plan entitlement ok: email=%s plan=%s raw=%s",
                session.email,
                result["plan"],
                result.get("raw_plan", "n/a"),
            )
            return result

        # ---- Fallback: parse plan từ session_payload ---------------------
        # Entitlement fail (Cloudflare 403 / expired token / network). Rơi
        # về heuristic cũ trên session JSON. Lag hơn nhưng đủ để tránh
        # "unknown" khi user chỉ cần biết chưa lên Plus.
        classified, raw_plan_str = _classify_plan_from_session(session_payload)
        result["plan"] = classified
        if raw_plan_str:
            result["raw_plan"] = raw_plan_str

        # Ghi lý do entitlement fail vào error (không che bug bước 2).
        entitlement_error = entitlement.get("error")
        if entitlement_error and classified == "unknown":
            result["error"] = f"entitlement: {entitlement_error}"

        self._logger.info(
            "chatgpt_check_plan fallback session: email=%s plan=%s raw=%s ent_err=%s",
            session.email,
            classified,
            raw_plan_str or "n/a",
            entitlement_error or "n/a",
        )
        return result

    async def _fetch_entitlement(
        self, *, access_token: str, referer_email: str
    ) -> dict[str, Any]:
        """GET `/backend-api/accounts/check/v4-2023-04-27` → entitlement plan.

        Port từ `gpt_signup_hybrid.session_phase.fetch_account_entitlement`.
        Header recipe khớp UPI đã verify vượt Cloudflare 403 (Bearer-only
        không đủ, phải kèm `Origin` + `x-openai-target-*` + UA persona đầy
        đủ đã set qua `_DEFAULT_HTTP_HEADERS` ở tầng flow).

        KHÔNG raise. Trả dict `{ok, plan, is_plus, has_active_subscription,
        expires_at, error}`:
            - `ok`: True nếu HTTP 200 + parse thành công.
            - `plan`: label rút gọn từ `subscription_plan` (bỏ prefix
              "chatgpt" + suffix "plan") — VD "plus"/"free"/"team".
            - `has_active_subscription`: bool từ backend.
            - `expires_at`: ISO datetime từ backend (nếu có).
            - `error`: lý do khi `ok=False` (dùng cho fallback logic).

        Args:
            access_token: accessToken (JWT) đã refresh qua bước 1.
            referer_email: chỉ dùng cho log — không ảnh hưởng logic.
        """
        if not isinstance(access_token, str) or not access_token.strip():
            return {"ok": False, "error": "access_token_empty"}

        url = f"{_CHATGPT_BASE_URL}{_ENDPOINT_ENTITLEMENT}"
        target = _ENDPOINT_ENTITLEMENT
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "*/*",
            "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
            "Origin": _CHATGPT_BASE_URL,
            "Referer": f"{_CHATGPT_BASE_URL}/",
            "OAI-Language": "nl-NL",
            "x-openai-target-path": target,
            "x-openai-target-route": target,
        }

        try:
            response = await self._client.get(url, headers=headers)
        except (
            http.TimeoutException,
            http.NetworkError,
            http.TransportError,
        ) as exc:
            self._logger.warning(
                "entitlement transport error: email=%s exc=%s",
                referer_email,
                exc,
            )
            return {"ok": False, "error": f"transport: {exc.__class__.__name__}"}

        # Body có thể echo lại token → CHỈ log status, KHÔNG log body.
        if not (200 <= response.status_code < 300):
            self._logger.warning(
                "entitlement non-2xx: email=%s status=%s",
                referer_email,
                response.status_code,
            )
            return {"ok": False, "error": f"http_{response.status_code}"}

        try:
            payload = response.json()
        except ValueError:
            return {"ok": False, "error": "invalid_json"}

        if not isinstance(payload, dict):
            return {"ok": False, "error": "payload_not_dict"}

        parsed = _parse_entitlement_plan(payload)
        return {"ok": True, **parsed}

    # ---- Internal helper: body snippet cho log HTTP error ---------------

    @staticmethod
    def _extract_body_snippet(response: http.Response) -> str:
        """Trích đoạn đầu response body (cắt ngắn) — dùng cho log HTTP error.

        KHÔNG raise nếu decode fail — trả empty string. Cắt tại
        `_HTTP_ERROR_BODY_SNIPPET_MAX_CHARS` ký tự đầu để tránh spam log/
        leak dữ liệu nhạy cảm.
        """
        try:
            text = response.text
        except (UnicodeDecodeError, http.DecodingError):
            return ""
        if not text:
            return ""
        if len(text) <= _HTTP_ERROR_BODY_SNIPPET_MAX_CHARS:
            return text
        return text[:_HTTP_ERROR_BODY_SNIPPET_MAX_CHARS] + "...(truncated)"


__all__ = [
    # Module-level helper.
    "parse_account_line",
    # Class.
    "ChatgptClient",
    # Reason code constants (dùng cho log / test / caller phân nhánh).
    "LOGIN_ERROR_INVALID_CREDENTIAL",
    "LOGIN_ERROR_MFA_REQUIRED",
    "LOGIN_ERROR_ACCOUNT_LOCKED",
    "LOGIN_ERROR_NETWORK",
]
