"""Fixtures for OaiPay client / flow tests (secrets → placeholders)."""

from __future__ import annotations

import json

# Tiny valid 1x1 PNG
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
    "AAAABJRU5ErkJggg=="
)
QR_DATA_URI = f"data:image/png;base64,{_PNG_B64}"
LONG_URL = "https://checkout.stripe.com/c/pay/cs_test_placeholder#upi"
SOLVER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SolverUA/oaipay"


def done_result_ok() -> dict:
    return {
        "ok": True,
        "cs_id": "cs_test_placeholder",
        "provider_redirect_url": QR_DATA_URI,
        "long_url": LONG_URL,
        "stripe_redirect_url": "https://checkout.stripe.com/redirect",
        "upi_intent_id": "pi_test",
        "upi_expires_at": "2026-07-12T12:00:00Z",
        "fallback": False,
        "provider_error": "",
    }


def done_event_ok() -> str:
    return "data: " + json.dumps({"type": "done", "result": done_result_ok()}) + "\n\n"


def success_sse_chunks() -> list[bytes]:
    """started → progress → done; split mid-JSON across chunks."""
    started = (
        'data: {"type":"started","active":1,"maxActive":5,"queued":2}\n\n'
    )
    progress = 'data: {"type":"progress","step":1,"total":3,"desc":"working"}\n\n'
    done = done_event_ok()
    full = started + progress + done
    mid = len(full) // 2
    return [full[:mid].encode("utf-8"), full[mid:].encode("utf-8")]


def done_result_failed(**overrides) -> dict:
    base = done_result_ok()
    base["ok"] = False
    base["provider_redirect_url"] = None
    base["long_url"] = None
    base.update(overrides)
    return base
