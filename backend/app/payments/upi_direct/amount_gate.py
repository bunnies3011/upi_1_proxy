"""Fail-closed amount/currency gates for UPI Direct."""

from __future__ import annotations

from app.payments.upi_direct.errors import AmountUnverifiedError, NoFreeOfferError
from app.payments.upi_direct.models import AmountState

_EXPECTED_CURRENCY = "inr"


def apply_amount_gates(
    amount: AmountState,
    *,
    require_promo: bool,
    expected_currency: str = _EXPECTED_CURRENCY,
) -> AmountState:
    """Validate AmountState; raise amount_unverified then no_free_offer in order."""
    if amount.amount_minor is None or amount.currency is None:
        raise AmountUnverifiedError(
            detail=f"missing amount or currency path={amount.source_path}"
        )
    expected = expected_currency.lower()
    if amount.currency != expected:
        raise AmountUnverifiedError(
            detail=f"wrong_currency={amount.currency} path={amount.source_path}"
        )
    if amount.amount_minor < 0:
        raise AmountUnverifiedError(detail="negative_amount")

    if require_promo and amount.amount_minor > 0:
        raise NoFreeOfferError(amount_minor=amount.amount_minor)
    return amount


__all__ = ["apply_amount_gates"]
