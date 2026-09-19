"""Typed results for the UPI vendor client (`payments/upi/`).

These frozen dataclasses hold the parsed shapes of the four `pix.capybara.cv`
endpoints. Keeping every vendor-schema field here (and in `vendor_client.py`)
means a host/schema change is a single-file swap — no vendor wire key leaks into
the license pool or the flow handler (they consume these typed attributes).

Payment_Module_Boundary: imports NOTHING from `app.core.*` or
`app.payments.ideal`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProgressEvent:
    """One non-final NDJSON progress object from `run`.

    `label`/`attempts` are absent on the earliest events (e.g. `starting`), so
    they default to `None`.
    """

    stage: str
    percent: int
    label: str | None = None
    attempts: int | None = None


@dataclass(frozen=True)
class VendorRunResult:
    """The parsed final `{"stage":"done","result":{…}}` of a `run` stream.

    On success `ok=True`, `code="upi_qr_ready"`, `qr_image_png` is a
    `data:image/png;base64,…` URI and `hosted_url` is the payment link. On a
    business failure `ok=False` and `code` carries the vendor's error code (the
    QR/link fields are `None`). QR decoding is the handler's job, not this
    client's — the raw fields are returned as-is.
    """

    ok: bool
    code: str | None
    qr_image_png: str | None
    qr_image_svg: str | None
    hosted_url: str | None
    amount: int | float | None
    currency: str | None
    intent_type: str | None
    expires_at: str | None
    elapsed_ms: int | None
    attempts: int | None


@dataclass(frozen=True)
class AccountCheck:
    """Parsed `POST /api/account/check` response — eligibility gate."""

    eligible: bool
    is_paid: bool
    plan_type: str | None


@dataclass(frozen=True)
class KeyVerifyItem:
    """One PK-code entry from `POST /api/v1/key/verify`."""

    code: str
    remaining: int
    total: int
    valid: bool


@dataclass(frozen=True)
class KeyVerify:
    """Parsed `POST /api/v1/key/verify` response — license credit per code."""

    channel: str
    items: tuple[KeyVerifyItem, ...]
    total: int
    valid: bool


@dataclass(frozen=True)
class Challenge:
    """Parsed `POST /api/chatgpt/challenge` response — anti-abuse token that is
    echoed back inside the `run` request body."""

    nonce: str
    expires: int
    mac: str


__all__ = [
    "ProgressEvent",
    "VendorRunResult",
    "AccountCheck",
    "KeyVerifyItem",
    "KeyVerify",
    "Challenge",
]
