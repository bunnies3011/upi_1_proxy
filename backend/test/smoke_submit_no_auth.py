"""Smoke E2E qua API (KHÔNG token): submit 1 account → poll qr_ready → fetch PNG.

Prerequisite: uvicorn chạy port 8989, DB có settings đủ chạy iDEAL flow.
"""
from __future__ import annotations
import asyncio
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
API = "http://127.0.0.1:8989"


def get_first_account() -> str:
    p = ROOT / "runtime" / "accounts_e2e.txt"
    return p.read_text().splitlines()[0].strip()


async def main() -> int:
    account = get_first_account()
    email = account.split("|")[0]
    print(f"[smoke] account: {email}", flush=True)

    # KHÔNG set X-Auth-Token — verify không auth
    async with httpx.AsyncClient(base_url=API, timeout=15.0) as client:
        r = await client.post(
            "/api/jobs",
            json={"payment_method": "ideal", "lines": [account]},
        )
        print(f"[1] POST /api/jobs → {r.status_code}", flush=True)
        if r.status_code != 200:
            print(f"    body: {r.text[:300]}", flush=True)
            return 1
        body = r.json()
        job_ids = body.get("created_job_ids", [])
        print(f"    created: {job_ids}", flush=True)
        if not job_ids:
            print(f"    skipped: {body.get('skipped')}", flush=True)
            return 1
        job_id = job_ids[0]

        # Poll
        deadline = time.time() + 90
        last_status = ""
        while time.time() < deadline:
            r = await client.get("/api/jobs")
            jobs = r.json() if r.status_code == 200 else []
            match = next((j for j in jobs if j["job_id"] == job_id), None)
            if match:
                status = match["status"]
                if status != last_status:
                    print(f"[poll] status={status}", flush=True)
                    last_status = status
                if status in ("qr_ready", "error", "stopped"):
                    break
            await asyncio.sleep(3)

        r = await client.get(f"/api/jobs/{job_id}")
        detail = r.json()
        print(f"\n[final] status={detail.get('status')}", flush=True)
        print(f"[final] payment_link: {(detail.get('payment_link') or '')[:100]}...", flush=True)
        print(f"[final] logs: {len(detail.get('logs', []))} entries", flush=True)

        if detail.get("status") == "qr_ready":
            r = await client.get(f"/api/jobs/{job_id}/qr.png")
            print(f"[final] GET qr.png → {r.status_code}, size={len(r.content)} bytes", flush=True)
            return 0 if r.status_code == 200 and len(r.content) > 100 else 1
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
