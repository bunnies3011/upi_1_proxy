"""Verify UI + API cùng chạy trên port 8989 (không cần Vite dev server).

Check:
  - GET /            → 200 text/html (index.html)
  - GET /assets/*.js → 200 application/javascript
  - GET /assets/*.css→ 200 text/css
  - GET /api/jobs    → 200 application/json (auth)
"""
from __future__ import annotations
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "runtime" / "e2e_test.db"
API_BASE = "http://127.0.0.1:8989"


def get_token() -> str:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT value FROM settings WHERE key='web.auth_token'")
    row = cur.fetchone()
    conn.close()
    val = row[0]
    return json.loads(val) if val.startswith('"') else val


async def main() -> int:
    token = get_token()
    async with httpx.AsyncClient(base_url=API_BASE, timeout=10.0) as client:
        # 1) index.html
        r = await client.get("/")
        print(f"[1] GET /            → {r.status_code} ctype={r.headers.get('content-type')}", flush=True)
        assert r.status_code == 200, r.text[:200]
        assert "text/html" in r.headers.get("content-type", ""), r.headers.get("content-type")
        assert "<div id=\"app\">" in r.text or "<div id='app'>" in r.text or "app" in r.text.lower(), r.text[:300]
        print(f"    HTML size: {len(r.text)} bytes", flush=True)

        # 2) Extract asset paths from HTML
        import re
        js_match = re.search(r"/assets/[^\"']+\.js", r.text)
        css_match = re.search(r"/assets/[^\"']+\.css", r.text)
        if not js_match or not css_match:
            print(f"[FAIL] no asset link in HTML", flush=True)
            return 1
        js_path = js_match.group(0)
        css_path = css_match.group(0)
        print(f"    JS asset: {js_path}", flush=True)
        print(f"    CSS asset: {css_path}", flush=True)

        # 3) JS asset
        r = await client.get(js_path)
        print(f"\n[2] GET {js_path[:40]}... → {r.status_code} ctype={r.headers.get('content-type')}", flush=True)
        assert r.status_code == 200
        assert "javascript" in r.headers.get("content-type", "")
        print(f"    JS size: {len(r.content):,} bytes", flush=True)

        # 4) CSS asset
        r = await client.get(css_path)
        print(f"\n[3] GET {css_path[:40]}... → {r.status_code} ctype={r.headers.get('content-type')}", flush=True)
        assert r.status_code == 200
        assert "css" in r.headers.get("content-type", "")

        # 5) API (auth OK)
        r = await client.get("/api/jobs", headers={"X-Auth-Token": token})
        print(f"\n[4] GET /api/jobs (auth) → {r.status_code}", flush=True)
        assert r.status_code == 200
        jobs = r.json()
        print(f"    jobs count: {len(jobs)}", flush=True)

        # 6) API (unauth)
        r = await client.get("/api/jobs")
        print(f"\n[5] GET /api/jobs (no auth) → {r.status_code} (expected 401)", flush=True)
        assert r.status_code == 401, r.status_code

        # 7) SPA fallback — random path
        r = await client.get("/some/random/vue/route")
        print(f"\n[6] GET /some/random/vue/route → {r.status_code} ctype={r.headers.get('content-type')}", flush=True)
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")
        print(f"    fallback to index.html: {'app' in r.text.lower()}", flush=True)

        # 8) /api/* fallback → 404 JSON không HTML
        r = await client.get("/api/nonexistent-endpoint")
        print(f"\n[7] GET /api/nonexistent-endpoint → {r.status_code} ctype={r.headers.get('content-type')}", flush=True)
        assert r.status_code == 404
        assert "json" in r.headers.get("content-type", "")

    print("\n[PASS] Single-server UI + API working", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
