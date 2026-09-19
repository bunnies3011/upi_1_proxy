"""Test xem httpx GET params=<list of tuples> có bug tương tự không."""

from __future__ import annotations

import asyncio
import sys


async def main() -> int:
    import httpx

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        # GET với params list
        try:
            params = [
                ("a", "1"),
                ("b[0]", "x"),
                ("b[1]", "y"),
            ]
            r = await client.get(
                "https://api.stripe.com/v1/elements/sessions",
                params=params,
                headers={"Accept": "application/json"},
            )
            print(f"[GET params-list] status={r.status_code} url={r.url}", flush=True)
        except Exception as ex:
            print(f"[GET params-list ERR] {type(ex).__name__}: {ex}", flush=True)

        # GET với data ok chưa
        try:
            r2 = await client.get(
                "https://api.stripe.com/v1/elements/sessions",
                params={"key": "pk_test", "type": "deferred_intent"},
                headers={"Accept": "application/json"},
            )
            print(f"[GET params-dict] status={r2.status_code}", flush=True)
        except Exception as ex:
            print(f"[GET params-dict ERR] {type(ex).__name__}: {ex}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
