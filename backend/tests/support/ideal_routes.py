"""Route builders cho FakeAsyncSession — 12-step iDEAL happy path.

Tách constants + registration logic khỏi từng test file để giảm boilerplate
sau khi migrate httpx/respx → curl_cffi/FakeAsyncSession. Mỗi test chỉ
override phần tử nó thực sự quan tâm (VD `test_no_stripe_consumers_lookup`
thêm route bẫy).
"""
from __future__ import annotations

from typing import Optional

from app.core import http_client as http
from tests.support.fake_http import FakeAsyncSession, FakeResponse


def register_ideal_happy_path(
    session: FakeAsyncSession,
    *,
    checkout_session_id: str,
    publishable_key: str,
    elements_session_id: str,
    encoded_tx_url: str,
    sig_value: str,
    redirect_to_url: str,
    ideal_location: str,
    default_issuer_id: str,
    issuer_deeplink: str,
    processor_entity: str = "openai_ie",
    amount: Optional[dict] = None,
    creditor_name: str = "OpenAI",
) -> None:
    """Đăng ký 8 route cho 12-step iDEAL happy-path.

    Các route:
        1. POST chatgpt.com/backend-api/payments/checkout — Step 3 create_checkout
        2. POST api.stripe.com/v1/payment_pages/{cs}/init — Step 4 init
        3. GET api.stripe.com/v1/elements/sessions — Step 5 elements
        4. POST api.stripe.com/v1/payment_pages/{cs}/confirm — Step 7 confirm
        5. POST chatgpt.com/backend-api/payments/checkout/approve — Step 8 approve
        6. GET api.stripe.com/v1/payment_pages/{cs} — Step 9 refresh_poll
        7. GET {redirect_to_url} — Step 10 follow_redirect (302)
        8. POST pay.ideal.nl/api/v1/transactions/{tx}/initiate — Step 11
    """
    if amount is None:
        amount = {"currency": "EUR", "value": "10.99"}

    session.route(
        "POST",
        "https://chatgpt.com/backend-api/payments/checkout",
        FakeResponse(
            200,
            json_body={
                "checkout_session_id": checkout_session_id,
                "publishable_key": publishable_key,
                "processor_entity": processor_entity,
                "client_secret": "secret_test",
                "status": "open",
                "payment_status": "pending",
                "requires_manual_approval": True,
            },
        ),
    )
    session.route(
        "POST",
        f"https://api.stripe.com/v1/payment_pages/{checkout_session_id}/init",
        FakeResponse(
            200,
            json_body={"init_checksum": "init_chk_test", "config_id": "cfg_test"},
        ),
    )
    session.route(
        "GET",
        "https://api.stripe.com/v1/elements/sessions",
        FakeResponse(200, json_body={"session_id": elements_session_id}),
    )
    # Step 6.5 — update_billing (POST /v1/payment_pages/{id}) trước confirm.
    session.route(
        "POST",
        f"https://api.stripe.com/v1/payment_pages/{checkout_session_id}",
        FakeResponse(200, json_body={}),
    )
    session.route(
        "POST",
        f"https://api.stripe.com/v1/payment_pages/{checkout_session_id}/confirm",
        FakeResponse(
            200,
            json_body={
                "setup_intent": {
                    "id": "seti_test_confirm",
                    "status": "requires_action",
                },
                "status": "requires_action",
            },
        ),
    )
    # Best-effort snapshot billing_address (chatgpt_client.snapshot_billing).
    session.route(
        "POST",
        "https://chatgpt.com/backend-api/payments/checkout/snapshot",
        FakeResponse(200, json_body={}),
    )
    session.route(
        "POST",
        "https://chatgpt.com/backend-api/payments/checkout/approve",
        FakeResponse(200, json_body={"result": "approved"}),
    )
    # Step 9 — Stripe refresh_poll. GET /v1/payment_pages/{id} (không có suffix).
    session.route(
        "GET",
        f"https://api.stripe.com/v1/payment_pages/{checkout_session_id}",
        FakeResponse(
            200,
            json_body={
                "setup_intent": {
                    "id": "seti_test_refresh",
                    "next_action": {
                        "type": "redirect_to_url",
                        "redirect_to_url": {"url": redirect_to_url},
                    },
                }
            },
        ),
    )
    # Step 10 — follow_redirect 302 → pay.ideal.nl (single-hop, no history).
    # Note: production stripe_client uses allow_redirects=True and reads
    # response.url. For FakeAsyncSession we short-circuit: return a "final"
    # response with .url set to ideal_location.
    session.route(
        "GET",
        redirect_to_url,
        FakeResponse(
            200,
            headers={"Location": ideal_location},
            url=ideal_location,
        ),
    )
    # Step 11 — pay.ideal.nl transaction initiate.
    session.route(
        "POST",
        f"https://pay.ideal.nl/api/v1/transactions/{encoded_tx_url}/initiate",
        FakeResponse(
            200,
            json_body={
                "view": "INITIAL_VIEW",
                "amount": amount,
                "creditorName": creditor_name,
                "qrCodeUrl": "https://tx.ideal.nl/2/tx_test?sig=" + sig_value,
                "payloadUri": "https%3A%2F%2Ftx.ideal.nl%2F2%2Ftx_test",
                "supportedIssuers": [
                    {
                        "id": default_issuer_id,
                        "deeplinkType": "url",
                        "deeplink": issuer_deeplink,
                        "availabilityStatus": "AVAILABLE",
                    }
                ],
            },
        ),
    )
    # Step 11a — GET pay.ideal.nl/transactions/{tx} to warm cookie
    # (flow.py step 11a fresh client GET landing page). Return HTML shell 200.
    session.route(
        "GET",
        f"https://pay.ideal.nl/transactions/{encoded_tx_url}",
        FakeResponse(200, text="<html>iDEAL</html>"),
    )
    # Step ~4.5: stripe_client.ensure_token_config() fetch js.stripe.com/v3/
    # cho js_checksum + rv_timestamp. Test không cần fetch live — raise
    # `http.HTTPError` để trigger cache fallback path trong
    # `stripe_token.fetch_bundles_live` (dùng bundle cache tại
    # `runtime/cache/stripe_bundles/*/`). Requires cache dir pre-populated
    # bởi 1 lần chạy live trước đó.
    session.route(
        "GET",
        "https://js.stripe.com/v3/",
        http.HTTPError("test-fake-fetch-fail"),
    )
