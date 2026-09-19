"""Synthetic Philippines billing profile for GCash Direct."""

from __future__ import annotations

import random
from dataclasses import dataclass

_FIRST = ("Miguel", "Jose", "Angelo", "Paolo", "Maria", "Ana", "Sofia")
_LAST = ("Santos", "Reyes", "Cruz", "Garcia", "Mendoza", "Ramos", "Torres")
_ADDRESSES = (
    ("Ayala Avenue, Barangay San Lorenzo", "Makati", "Metro Manila", "1226"),
    ("Roxas Boulevard, Ermita", "Manila", "Metro Manila", "1000"),
    ("East Avenue, Diliman", "Quezon City", "Metro Manila", "1100"),
    ("Osmena Boulevard", "Cebu City", "Cebu", "6000"),
    ("J.P. Laurel Avenue, Bajada", "Davao City", "Davao del Sur", "8000"),
)


@dataclass(frozen=True)
class PhilippinesBillingProfile:
    name: str
    address_line1: str
    city: str
    state: str
    postal_code: str
    country_code: str = "PH"


def generate_philippines_profile(
    *, rng: random.Random | None = None
) -> PhilippinesBillingProfile:
    r = rng or random.Random()
    line1, city, state, postal_code = r.choice(_ADDRESSES)
    return PhilippinesBillingProfile(
        name=f"{r.choice(_FIRST)} {r.choice(_LAST)}",
        address_line1=line1,
        city=city,
        state=state,
        postal_code=postal_code,
        country_code="PH",
    )


__all__ = ["PhilippinesBillingProfile", "generate_philippines_profile"]
