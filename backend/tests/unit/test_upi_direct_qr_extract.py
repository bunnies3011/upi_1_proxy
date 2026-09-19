"""Exact UPI next_action parser + URL policy tests."""

from __future__ import annotations

import json
import time

import pytest

from app.payments.upi_direct.models import ExpectedPaymentState, QrInstruction
from app.payments.upi_direct.network_safety import (
    is_allowed_hosted_instructions_url,
    is_allowed_qr_png_url,
)
from app.payments.upi_direct.qr_extract import (
    merge_qr_instructions,
    parse_upi_next_action,
)
from app.payments.upi_direct.confirm_qr_flow import _payload_shape


def _expected() -> ExpectedPaymentState:
    return ExpectedPaymentState(
        checkout_session_id="cs_1",
        page_id="ppage_1",
        elements_session_id="els_1",
        amount_minor=0,
    )


def test_parse_exact_next_action() -> None:
    payload = {
        "setup_intent": {
            "next_action": {
                "upi_handle_redirect_or_display_qr_code": {
                    "qr_code": {
                        "image_url_png": "https://qr.stripe.com/test.png",
                        "expires_at": time.time() + 600,
                    },
                    "hosted_instructions_url": (
                        "https://payments.stripe.com/upi/instructions/abc"
                    ),
                }
            }
        }
    }
    instr = parse_upi_next_action(payload, _expected())
    assert instr is not None
    assert instr.image_url_png.endswith(".png")
    assert "upi/instructions" in (instr.hosted_instructions_url or "")


def test_parse_next_action_nested_in_blob_dict() -> None:
    payload = {
        "id": "ppage_1",
        "blob": {
            "payment_intent": {
                "next_action": {
                    "upi_handle_redirect_or_display_qr_code": {
                        "qr_code": {
                            "image_url_png": "https://qr.stripe.com/blob.png",
                            "expires_at": time.time() + 600,
                        }
                    }
                }
            }
        },
    }
    instr = parse_upi_next_action(payload, _expected())
    assert instr is not None
    assert instr.image_url_png == "https://qr.stripe.com/blob.png"


def test_parse_next_action_nested_in_blob_json_string() -> None:
    payload = {
        "id": "ppage_1",
        "blob": json.dumps(
            {
                "setup_intent": {
                    "next_action": {
                        "upi_handle_redirect_or_display_qr_code": {
                            "hosted_instructions_url": (
                                "https://payments.stripe.com/upi/instructions/blob"
                            )
                        }
                    }
                }
            }
        ),
    }
    instr = parse_upi_next_action(payload, _expected())
    assert instr is not None
    assert instr.hosted_instructions_url == (
        "https://payments.stripe.com/upi/instructions/blob"
    )


def test_reject_qr_looking_string_elsewhere() -> None:
    payload = {
        "error": {
            "message": "see https://qr.stripe.com/evil.png",
        }
    }
    assert parse_upi_next_action(payload, _expected()) is None


def test_parse_allowed_hosted_url_from_new_api_payload_shape() -> None:
    payload = {
        "id": "ppage_1",
        "server_update": {
            "next_action_payload": {
                "hosted_instructions_url": (
                    "https://payments.stripe.com/upi/instructions/new-shape"
                )
            }
        },
    }
    instr = parse_upi_next_action(payload, _expected())
    assert instr is not None
    assert instr.source == "payload_scan"
    assert instr.hosted_instructions_url == (
        "https://payments.stripe.com/upi/instructions/new-shape"
    )


def test_parse_allowed_qr_png_from_encoded_blob() -> None:
    payload = {
        "id": "ppage_1",
        "blob": json.dumps(
            {
                "next_action_payload": {
                    "qr_code": {
                        "image_url_png": "https%3A%2F%2Fqr.stripe.com%2Fnew.png"
                    }
                }
            }
        ),
    }
    instr = parse_upi_next_action(payload, _expected())
    assert instr is not None
    assert instr.image_url_png == "https://qr.stripe.com/new.png"


def test_reject_expired() -> None:
    payload = {
        "setup_intent": {
            "next_action": {
                "upi_handle_redirect_or_display_qr_code": {
                    "qr_code": {
                        "image_url_png": "https://qr.stripe.com/test.png",
                        "expires_at": time.time() - 10,
                    }
                }
            }
        }
    }
    with pytest.raises(ValueError, match="qr_expired"):
        parse_upi_next_action(payload, _expected())


def test_merge_conflict() -> None:
    a = QrInstruction(image_url_png="https://qr.stripe.com/a.png")
    b = QrInstruction(image_url_png="https://qr.stripe.com/b.png")
    with pytest.raises(ValueError, match="conflict"):
        merge_qr_instructions([a, b])


def test_url_policy_png() -> None:
    assert is_allowed_qr_png_url("https://qr.stripe.com/foo.png")
    assert not is_allowed_qr_png_url("http://qr.stripe.com/foo.png")
    assert not is_allowed_qr_png_url("https://evil.qr.stripe.com/foo.png")
    assert not is_allowed_qr_png_url("https://qr.stripe.com/foo.svg")
    assert not is_allowed_qr_png_url("https://user:pass@qr.stripe.com/foo.png")
    assert not is_allowed_qr_png_url("https://qr.stripe.com:8443/foo.png")


def test_url_policy_hosted() -> None:
    assert is_allowed_hosted_instructions_url(
        "https://payments.stripe.com/upi/instructions/xyz"
    )
    assert not is_allowed_hosted_instructions_url(
        "https://payments.stripe.com/other/xyz"
    )


def test_payload_shape_reports_next_action_keys_without_values() -> None:
    payload = {
        "id": "ppage_1",
        "setup_intent": {
            "next_action": {
                "upi_handle_redirect_or_display_qr_code": {
                    "qr_code": {"image_url_png": "https://qr.stripe.com/secret.png"}
                }
            }
        },
    }
    shape = _payload_shape(payload)
    assert "id" in shape
    assert "setup_intent.next_action=upi_handle_redirect_or_display_qr_code" in shape
    assert "paths=setup_intent{next_action}" in shape
    assert "secret.png" not in shape


def test_payload_shape_reports_blob_type_without_blob_value() -> None:
    payload = {
        "blob": "opaque-secret-blob",
        "payment_method_types": ["card", "upi"],
    }
    shape = _payload_shape(payload)
    assert "blob<str,len=18,json=no>" in shape
    assert "payment_method_types[len=2]" in shape
    assert "opaque-secret-blob" not in shape
