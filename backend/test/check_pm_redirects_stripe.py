"""Chạy live 1 account, ngay khi có redirect_url → thử nhiều header combos
để tìm cấu hình chấp nhận bởi pm-redirects.stripe.com.

Đơn giản: chạy 1 flow, extract redirect URL từ log, thử curl-like variants.
"""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]

# Manual: nhập URL từ live run gần đây (dán tay)
# Hoặc extract từ /tmp log
LOG_PATH = Path("/tmp/ideal_qr_smoke_e2e.log")


def extract_redirect_url_from_log() -> str | None:
    if not LOG_PATH.exists():
        return None
    text = LOG_PATH.read_text()
    import re
    m = re.search(r"redirect_prefix['\"]?:\s*['\"](https://pm-redirects\.stripe\.com/authorize/[^'\"]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"redirect_to_url=['\"](https://pm-redirects\.stripe\.com/authorize/[^'\"]+)['\"]", text)
    if m:
        return m.group(1)
    return None


async def try_variant(url: str, tag: str, headers: dict, follow: bool = True):
    async with httpx.AsyncClient(follow_redirects=follow, timeout=15.0, headers=headers) as client:
        try:
            r = await client.get(url)
            print(f"  [{tag}] HTTP {r.status_code} final_url={str(r.url)[:100]}", flush=True)
            if r.status_code != 200:
                print(f"    body[:200]: {r.text[:200]!r}", flush=True)
        except Exception as e:
            print(f"  [{tag}] ERR: {e}", flush=True)


async def main() -> int:
    url = extract_redirect_url_from_log()
    if not url:
        # Hardcode 1 URL từ log gần đây (nếu extract fail)
        print("[FAIL] no URL from log, need to run e2e first", flush=True)
        return 1

    print(f"[URL] {url}", flush=True)

    # Variants
    ua_chrome = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    ua_safari = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    ua_iphone = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"

    await try_variant(url, "V1: minimal", {})
    await try_variant(url, "V2: chrome UA", {"User-Agent": ua_chrome})
    await try_variant(url, "V3: chrome UA + accept", {
        "User-Agent": ua_chrome,
        "Accept": "text/html,application/xhtml+xml,*/*",
    })
    await try_variant(url, "V4: chrome UA + navigate fetch", {
        "User-Agent": ua_chrome,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://checkout.stripe.com/",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    })
    await try_variant(url, "V5: iPhone Safari", {
        "User-Agent": ua_iphone,
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    await try_variant(url, "V6: chrome UA no follow", {
        "User-Agent": ua_chrome,
        "Accept": "text/html,*/*;q=0.9",
    }, follow=False)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
