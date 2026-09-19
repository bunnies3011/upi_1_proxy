"""Smoke: submit 1 account qua API để verify E2E flow qua HTTP + SSE broadcast.

Sau khi submit, poll GET /api/jobs cho tới qr_ready hoặc timeout 90s. Đọc
payment_link + verify.
"""
from __future__ import annotations
import asyncio
import json
import sqlite3
import sys
import time
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
    if val.startswith('"'):
        return json.loads(val)
    return val


def get_first_account() -> str:
    accounts_file = ROOT / "runtime" / "accounts_e2e.txt"
    return accounts_file.read_text().splitlines()[0].strip()


async def main() -> int:
    token = get_token()
    account = get_first_account()
    email = account.split("|")[0]
    print(f"[smoke] token: {token[:8]}..., account: {email}", flush=True)

    headers = {"X-Auth-Token": token, "Content-Type": "application/json"}
    async with httpx.AsyncClient(base_url=API_BASE, timeout=15.0, headers=headers) as client:
        # 1) POST /api/jobs
        r = await client.post(
            "/api/jobs",
            json={"payment_method": "ideal", "lines": [account]},
        )
        print(f"[smoke] POST /api/jobs → {r.status_code}", flush=True)
        if r.status_code != 200:
            print(f"[FAIL] body: {r.text[:300]}", flush=True)
            return 1
        body = r.json()
        job_ids = body.get("created_job_ids", [])
        print(f"[smoke] created: {job_ids}", flush=True)
        if not job_ids:
            print(f"[FAIL] no job created. skipped: {body.get('skipped')}", flush=True)
            return 1
        job_id = job_ids[0]

        # 2) Poll GET /api/jobs
        deadline = time.time() + 90
        last_status = ""
        while time.time() < deadline:
            r = await client.get("/api/jobs")
            jobs = r.json() if r.status_code == 200 else []
            match = next((j for j in jobs if j["job_id"] == job_id), None)
            if not match:
                print(f"[smoke] job not in list yet", flush=True)
                await asyncio.sleep(2)
                continue
            status = match["status"]
            if status != last_status:
                print(f"[smoke] status={status}, payment_link={(match.get('payment_link') or '')[:80]!r}...", flush=True)
                last_status = status
            if status in ("qr_ready", "error", "stopped"):
                break
            await asyncio.sleep(3)

        # 3) GET detail
        r = await client.get(f"/api/jobs/{job_id}")
        detail = r.json()
        print(f"\n[smoke] final status: {detail.get('status')}", flush=True)
        print(f"[smoke] logs count: {len(detail.get('logs', []))}", flush=True)
        print(f"[smoke] payment_link: {(detail.get('payment_link') or '')[:100]}...", flush=True)
        print(f"[smoke] artifact_path: {detail.get('artifact_path', '')}", flush=True)

        # 4) GET QR PNG
        if detail.get("status") == "qr_ready":
            r = await client.get(f"/api/jobs/{job_id}/qr.png")
            print(f"\n[smoke] GET qr.png → {r.status_code}, size={len(r.content)} bytes, ctype={r.headers.get('content-type')}", flush=True)
            return 0 if r.status_code == 200 and len(r.content) > 100 else 1
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
