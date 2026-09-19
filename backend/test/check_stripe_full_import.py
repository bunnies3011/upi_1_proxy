"""Verify stripe_client full import + basic flow."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ast

for f in [
    ROOT / "app/payments/ideal/stripe_client.py",
    ROOT / "app/payments/ideal/models.py",
    ROOT / "app/payments/ideal/flow.py",
]:
    ast.parse(f.read_text(encoding="utf-8"))
    print(f"[AST-OK] {f.relative_to(ROOT)}", flush=True)

from app.payments.ideal.stripe_client import (  # noqa: E402
    StripeClient,
    _flatten_form,
    _encode_form_urlencoded,
    _stripe_guid,
)
from app.payments.ideal.models import StripePaymentPageInit  # noqa: E402
from app.payments.ideal.flow import IdealFlowHandler  # noqa: E402

# Test StripePaymentPageInit có amount field
init = StripePaymentPageInit(init_checksum="c1", config_id="cfg1", amount=2000)
assert init.amount == 2000
print(f"[MODEL-OK] StripePaymentPageInit amount={init.amount}", flush=True)

# Test _stripe_guid format
g = _stripe_guid()
assert len(g) > 40, g
print(f"[GUID-OK] len={len(g)} sample={g[:20]}…", flush=True)

# Test _flatten_form + _encode_form_urlencoded
payload = {"a": {"b": [1, 2]}, "c": "x"}
pairs = _flatten_form(payload)
enc = _encode_form_urlencoded(pairs)
assert b"a%5Bb%5D%5B0%5D=1" in enc, enc
print(f"[ENCODE-OK] {enc[:80]!r}", flush=True)

print("[ALL-OK]", flush=True)
