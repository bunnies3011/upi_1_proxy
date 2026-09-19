"""Reproduce httpx 'Attempted to send an sync request with an AsyncClient instance'.

Test cụ thể: chuyện gì xảy ra khi client dùng dữ liệu form-encoded list of
tuples với nhiều request cùng session.
"""

from __future__ import annotations

import asyncio
import sys


async def main() -> int:
    import httpx

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=30.0,
    ) as client:
        # Request 1: GET
        try:
            r1 = await client.get("https://api.stripe.com/v1/payment_pages/cs_test_dummy/init")
            print(f"[R1] GET stripe: {r1.status_code}", flush=True)
        except Exception as ex:
            print(f"[R1-ERR] {type(ex).__name__}: {ex}", flush=True)

        # Request 2: POST với data list of tuples
        try:
            data = [
                ("browser_locale", "nl-NL"),
                ("key", "pk_test_dummy"),
                ("_stripe_version", "2025-03-31.basil"),
            ]
            r2 = await client.post(
                "https://api.stripe.com/v1/payment_pages/cs_test_dummy/init",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            )
            print(f"[R2] POST stripe form-list: {r2.status_code}", flush=True)
            print(f"[R2-BODY] {r2.text[:200]!r}", flush=True)
        except Exception as ex:
            print(f"[R2-ERR] {type(ex).__name__}: {ex}", flush=True)

        # Request 3: POST với data dict
        try:
            data = {"foo": "bar"}
            r3 = await client.post(
                "https://api.stripe.com/v1/payment_pages/cs_test_dummy/init",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            )
            print(f"[R3] POST stripe form-dict: {r3.status_code}", flush=True)
        except Exception as ex:
            print(f"[R3-ERR] {type(ex).__name__}: {ex}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
