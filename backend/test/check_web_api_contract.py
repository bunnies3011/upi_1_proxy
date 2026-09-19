"""Sanity check web API contract: verify các endpoint FE dùng trả đúng shape.

Cụ thể:
- GET  /api/jobs                     → list[JobViewCompact] có payment_link
- GET  /api/jobs/{id}                → JobViewDetail có payment_link
- GET  /api/jobs/{id}/qr.png         → binary PNG
- GET  /api/events (SSE)             → text/event-stream

Đọc token từ DB settings (`web.auth_token`) để gọi API.
"""
from __future__ import annotations
import asyncio
import sqlite3
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "runtime" / "e2e_test.db"
API_BASE = "http://127.0.0.1:8989"


def _get_auth_token() -> str:
    if not DB_PATH.exists():
        print(f"[FAIL] no DB: {DB_PATH}", flush=True)
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT value FROM settings WHERE key='web.auth_token'")
    row = cur.fetchone()
    conn.close()
    if not row:
        print("[FAIL] web.auth_token not set", flush=True)
        sys.exit(1)
    import json as _json
    val = row[0]
    if isinstance(val, str) and val.startswith('"') and val.endswith('"'):
        return _json.loads(val)
    return val


async def main() -> int:
    token = _get_auth_token()
    print(f"[info] token: {token[:8]}...", flush=True)
    headers = {"X-Auth-Token": token, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        # 1) GET /api/jobs
        r = await client.get(f"{API_BASE}/api/jobs")
        print(f"\n[1] GET /api/jobs → {r.status_code}", flush=True)
        if r.status_code == 200:
            jobs = r.json()
            print(f"    count={len(jobs)}", flush=True)
            qr_ready = [j for j in jobs if j.get("status") == "qr_ready"]
            print(f"    qr_ready={len(qr_ready)}", flush=True)
            if qr_ready:
                j = qr_ready[0]
                print(f"    sample fields: {list(j.keys())}", flush=True)
                print(f"    payment_link: {(j.get('payment_link') or '')[:80]}...", flush=True)

                # 2) GET /api/jobs/{id}
                jid = j["job_id"]
                r2 = await client.get(f"{API_BASE}/api/jobs/{jid}")
                print(f"\n[2] GET /api/jobs/{jid[:8]} → {r2.status_code}", flush=True)
                if r2.status_code == 200:
                    detail = r2.json()
                    print(f"    keys: {list(detail.keys())}", flush=True)
                    print(f"    logs count: {len(detail.get('logs', []))}", flush=True)
                    print(f"    payment_link: {(detail.get('payment_link') or '')[:80]}...", flush=True)
                    print(f"    artifact_path: {detail.get('artifact_path', '')}", flush=True)

                # 3) GET /api/jobs/{id}/qr.png
                r3 = await client.get(f"{API_BASE}/api/jobs/{jid}/qr.png")
                print(f"\n[3] GET /api/jobs/{jid[:8]}/qr.png → {r3.status_code}", flush=True)
                print(f"    Content-Type: {r3.headers.get('content-type')}", flush=True)
                print(f"    Size: {len(r3.content)} bytes", flush=True)
        else:
            print(f"    body: {r.text[:300]}", flush=True)

        # 4) SSE quick smoke — open stream, read first event, close
        print(f"\n[4] GET /api/events (SSE)", flush=True)
        try:
            async with client.stream(
                "GET",
                f"{API_BASE}/api/events",
                headers={**headers, "Accept": "text/event-stream"},
                timeout=5.0,
            ) as sse_r:
                print(f"    status={sse_r.status_code}, content-type={sse_r.headers.get('content-type')}", flush=True)
                first_line = ""
                try:
                    async for line in sse_r.aiter_lines():
                        if line:
                            first_line = line
                            break
                except Exception:
                    pass
                print(f"    first line: {first_line[:120]}", flush=True)
        except Exception as e:
            print(f"    SSE stream error: {e}", flush=True)

    print("\n[done]", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
