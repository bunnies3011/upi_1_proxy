"""Verify URL Stripe hosted checkout mở được và render iDEAL bank picker.

Test: HTTP GET tới 1 payment_link → parse HTML → search iDEAL bank names.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import cv2
import httpx

ROOT = Path(__file__).resolve().parents[1]


async def main() -> int:
    qr_dir = ROOT / "runtime" / "qr"
    files = sorted(qr_dir.glob("*.png"))
    if not files:
        print("[FAIL] no QR files in runtime/qr/", flush=True)
        return 1

    # Decode 1 QR đầu → lấy URL
    import zxingcpp
    from PIL import Image

    img = Image.open(files[0])
    results = zxingcpp.read_barcodes(img)
    if not results:
        print(f"[FAIL] cannot decode {files[0].name}", flush=True)
        return 1
    url = results[0].text
    print(f"[QR] decoded URL: {url[:120]}...", flush=True)

    # HTTP GET URL, follow redirect, parse HTML
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=30.0,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/141.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    ) as client:
        r = await client.get(url)
        print(f"[HTTP] status={r.status_code}  final_url={str(r.url)[:100]}...", flush=True)
        print(f"[HTTP] content-length={len(r.text)} bytes", flush=True)

        # Search for iDEAL indicators trong HTML
        text_lower = r.text.lower()
        indicators = {
            "iDEAL keyword": "ideal" in text_lower,
            "rabobank": "rabobank" in text_lower,
            "ing bank": "ing" in text_lower and "bank" in text_lower,
            "checkout stripe assets": "stripe.com" in text_lower,
            "cs_live_ id": "cs_live_" in text_lower,
            "Netherlands NL": "nl" in text_lower and ("netherlands" in text_lower or "nederland" in text_lower or "netherland" in text_lower),
        }
        print("\n[HTML indicators]", flush=True)
        for k, v in indicators.items():
            marker = "✓" if v else "✗"
            print(f"  {marker} {k}: {v}", flush=True)

        # Check title
        title_match = re.search(r"<title[^>]*>([^<]+)</title>", r.text, re.I)
        if title_match:
            print(f"\n[HTML title]: {title_match.group(1).strip()}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
