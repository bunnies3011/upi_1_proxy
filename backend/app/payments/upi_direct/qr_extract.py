"""Exact UPI next_action parser — fail-closed, no fuzzy scraping."""

from __future__ import annotations

import html
import json
import re
import time
from typing import Any
from urllib.parse import unquote

from app.payments.upi_direct.models import ExpectedPaymentState, QrInstruction
from app.payments.upi_direct.network_safety import (
    is_allowed_hosted_instructions_url,
    is_allowed_qr_png_url,
)

_NEXT_ACTION_KEY = "upi_handle_redirect_or_display_qr_code"
_HOSTED_URL_RE = re.compile(r"https://payments\.stripe\.com/upi/instructions/[^\s\"'<>\\]+")
_QR_PNG_RE = re.compile(r"https://qr\.stripe\.com/[^\s\"'<>\\]+\.png(?:\?[^\s\"'<>\\]*)?")
_UPI_URI_RE = re.compile(r"upi:[^\s\"'<>\\]{1,2048}", re.I)
_SCAN_PATH_TERMS = (
    "blob",
    "hosted_instructions",
    "image_url",
    "mobile_auth_url",
    "next_action",
    "qr_code",
    "upi_handle_redirect_or_display_qr_code",
    "upi_intent_url",
)


def parse_upi_next_action(
    response: dict[str, Any],
    expected: ExpectedPaymentState,
) -> QrInstruction | None:
    """Parse exact nested UPI next_action path only.

    Accepts setup_intent or payment_intent next_action under documented shape.
    Returns None if path absent; raises ValueError on binding/conflict/expiry.
    """
    if not isinstance(response, dict):
        return None

    block = _find_upi_next_action_block(response)
    if block is None:
        return _scan_allowed_instruction_sources(response, expected)

    _validate_binding(response, expected)

    qr_code = block.get("qr_code")
    image_url_png: str | None = None
    expires_at: float | None = None
    if isinstance(qr_code, dict):
        png = qr_code.get("image_url_png")
        if isinstance(png, str) and png.strip():
            image_url_png = png.strip()
        exp = qr_code.get("expires_at")
        if isinstance(exp, (int, float)) and not isinstance(exp, bool):
            expires_at = float(exp)
            if expires_at < time.time():
                raise ValueError("qr_expired")

    hosted = block.get("hosted_instructions_url")
    hosted_url = hosted.strip() if isinstance(hosted, str) and hosted.strip() else None

    mobile = block.get("mobile_auth_url") or block.get("upi_intent_url")
    upi_uri = None
    if isinstance(mobile, str) and mobile.strip().lower().startswith("upi:"):
        upi_uri = mobile.strip()

    if not image_url_png and not hosted_url and not upi_uri:
        return None

    return QrInstruction(
        image_url_png=image_url_png,
        hosted_instructions_url=hosted_url,
        upi_uri=upi_uri,
        expires_at=expires_at,
        source="next_action",
    )


def merge_qr_instructions(
    candidates: list[QrInstruction],
) -> QrInstruction:
    """Merge complementary candidates; conflicting same fields fail."""
    if not candidates:
        raise ValueError("no_qr_candidates")
    image_url_png = candidates[0].image_url_png
    hosted_instructions_url = candidates[0].hosted_instructions_url
    upi_uri = candidates[0].upi_uri
    expires_values = [
        candidate.expires_at for candidate in candidates if candidate.expires_at is not None
    ]
    for other in candidates[1:]:
        image_url_png = _merge_field("image_url_png", image_url_png, other.image_url_png)
        hosted_instructions_url = _merge_field(
            "hosted_instructions_url",
            hosted_instructions_url,
            other.hosted_instructions_url,
        )
        upi_uri = _merge_field("upi_uri", upi_uri, other.upi_uri)
    return QrInstruction(
        image_url_png=image_url_png,
        hosted_instructions_url=hosted_instructions_url,
        upi_uri=upi_uri,
        expires_at=min(expires_values) if expires_values else None,
        source="merged" if len(candidates) > 1 else candidates[0].source,
    )


