"""Debug helper: gọi trực tiếp chatgpt.com/api/auth/csrf để xem raw response.

Chạy: python test/debug_chatgpt_csrf.py
"""
from __future__ import annotations

import asyncio
import sys

import httpx


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/141.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9,nl;q=0.8",
    "Accept-Encoding": "gzip, deflate",  # bỏ br để đảm bảo httpx tự decode
    "Sec-CH-UA": '"Chromium";v="141", "Not?A_Brand";v="24", "Google Chrome";v="141"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


async def probe(url: str, tag: str) -> None:
    print(f"[{tag}] GET {url}", flush=True)
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0, headers=HEADERS) as c:
        r = await c.get(url)
    print(f"[{tag}] status={r.status_code}", flush=True)
    print(f"[{tag}] content-type={r.headers.get('content-type')}", flush=True)
    print(f"[{tag}] content-encoding={r.headers.get('content-encoding')}", flush=True)
    print(f"[{tag}] final-url={r.url}", flush=True)
    raw_len = len(r.content)
    print(f"[{tag}] body_bytes={raw_len}", flush=True)
    # First 200 bytes hex + text
    head = r.content[:200]
    print(f"[{tag}] head_hex={head.hex()}", flush=True)
    try:
        text_head = head.decode('utf-8', errors='replace')[:400]
    except Exception as e:
        text_head = f"<decode err {e}>"
    print(f"[{tag}] head_text={text_head!r}", flush=True)


async def main() -> int:
    await probe("https://chatgpt.com/api/auth/csrf", "csrf")
    print("---", flush=True)
    await probe("https://chatgpt.com/api/auth/session", "session")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
