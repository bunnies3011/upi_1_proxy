"""Synthetic Korea billing profile for Kakao Direct."""

from __future__ import annotations

import random
from dataclasses import dataclass

_FIRST = ("Minjun", "Seojun", "Jiho", "Doyun", "Jisoo", "Seoyeon", "Hana")
_LAST = ("Kim", "Lee", "Park", "Choi", "Jung", "Kang", "Cho")
_ADDRESSES = (
    ("110 Sejong-daero, Jung-gu", "Seoul", "Seoul", "04524"),
    ("513 Yeongdong-daero, Gangnam-gu", "Seoul", "Seoul", "06164"),
    ("17 Jong-ro 1-gil, Jongno-gu", "Seoul", "Seoul", "03157"),
    ("1001 Jungang-daero, Yeonje-gu", "Busan", "Busan", "47545"),
    ("40 Munhwa-ro, Jung-gu", "Daejeon", "Daejeon", "34944"),
)


@dataclass(frozen=True)
class KoreaBillingProfile:
    name: str
    address_line1: str
    city: str
    state: str
    postal_code: str
    country_code: str = "KR"


def generate_korea_profile(*, rng: random.Random | None = None) -> KoreaBillingProfile:
    r = rng or random.Random()
    line1, city, state, postal_code = r.choice(_ADDRESSES)
    return KoreaBillingProfile(
        name=f"{r.choice(_FIRST)} {r.choice(_LAST)}",
        address_line1=line1,
        city=city,
        state=state,
        postal_code=postal_code,
        country_code="KR",
    )


__all__ = ["KoreaBillingProfile", "generate_korea_profile"]
