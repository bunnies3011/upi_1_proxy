"""Debug helper: gọi POST /api/auth/callback/credentials để xem body 400.

Không dùng credential thật — dùng dummy để chỉ xem shape của lỗi mà server trả về
(giúp reverse-engineer endpoint expects gì).

Chạy: python test/debug_chatgpt_credentials.py
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
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9,nl;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Origin": "https://chatgpt.com",
    "Referer": "https://chatgpt.com/",
    "Sec-CH-UA": '"Chromium";v="141", "Not?A_Brand";v="24", "Google Chrome";v="141"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


async def main() -> int:
    async with httpx.AsyncClient(follow_redirects=False, timeout=30.0, headers=HEADERS) as c:
        # Step 1: fetch CSRF
        r_csrf = await c.get("https://chatgpt.com/api/auth/csrf")
        print(f"[csrf] status={r_csrf.status_code} body={r_csrf.text[:200]}", flush=True)
        try:
            csrf_token = r_csrf.json().get("csrfToken")
        except Exception as e:
            print(f"[csrf] parse fail: {e}", flush=True)
            return 1
        print(f"[csrf] token_len={len(csrf_token or '')}", flush=True)

        # Step 2: probe providers list (real NextAuth expose ở /api/auth/providers)
        r_prov = await c.get("https://chatgpt.com/api/auth/providers")
        print(f"[providers] status={r_prov.status_code} ct={r_prov.headers.get('content-type')}", flush=True)
        print(f"[providers] body={r_prov.text[:600]}", flush=True)

        # Step 3: try POST callback với dummy creds
        r_cred = await c.post(
            "https://chatgpt.com/api/auth/callback/credentials",
            data={
                "email": "dummy-probe@example.invalid",
                "password": "dummy-probe-passphrase-xyz",
                "csrfToken": csrf_token or "",
                "callbackUrl": "https://chatgpt.com/",
                "json": "true",
            },
        )
        print(f"[cred] status={r_cred.status_code} ct={r_cred.headers.get('content-type')}", flush=True)
        print(f"[cred] location={r_cred.headers.get('location')}", flush=True)
        print(f"[cred] body_len={len(r_cred.text)}", flush=True)
        print(f"[cred] body={r_cred.text[:1000]}", flush=True)

        # Step 4: probe signin GET để xem provider list HTML
        r_signin = await c.get("https://chatgpt.com/api/auth/signin")
        print(f"[signin] status={r_signin.status_code} location={r_signin.headers.get('location')}", flush=True)
        print(f"[signin] body_head={r_signin.text[:400]}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
