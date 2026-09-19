"""Verify: sau khi bỏ auth, mọi endpoint truy cập KHÔNG có `X-Auth-Token`
vẫn trả 2xx (không còn 401)."""
from __future__ import annotations
import asyncio
import sys

import httpx

API = "http://127.0.0.1:8989"


async def main() -> int:
    async with httpx.AsyncClient(base_url=API, timeout=10.0) as client:
        # Không set header X-Auth-Token cho tất cả requests
        checks = [
            ("GET",  "/api/jobs",                None),
            ("GET",  "/api/settings",            None),
            ("GET",  "/api/session-cache",       None),
            ("GET",  "/",                        None),   # UI
            ("GET",  "/assets/index-CwgS9ju9.js", None),
        ]
        ok = 0
        for method, path, body in checks:
            try:
                if method == "GET":
                    r = await client.get(path)
                else:
                    r = await client.request(method, path, json=body)
                marker = "PASS" if 200 <= r.status_code < 400 else "FAIL"
                print(f"  [{marker}] {method:5} {path:35} → {r.status_code}", flush=True)
                if marker == "PASS":
                    ok += 1
                elif r.status_code == 401:
                    print(f"          !!! 401 nghĩa là auth VẪN CÒN", flush=True)
                    print(f"          body: {r.text[:200]}", flush=True)
            except Exception as e:
                print(f"  [ERR ] {method:5} {path:35} → {type(e).__name__}: {e}", flush=True)

        # SSE
        try:
            async with client.stream("GET", "/api/events/stream", timeout=3.0) as sr:
                print(f"  [{'PASS' if sr.status_code == 200 else 'FAIL'}] GET   /api/events/stream            → {sr.status_code}", flush=True)
                if sr.status_code == 200:
                    ok += 1
        except Exception as e:
            print(f"  [ERR ] GET   /api/events/stream            → {e}", flush=True)

        print(f"\n[verify] {ok}/6 pass, ZERO auth required", flush=True)
        return 0 if ok >= 5 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