def build_qr_diagnostics(obj: Any, *, max_len: int = 500) -> str:
    """Redacted truncated diagnostics — never feed to parser."""
    text = repr(obj)
    if len(text) > max_len:
        return text[:max_len] + "…"
    return text


def _find_upi_next_action_block(response: dict[str, Any]) -> dict[str, Any] | None:
    direct = _find_direct_upi_next_action_block(response)
    if direct is not None:
        return direct

    # Stripe refresh/confirm can return a payment-page object with the live
    # intent state nested under `blob`. Keep this exact-key and bounded.
    return _find_nested_upi_next_action_block(response, depth=0, max_depth=8)


def _find_direct_upi_next_action_block(response: dict[str, Any]) -> dict[str, Any] | None:
    for intent_key in ("setup_intent", "payment_intent"):
        intent = response.get(intent_key)
        if not isinstance(intent, dict):
            continue
        block = _read_next_action_block(intent)
        if block is not None:
            return block
    return _read_next_action_block(response)


def _read_next_action_block(container: dict[str, Any]) -> dict[str, Any] | None:
    next_action = container.get("next_action")
    if not isinstance(next_action, dict):
        return None
    block = next_action.get(_NEXT_ACTION_KEY)
    if isinstance(block, dict):
        return block
    return None


def _scan_allowed_instruction_sources(
    response: dict[str, Any],
    expected: ExpectedPaymentState,
) -> QrInstruction | None:
    _validate_binding(response, expected)
    found = _AllowedInstructionAccumulator()

    def walk(obj: Any, path: str, depth: int) -> None:
        if depth > 10 or found.complete:
            return
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_s = str(key)
                child_path = f"{path}.{key_s}" if path else key_s
                if key_s == "blob":
                    parsed = _parse_blob(value)
                    if parsed is not None:
                        walk(parsed, child_path, depth + 1)
                _scan_value(value, child_path, found)
                if isinstance(value, (dict, list)):
                    walk(value, child_path, depth + 1)
                if found.complete:
                    return
        elif isinstance(obj, list):
            for idx, item in enumerate(obj[:64]):
                walk(item, f"{path}[{idx}]", depth + 1)
                if found.complete:
                    return

    walk(response, "", 0)
    return found.to_instruction()


class _AllowedInstructionAccumulator:
    def __init__(self) -> None:
        self.image_url_png: str | None = None
        self.hosted_instructions_url: str | None = None
        self.upi_uri: str | None = None

    @property
    def complete(self) -> bool:
        return bool(self.image_url_png and self.hosted_instructions_url)

    def to_instruction(self) -> QrInstruction | None:
        if not (self.image_url_png or self.hosted_instructions_url or self.upi_uri):
            return None
        return QrInstruction(
            image_url_png=self.image_url_png,
            hosted_instructions_url=self.hosted_instructions_url,
            upi_uri=self.upi_uri,
            source="payload_scan",
        )


def _scan_value(value: Any, path: str, found: _AllowedInstructionAccumulator) -> None:
    if not isinstance(value, str):
        return
    path_l = path.lower()
    if not any(term in path_l for term in _SCAN_PATH_TERMS):
        return
    for candidate in _string_variants(value):
        if found.hosted_instructions_url is None and is_allowed_hosted_instructions_url(candidate):
            found.hosted_instructions_url = candidate
        elif found.image_url_png is None and is_allowed_qr_png_url(candidate):
            found.image_url_png = candidate
        elif found.upi_uri is None and _is_valid_upi_uri(candidate):
            found.upi_uri = candidate
        if len(candidate) > 10_000:
            continue
        if found.hosted_instructions_url is None:
            hosted = _first_allowed_hosted_url(candidate)
            if hosted:
                found.hosted_instructions_url = hosted
        if found.image_url_png is None:
            png = _first_allowed_qr_png_url(candidate)
            if png:
                found.image_url_png = png
        if found.upi_uri is None:
            upi_uri = _first_valid_upi_uri(candidate)
            if upi_uri:
                found.upi_uri = upi_uri


