"""Smoke checks for UPI Direct payment_link preservation.

Run:
    python test/check_upi_direct_payment_link.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.notifiers.telegram.formatter import build_caption
from app.payments.upi_direct.confirm_qr_flow import _resolve_payment_link
from app.payments.upi_direct.models import QrInstruction


def _log(status: str, case_id: str, desc: str, detail: str = "") -> None:
    line = f"[{status}] {case_id} - {desc}"
    if detail:
        line += f" :: {detail}"
    print(line, flush=True)


def _run() -> int:
    failed = 0
    hosted_url = "https://payments.stripe.com/upi/instructions/CDQQARoXdemo"

    with_png_and_hosted = QrInstruction(
        image_url_png="https://qr.stripe.com/upiqr_123.png",
        hosted_instructions_url=hosted_url,
        expires_at=1784131200.0,
        source="next_action",
    )
    got = _resolve_payment_link(with_png_and_hosted)
    if got == hosted_url:
        _log("PASS", "TC-01", "preserves hosted link when QR PNG is present")
    else:
        _log("FAIL", "TC-01", "preserves hosted link when QR PNG is present", f"got={got!r}")
        failed += 1

    caption = build_caption(
        account_line="buyer@example.com|pw|totp",
        finished_at=1784130900.0,
        payment_link=got,
        payment_method="upi_direct",
        order=3503,
        qr_expires_at=1784131200.0,
        chat_label="@h1kalz007",
        chat_id="-5532607384",
        sent_at=1784130938.0,
    )
    if hosted_url in caption and "QR #3503 - UPI ChatGPT Plus (IN)" in caption:
        _log("PASS", "TC-02", "Telegram UPI caption includes raw hosted link")
    else:
        _log("FAIL", "TC-02", "Telegram UPI caption includes raw hosted link", caption)
        failed += 1

    png_only = QrInstruction(
        image_url_png="https://qr.stripe.com/upiqr_123.png",
        hosted_instructions_url=None,
        source="next_action",
    )
    if _resolve_payment_link(png_only) is None:
        _log("PASS", "TC-03", "no hosted link stays hidden")
    else:
        _log("FAIL", "TC-03", "no hosted link stays hidden")
        failed += 1

    bad_host = QrInstruction(
        image_url_png="https://qr.stripe.com/upiqr_123.png",
        hosted_instructions_url="https://example.com/upi/instructions/CDQQARoXdemo",
        source="next_action",
    )
    if _resolve_payment_link(bad_host) is None:
        _log("PASS", "TC-04", "rejects non-Stripe hosted link")
    else:
        _log("FAIL", "TC-04", "rejects non-Stripe hosted link")
        failed += 1

    if failed == 0:
        _log("PASS", "SUMMARY", "all checks passed")
        return 0

    _log("FAIL", "SUMMARY", f"{failed} checks failed")
    return 1


if __name__ == "__main__":
    raise SystemExit(_run())
