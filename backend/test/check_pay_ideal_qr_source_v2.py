"""Dump full value 'qrCodeUrl', 'payloadUri' từ response POST /initiate."""

from __future__ import annotations
import asyncio
import json
import re
import sys
from urllib.parse import unquote

import httpx


URL = (
    "https://pay.ideal.nl/transactions/"
    "https%3A%2F%2Ftx.ideal.nl%2F2%2FAPR6Z4ZZQQUYLYAARZ6PDZFPU44"
    "?sig=CGBCQEIIA75FEJ7WTU5OLTT3AEKXVIDVK62Y2DYGILOIFHFR6DLOLGPIWOYOQEIB7HNNK6RNBMMTHMVEW4YCCE6SPATLV2LNDAP7NGDNDIDQWWCMTFE"
)


async def main() -> int:
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=30.0,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/141.0.0.0 Safari/537.36",
        },
    ) as client:
        # Get page first to establish tx_api_token cookie
        r = await client.get(URL)
        print(f"[1] GET pay.ideal.nl → {r.status_code} (cookie set)", flush=True)

        # POST initiate
        path_match = re.search(r"/transactions/([^?]+)", URL)
        encoded_tx = path_match.group(1)
        r2 = await client.post(
            f"https://pay.ideal.nl/api/v1/transactions/{encoded_tx}/initiate",
            json={
                "deviceInfo": {
                    "language": "en-US",
                    "timeZone": "Asia/Ho_Chi_Minh",
                    "screenWidth": 1440,
                    "screenHeight": 800,
                    "screenAvailableWidth": 1440,
                    "screenAvailableHeight": 885,
                    "colorDepth": 30,
                },
                "httpReferrer": "https://chatgpt.com/",
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Origin": "https://pay.ideal.nl",
                "Referer": URL,
            },
        )
        print(f"[2] POST /initiate → {r2.status_code}", flush=True)
        data = r2.json()

        # Print ALL string fields with value
        print("\n=== ALL response fields ===", flush=True)
        for k, v in data.items():
            if isinstance(v, (str, int, float, bool)):
                v_str = str(v) if not isinstance(v, str) else v
                print(f"  {k} = {v_str[:400]}", flush=True)
            elif isinstance(v, list):
                print(f"  {k} = list[{len(v)}]", flush=True)
            elif isinstance(v, dict):
                print(f"  {k} = dict{{keys={list(v.keys())}}}", flush=True)
            else:
                print(f"  {k} = {type(v).__name__}: {v}", flush=True)

        # Focus qrCodeUrl + payloadUri
        print("\n=== KEY VALUES ===", flush=True)
        for k in ("qrCodeUrl", "payloadUri", "returnUrl"):
            v = data.get(k)
            if v:
                print(f"  {k}: {v}", flush=True)
                print(f"  {k} (unquoted): {unquote(v)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
