"""Smoke test cho bug fix `_extract_address_rejected_field` +
nhánh 4xx của `StripeClient.confirm` (payments/ideal/stripe_client.py).

Verify các contract quan trọng sau khi siết logic phân loại lỗi 4xx:

    TC-01  Address field trong whitelist (`error.param="address_line1"`,
           `error.type=invalid_request_error`) → `AddressValidationRejectedError`
           với `rejected_field="address_line1"`.

    TC-02  Address keyword trong message, param rỗng
           (`error.message="address_postal_code is invalid"`) →
           `AddressValidationRejectedError(rejected_field="address_postal_code")`.

    TC-03  **Bug case gốc:** `error.param="payment_method"`,
           `error.type=invalid_request_error`, message không chứa keyword
           address → PHẢI raise `ConfirmInvalidResponseError` (KHÔNG còn
           label sai "địa chỉ") và message exception phải chứa "payment_method"
           để log giữ nguyên nhân thật.

    TC-04  Case 3 token JS-runtime (`error.code=js_checksum_invalid`,
           không có `error.param`, message không match address keyword) →
           `ConfirmInvalidResponseError`, message chứa `code` từ Stripe.

    TC-05  Payload không có `error` object → `ConfirmInvalidResponseError`
           chung (nhánh 4xx fallback).

Chạy trực tiếp: `python3 backend/test/check_stripe_confirm_error_mapping.py`
(cần .venv của backend đã cài dependencies).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

# Cho phép import `app.*` khi chạy script trực tiếp.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_DIR))

import httpx  # noqa: E402

from app.payments.ideal.errors import (  # noqa: E402
    AddressValidationRejectedError,
    ConfirmInvalidResponseError,
)
from app.payments.ideal.models import BillingAddress  # noqa: E402
from app.payments.ideal.stripe_client import StripeClient  # noqa: E402


class _FakeSettings:
    async def get(self, key: str) -> Any | None:  # noqa: ARG002
        return None


def _make_billing() -> BillingAddress:
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


def _run_confirm_with_response(response_status: int, response_json: Any) -> Exception:
    """Gọi `confirm()` với response giả lập, trả về exception raise ra."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(response_status, json=response_json)

    async def _run() -> None:
        transport = httpx.MockTransport(_handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            client = StripeClient(
                http_client=http_client,
                settings=_FakeSettings(),  # type: ignore[arg-type]
                logger=logging.getLogger("test.stripe.confirm_mapping"),
            )
            await client.confirm(
                checkout_session_id="cs_test",
                publishable_key="pk_test",
                elements_session_id="es_test",
                billing=_make_billing(),
            )

    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 — smoke: catch để inspect
        return exc
    raise AssertionError("confirm() không raise — smoke test không có ý nghĩa")


def _print_pass(tc: str, desc: str, detail: str) -> None:
    print(f"[PASS] {tc} — {desc} :: {detail}", flush=True)


def _print_fail(tc: str, desc: str, detail: str) -> None:
    print(f"[FAIL] {tc} — {desc} :: {detail}", flush=True)


def run_tc01() -> bool:
    desc = "Address field whitelist → AddressValidationRejectedError"
    exc = _run_confirm_with_response(
        400,
        {
            "error": {
                "type": "invalid_request_error",
                "param": "address_line1",
                "message": "address_line1 is invalid",
            }
        },
    )
    if not isinstance(exc, AddressValidationRejectedError):
        _print_fail("TC-01", desc, f"kỳ vọng AddressValidationRejectedError, nhận {type(exc).__name__}: {exc}")
        return False
    if exc.rejected_field != "address_line1":
        _print_fail("TC-01", desc, f"rejected_field kỳ vọng 'address_line1', nhận {exc.rejected_field!r}")
        return False
    _print_pass("TC-01", desc, f"rejected_field={exc.rejected_field!r}")
    return True


def run_tc02() -> bool:
    desc = "Keyword-in-message → AddressValidationRejectedError"
    exc = _run_confirm_with_response(
        400,
        {
            "error": {
                "type": "invalid_request_error",
                "message": "address_postal_code is invalid for country NL",
            }
        },
    )
    if not isinstance(exc, AddressValidationRejectedError):
        _print_fail("TC-02", desc, f"kỳ vọng AddressValidationRejectedError, nhận {type(exc).__name__}: {exc}")
        return False
    if exc.rejected_field != "address_postal_code":
        _print_fail("TC-02", desc, f"rejected_field kỳ vọng 'address_postal_code', nhận {exc.rejected_field!r}")
        return False
    _print_pass("TC-02", desc, f"rejected_field={exc.rejected_field!r}")
    return True


def run_tc03() -> bool:
    desc = "Bug case: param='payment_method' → ConfirmInvalidResponseError (KHÔNG label address)"
    exc = _run_confirm_with_response(
        400,
        {
            "error": {
                "type": "invalid_request_error",
                "param": "payment_method",
                "message": "The payment_method is invalid.",
            }
        },
    )
    if isinstance(exc, AddressValidationRejectedError):
        _print_fail("TC-03", desc, f"REGRESSION: vẫn label thành address error (rejected_field={exc.rejected_field!r})")
        return False
    if not isinstance(exc, ConfirmInvalidResponseError):
        _print_fail("TC-03", desc, f"kỳ vọng ConfirmInvalidResponseError, nhận {type(exc).__name__}: {exc}")
        return False
    if exc.http_status != 400:
        _print_fail("TC-03", desc, f"http_status kỳ vọng 400, nhận {exc.http_status}")
        return False
    msg = str(exc)
    if "payment_method" not in msg:
        _print_fail("TC-03", desc, f"message KHÔNG chứa 'payment_method' (mất context debug): {msg!r}")
        return False
    _print_pass("TC-03", desc, f"message={msg[:120]!r}")
    return True


def run_tc04() -> bool:
    desc = "JS-runtime token reject (code, no param) → ConfirmInvalidResponseError với code trong message"
    exc = _run_confirm_with_response(
        400,
        {
            "error": {
                "type": "invalid_request_error",
                "code": "js_checksum_invalid",
                "message": "js_checksum_invalid: token failed server-side validation",
            }
        },
    )
    if not isinstance(exc, ConfirmInvalidResponseError):
        _print_fail("TC-04", desc, f"kỳ vọng ConfirmInvalidResponseError, nhận {type(exc).__name__}: {exc}")
        return False
    msg = str(exc)
    if "js_checksum_invalid" not in msg:
        _print_fail("TC-04", desc, f"message KHÔNG chứa 'js_checksum_invalid': {msg!r}")
        return False
    _print_pass("TC-04", desc, f"http_status={exc.http_status}, message={msg[:120]!r}")
    return True


def run_tc05() -> bool:
    desc = "Payload không có `error` object → ConfirmInvalidResponseError fallback"
    exc = _run_confirm_with_response(
        400,
        {"unrelated": "shape"},
    )
    if not isinstance(exc, ConfirmInvalidResponseError):
        _print_fail("TC-05", desc, f"kỳ vọng ConfirmInvalidResponseError, nhận {type(exc).__name__}: {exc}")
        return False
    if exc.http_status != 400:
        _print_fail("TC-05", desc, f"http_status kỳ vọng 400, nhận {exc.http_status}")
        return False
    _print_pass("TC-05", desc, f"http_status={exc.http_status}, message={str(exc)[:120]!r}")
    return True


def main() -> int:
    print("[START] check_stripe_confirm_error_mapping — 5 test case", flush=True)
    results = [
        run_tc01(),
        run_tc02(),
        run_tc03(),
        run_tc04(),
        run_tc05(),
    ]
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"[DONE] {passed}/{total} passed", flush=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
