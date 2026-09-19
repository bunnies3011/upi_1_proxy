"""Unit test cho Fail-fast không retry khi Stripe reject 3 token JS-runtime (Task 35.1).

**Requirement 3.3**: `StripeClient.confirm(...)` tính nội bộ 3 token:
`js_checksum`, `rv_timestamp`, `passive_captcha_token`. Nếu Stripe trả HTTP
4xx với mã lỗi liên quan đến 3 token này (`js_checksum_invalid`,
`rv_timestamp_invalid`, `passive_captcha_token_missing`), Backend_Service
PHẢI Fail_Fast_Policy: raise exception domain iDEAL NGAY LẬP TỨC, KHÔNG
retry — vì retry với cùng giá trị token vừa bị reject không thể giải quyết
lỗi validate (dữ liệu đã sai thì retry cũng sai).

Ràng buộc kỹ thuật (đọc từ code):

- `StripeClient.confirm` gọi HTTP 1 lần duy nhất (R3.5, comment trong
  stripe_client.py line "R3.5 — KHÔNG retry: gọi HTTP 1 lần duy nhất"),
  KHÔNG dùng `_bounded_retry` — nên bất kỳ 4xx nào cũng chỉ tạo đúng 1
  request.
- Khi status ∈ [400, 500) VÀ `error.type == "invalid_request_error"`:
  `_extract_address_rejected_field` coi là address error, raise
  `AddressValidationRejectedError`.
- Khi status 4xx nhưng KHÔNG match `invalid_request_error` và cũng KHÔNG
  match keyword address: raise `ConfirmInvalidResponseError`.

Test dùng `httpx.MockTransport` (chuẩn httpx, đơn giản hơn `respx` cho
scope 1 endpoint) để bắt request `POST api.stripe.com/v1/payment_pages/
{id}/confirm` và trả 400 với 3 mã lỗi JS-runtime tương ứng. Assert:

    1. Đúng 1 exception thuộc hierarchy `IdealFlowError` được raise
       (`AddressValidationRejectedError` HOẶC `ConfirmInvalidResponseError`).
    2. HTTP handler chỉ được gọi đúng 1 lần (không retry).

**Validates: Requirements 3.3**
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

import pytest

from app.payments.ideal.errors import (
    AddressValidationRejectedError,
    ConfirmInvalidResponseError,
    IdealFlowError,
)
from app.payments.ideal.models import BillingAddress
from app.payments.ideal.stripe_client import StripeClient
from tests.support.fake_http import FakeAsyncSession, FakeResponse

# ---------------------------------------------------------------------------
# Fixture data — cố định để boundary lỗi là biến duy nhất trong test.
# ---------------------------------------------------------------------------

_CHECKOUT_SESSION_ID = "cs_test_35_1"
_PUBLISHABLE_KEY = "pk_test_35_1"
_ELEMENTS_SESSION_ID = "es_test_35_1"

#: 3 mã lỗi mà Stripe có thể trả khi validate 3 token JS-runtime fail (R3.3).
#: Test đảm bảo với MỖI mã, `confirm` fail-fast đúng 1 call.
_JS_RUNTIME_ERROR_CODES: tuple[str, ...] = (
    "js_checksum_invalid",
    "rv_timestamp_invalid",
    "passive_captcha_token_missing",
)


class _FakeSettings:
    """Duck-typed replacement cho `SettingsRepository` — trả `None` cho mọi key.

    `confirm()` chỉ đọc `ideal.stripe_request_timeout_seconds` (R3.5 —
    KHÔNG retry, nên `_read_max_retry_attempts`/`_read_retry_backoff_seconds`
    không được gọi). Trả `None` → `_read_request_timeout_seconds` fallback
    về default 30s — không ảnh hưởng test vì handler trả response ngay lập
    tức trước khi timeout chạm.
    """

    async def get(self, key: str) -> Any | None:  # noqa: ARG002
        return None


def _make_billing() -> BillingAddress:
    """`BillingAddress` NL hợp lệ shape tối thiểu để `_build_confirm_request`
    không raise `ValueError` ở khối validate input.
    """
    return BillingAddress(
        name="Jan Test",
        email="jan@example.com",
        address={
            "country": "NL",
            "line1": "Teststraat 1",
            "line2": "",
            "city": "Amsterdam",
            "postal_code": "1000 AA",
            "state": "",
        },
    )


def _build_stripe_error_body(error_code: str) -> dict[str, Any]:
    """Body 400 mô phỏng Stripe reject 1 trong 3 token JS-runtime (R3.3).

    Shape sát HAR Stripe: `error.type=invalid_request_error`,
    `error.code=<js_runtime_code>`, `error.message="<code> failed validation"`.
    KHÔNG kèm `error.param` (Stripe không set param cho 3 token này — chúng
    ở outer body chứ không phải field address).
    """
    return {
        "error": {
            "type": "invalid_request_error",
            "code": error_code,
            "message": f"{error_code}: token failed server-side validation",
        }
    }


# ---------------------------------------------------------------------------
# Test — parametric theo 3 mã lỗi JS-runtime.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("error_code", _JS_RUNTIME_ERROR_CODES)
def test_confirm_fail_fast_no_retry_when_stripe_rejects_js_runtime_token(
    error_code: str,
) -> None:
    """Stripe reject bằng mã lỗi 3 token JS-runtime → Fail_Fast đúng 1 call.

    Contract R3.3 + R3.5:
        - `confirm()` gọi HTTP 1 lần duy nhất, KHÔNG retry.
        - Response 400 với `error.type=invalid_request_error` → raise
          exception thuộc hierarchy `IdealFlowError`
          (`AddressValidationRejectedError` do logic mapping hiện tại,
          nhưng test chấp nhận cả `ConfirmInvalidResponseError` nếu Stripe
          mapping thay đổi tương lai).
        - Handler HTTP được gọi ĐÚNG 1 LẦN (`call_count == 1`) — bằng
          chứng KHÔNG retry.

    **Validates: Requirements 3.3**
    """
    # Track số lần MockTransport nhận request POST tới endpoint confirm.
    # Dùng list-of-URL để có thể debug (URL cụ thể của call, không chỉ
    # count) — dễ hơn khi test fail.
    captured_calls: list[str] = []

    def _handler(call):  # noqa: ANN001
        # Chỉ đếm request tới path confirm (không tính request khác nếu có).
        # Path template: /v1/payment_pages/{checkout_session_id}/confirm
        if urlparse(call.url).path.endswith("/confirm"):
            captured_calls.append(call.url)
        return FakeResponse(
            400,
            json_body=_build_stripe_error_body(error_code),
        )

    async def _run_confirm() -> None:
        fake_session = FakeAsyncSession(default_handler=_handler)
        client = StripeClient(
            http_client=fake_session,
            settings=_FakeSettings(),  # type: ignore[arg-type]
            logger=logging.getLogger(
                f"test.stripe_confirm.js_runtime.{error_code}"
            ),
        )
        await client.confirm(
            checkout_session_id=_CHECKOUT_SESSION_ID,
            publishable_key=_PUBLISHABLE_KEY,
            elements_session_id=_ELEMENTS_SESSION_ID,
            billing=_make_billing(),
        )

    # Phải raise 1 exception thuộc hierarchy IdealFlowError. Chấp nhận cả 2
    # exception cụ thể (mapping tuỳ theo Stripe schema — R3.3 không chốt
    # cứng exception nào; chỉ chốt "không retry + fail fast").
    with pytest.raises(IdealFlowError) as exc_info:
        asyncio.run(_run_confirm())

    assert isinstance(
        exc_info.value,
        (AddressValidationRejectedError, ConfirmInvalidResponseError),
    ), (
        f"Kỳ vọng exception AddressValidationRejectedError hoặc "
        f"ConfirmInvalidResponseError khi Stripe reject token "
        f"'{error_code}', nhận: {type(exc_info.value).__name__} "
        f"(step={exc_info.value.step!r}, "
        f"error_code={exc_info.value.error_code!r})"
    )

    # Assert đúng 1 lần HTTP call tới endpoint confirm — bằng chứng
    # Fail_Fast_Policy KHÔNG retry (R3.5).
    assert len(captured_calls) == 1, (
        f"Fail_Fast_Policy R3.5 yêu cầu KHÔNG retry request confirm khi "
        f"Stripe reject 3 token JS-runtime. Kỳ vọng ĐÚNG 1 HTTP call tới "
        f"endpoint confirm, thực tế nhận {len(captured_calls)} call(s): "
        f"{captured_calls!r}"
    )

    # Bổ sung: verify URL request khớp path template R3.2.
    assert (
        f"/v1/payment_pages/{_CHECKOUT_SESSION_ID}/confirm"
        in captured_calls[0]
    ), (
        f"Request KHÔNG khớp path template Stripe confirm (R3.2). "
        f"Kỳ vọng chứa '/v1/payment_pages/{_CHECKOUT_SESSION_ID}/confirm', "
        f"nhận: {captured_calls[0]!r}"
    )


def test_confirm_fail_fast_step_is_stripe_confirm() -> None:
    """Cross-cut: exception phải có `step="stripe_confirm"` (log realtime R14.2).

    Test 1 case bất kỳ trong 3 token để verify field `step` — dùng cho log
    realtime định danh CHÍNH XÁC bước flow xảy ra lỗi (R14.2).
    """
    def _handler(call):  # noqa: ANN001
        return FakeResponse(400, json_body=_build_stripe_error_body("js_checksum_invalid"))

    async def _run_confirm() -> None:
        fake_session = FakeAsyncSession(default_handler=_handler)
        client = StripeClient(
            http_client=fake_session,
            settings=_FakeSettings(),  # type: ignore[arg-type]
            logger=logging.getLogger("test.stripe_confirm.step_assertion"),
        )
        await client.confirm(
            checkout_session_id=_CHECKOUT_SESSION_ID,
            publishable_key=_PUBLISHABLE_KEY,
            elements_session_id=_ELEMENTS_SESSION_ID,
            billing=_make_billing(),
        )

    with pytest.raises(IdealFlowError) as exc_info:
        asyncio.run(_run_confirm())

    assert exc_info.value.step == "stripe_confirm", (
        f"IdealFlowError.step phải = 'stripe_confirm' để log realtime "
        f"định danh đúng bước flow (R14.2), nhận: {exc_info.value.step!r}"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
