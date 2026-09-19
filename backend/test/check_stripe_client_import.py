"""Import check + validate `_flatten_form` behavior nhanh."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.payments.ideal.stripe_client import (  # noqa: E402
    StripeClient,
    _flatten_form,
)

# Test 1: flat dict
r1 = _flatten_form({"a": "1", "b": 2, "c": True})
assert ("a", "1") in r1 and ("b", "2") in r1 and ("c", "true") in r1, r1
print("[TEST-1] flat dict OK", flush=True)

# Test 2: nested
r2 = _flatten_form({"parent": {"child": {"grand": "v"}}})
assert ("parent[child][grand]", "v") in r2, r2
print("[TEST-2] nested dict OK", flush=True)

# Test 3: array
r3 = _flatten_form({"list": ["a", "b"]})
assert ("list[0]", "a") in r3 and ("list[1]", "b") in r3, r3
print("[TEST-3] list OK", flush=True)

# Test 4: None → empty
r4 = _flatten_form({"a": None})
assert ("a", "") in r4, r4
print("[TEST-4] None → empty OK", flush=True)

print("[ALL-OK]", flush=True)