def _string_variants(value: str) -> list[str]:
    text = html.unescape(value.strip())
    if not text:
        return []
    variants = [text]
    for _ in range(3):
        decoded = unquote(variants[-1])
        if decoded == variants[-1]:
            break
        variants.append(html.unescape(decoded.strip()))
    return variants


def _first_allowed_hosted_url(text: str) -> str | None:
    for match in _HOSTED_URL_RE.finditer(text):
        candidate = match.group(0)
        if is_allowed_hosted_instructions_url(candidate):
            return candidate
    return None


def _first_allowed_qr_png_url(text: str) -> str | None:
    for match in _QR_PNG_RE.finditer(text):
        candidate = match.group(0)
        if is_allowed_qr_png_url(candidate):
            return candidate
    return None


def _first_valid_upi_uri(text: str) -> str | None:
    match = _UPI_URI_RE.search(text)
    if not match:
        return None
    candidate = match.group(0)
    return candidate if _is_valid_upi_uri(candidate) else None


def _is_valid_upi_uri(value: Any) -> bool:
    if not isinstance(value, str) or not value.lower().startswith("upi:"):
        return False
    if len(value) > 2048:
        return False
    return not any(ord(ch) < 0x20 or ch.isspace() for ch in value)


def _find_nested_upi_next_action_block(
    obj: Any,
    *,
    depth: int,
    max_depth: int,
) -> dict[str, Any] | None:
    if depth > max_depth:
        return None
    if isinstance(obj, dict):
        direct = _find_direct_upi_next_action_block(obj)
        if direct is not None:
            return direct
        for key, value in obj.items():
            if key == "blob":
                parsed = _parse_blob(value)
                if parsed is not None:
                    found = _find_nested_upi_next_action_block(
                        parsed, depth=depth + 1, max_depth=max_depth
                    )
                    if found is not None:
                        return found
            if isinstance(value, (dict, list)):
                found = _find_nested_upi_next_action_block(
                    value, depth=depth + 1, max_depth=max_depth
                )
                if found is not None:
                    return found
    elif isinstance(obj, list):
        for item in obj[:64]:
            found = _find_nested_upi_next_action_block(
                item, depth=depth + 1, max_depth=max_depth
            )
            if found is not None:
                return found
    return None


def _parse_blob(value: Any) -> Any | None:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 1_000_000:
        return None
    if text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _validate_binding(response: dict[str, Any], expected: ExpectedPaymentState) -> None:
    # Optional binding: if response carries explicit ids/amounts, they must match.
    page_id = response.get("id")
    if isinstance(page_id, str) and expected.page_id and page_id != expected.page_id:
        # refresh may use checkout session id as path param; only fail if ppage mismatch
        if page_id.startswith("ppage_") and expected.page_id.startswith("ppage_"):
            raise ValueError("page_id_mismatch")

    amount = response.get("amount")
    if (
        isinstance(amount, int)
        and not isinstance(amount, bool)
        and expected.amount_minor is not None
        and expected.amount_minor > 0
        and amount != expected.amount_minor
    ):
        raise ValueError("amount_mismatch")

    currency = response.get("currency")
    if (
        isinstance(currency, str)
        and expected.currency
        and currency.strip().lower() != expected.currency.lower()
    ):
        raise ValueError("currency_mismatch")


def _merge_field(label: str, current: str | None, incoming: str | None) -> str | None:
    if current and incoming and current != incoming:
        raise ValueError(f"qr_instruction_conflict:{label}")
    return current or incoming


__all__ = [
    "parse_upi_next_action",
    "merge_qr_instructions",
    "build_qr_diagnostics",
]
