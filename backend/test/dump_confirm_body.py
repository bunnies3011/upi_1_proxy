"""Dump body form gửi tới Stripe confirm — verify shape chính xác."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.payments.ideal.stripe_client import StripeClient  # noqa: E402
from app.payments.ideal.models import BillingAddress  # noqa: E402

# Fake client + settings + logger để chỉ test _build_confirm_request
class FakeLogger:
    def info(self, *a, **k): pass

class FakeSettings:
    pass


sc = StripeClient(
    http_client=None,  # type: ignore
    settings=FakeSettings(),  # type: ignore
    logger=FakeLogger(),  # type: ignore
)
sc._stripe_js_id = "test-stripe-js-id-abcdef1234"


billing = BillingAddress(
    name="Jan de Vries",
    email="test@icloud.com",
    address={
        "country": "NL",
        "line1": "Molenweg 23",
        "line2": "",
        "city": "Amsterdam",
        "postal_code": "1015 GH",
        "state": "NH",
    },
)

url, body, headers = sc._build_confirm_request(
    checkout_session_id="cs_live_test",
    publishable_key="pk_live_test",
    elements_session_id="elements_session_abc",
    billing=billing,
    init_checksum="checksum123",
    amount=1901,
    elements_config_id="ec_id_123",
    init_config_id="ic_id_456",
    default_issuer_bic="RABONL2U",
)

print(f"URL: {url}")
print(f"\nHeaders:")
for k, v in headers.items():
    print(f"  {k}: {v}")

print(f"\nBody (decoded, sorted):")
from urllib.parse import parse_qsl
pairs = parse_qsl(body.decode(), keep_blank_values=True)
for k, v in sorted(pairs):
    if len(v) > 80:
        v = v[:80] + "..."
    print(f"  {k} = {v}")

print(f"\n[TOTAL: {len(pairs)} form fields, body={len(body)} bytes]")
