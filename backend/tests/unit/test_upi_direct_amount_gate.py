"""Fail-closed amount/currency parser + promo gates for UPI Direct."""

from __future__ import annotations

import pytest

from app.payments.upi_direct.amount_gate import apply_amount_gates
from app.payments.upi_direct.errors import AmountUnverifiedError, NoFreeOfferError
from app.payments.upi_direct.models import AmountState, parse_amount_state


def test_parse_elements_options_amount() -> None:
    state = parse_amount_state(
        {"elements_options": {"amount": 0, "currency": "inr"}}
    )
    assert state.amount_minor == 0
    assert state.currency == "inr"
    assert state.source_path == "elements_options.amount"


def test_parse_duplicate_equal_candidates() -> None:
    state = parse_amount_state(
        {
            "elements_options": {"amount": 0},
            "amount_total": 0,
            "currency": "INR",
        }
    )
    assert state.amount_minor == 0
    assert state.currency == "inr"


def test_parse_conflicting_amounts() -> None:
    state = parse_amount_state(
        {
            "elements_options": {"amount": 0},
            "amount_total": 100,
            "currency": "inr",
        }
    )
    assert state.amount_minor is None
    assert state.source_path and "conflict" in state.source_path


def test_parse_missing_amount() -> None:
    state = parse_amount_state({"currency": "inr"})
    assert state.amount_minor is None


def test_parse_missing_currency() -> None:
    state = parse_amount_state({"elements_options": {"amount": 0}})
    assert state.currency is None


def test_parse_wrong_types_bool_string_float() -> None:
    assert parse_amount_state({"elements_options": {"amount": True}}).amount_minor is None
    assert parse_amount_state({"elements_options": {"amount": "0"}}).amount_minor is None
    assert parse_amount_state({"elements_options": {"amount": 0.0}}).amount_minor is None


def test_parse_negative() -> None:
    assert parse_amount_state({"elements_options": {"amount": -1}}).amount_minor is None


def test_gate_require_promo_zero_ok() -> None:
    state = AmountState(0, "inr", "elements_options.amount")
    assert apply_amount_gates(state, require_promo=True).amount_minor == 0


def test_gate_require_promo_positive_fails() -> None:
    state = AmountState(19900, "inr", "elements_options.amount")
    with pytest.raises(NoFreeOfferError):
        apply_amount_gates(state, require_promo=True)


def test_gate_missing_amount_unverified_first() -> None:
    with pytest.raises(AmountUnverifiedError):
        apply_amount_gates(AmountState(None, "inr", None), require_promo=True)
    with pytest.raises(AmountUnverifiedError):
        apply_amount_gates(AmountState(0, None, None), require_promo=False)


def test_gate_wrong_currency() -> None:
    with pytest.raises(AmountUnverifiedError):
        apply_amount_gates(AmountState(0, "eur", "x"), require_promo=False)


def test_gate_require_promo_false_allows_positive() -> None:
    state = AmountState(500, "inr", "x")
    assert apply_amount_gates(state, require_promo=False).amount_minor == 500
