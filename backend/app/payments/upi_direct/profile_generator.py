"""Synthetic India billing profile for UPI Direct (no source records)."""

from __future__ import annotations

import random
from dataclasses import dataclass

_FIRST = ("Aarav", "Vivaan", "Aditya", "Vihaan", "Arjun", "Sai", "Reyansh", "Ayaan")
_LAST = ("Sharma", "Patel", "Singh", "Kumar", "Gupta", "Reddy", "Mehta", "Joshi")
_CITIES = (
    ("Mumbai", "MH", "400001"),
    ("Bengaluru", "KA", "560001"),
    ("Delhi", "DL", "110001"),
    ("Hyderabad", "TG", "500001"),
    ("Chennai", "TN", "600001"),
    ("Pune", "MH", "411001"),
)
_STREETS = ("MG Road", "Brigade Road", "Park Street", "FC Road", "Linking Road")


@dataclass(frozen=True)
class IndiaBillingProfile:
    name: str
    address_line1: str
    city: str
    state: str
    postal_code: str
    country_code: str = "IN"


def generate_india_profile(*, rng: random.Random | None = None) -> IndiaBillingProfile:
    r = rng or random.Random()
    city, state, postal = r.choice(_CITIES)
    return IndiaBillingProfile(
        name=f"{r.choice(_FIRST)} {r.choice(_LAST)}",
        address_line1=f"{r.randint(1, 200)} {r.choice(_STREETS)}",
        city=city,
        state=state,
        postal_code=postal,
        country_code="IN",
    )


__all__ = ["IndiaBillingProfile", "generate_india_profile"]
