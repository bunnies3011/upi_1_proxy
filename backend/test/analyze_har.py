"""Phân tích HAR trace của flow iDEAL để hiểu Stripe/ChatGPT API calls thật.

Đọc requests.jsonl từ web_record_20260704-000744_manual, lọc:
- Requests tới Stripe payment_pages/*
- Requests tới chatgpt.com/backend-api/payments/*
- Requests tới pay.ideal.nl/*

Đặc biệt quan tâm: body của POST confirm, và response chứa
next_action/redirect_to_url — mẫu chuẩn của flow HTTP thành công.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlparse

HAR_DIR = Path(
    "/Volumes/SSD/Developments/TOOL/gpt_signup_hybrid/runtime/research_logs/web_record_20260704-000744_manual"
)
REQ_FILE = HAR_DIR / "requests.jsonl"


def _short(s, n=200):
    if not isinstance(s, str):
        s = str(s)
    return s if len(s) <= n else s[:n] + "…"


def main() -> int:
    if not REQ_FILE.exists():
        print(f"[FATAL] {REQ_FILE} not found", flush=True)
        return 1

    interesting_hosts = {
        "api.stripe.com",
        "chatgpt.com",
        "pay.ideal.nl",
        "checkout.stripe.com",
    }
    interesting_paths = (
        "/backend-api/payments/",
        "/v1/payment_pages/",
        "/v1/elements/sessions",
        "/api/v1/transactions/",
        "/c/pay/",
    )

    count = 0
    with REQ_FILE.open() as f:
        for line_num, line in enumerate(f, 1):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            url = rec.get("url", "")
            if not url:
                continue
            u = urlparse(url)
            if u.netloc not in interesting_hosts:
                continue
            if not any(p in u.path for p in interesting_paths):
                continue

            method = rec.get("method", "?")
            status = rec.get("status") or "-"
            print(f"\n===== [{count+1}] line#{line_num} {method} {u.netloc}{u.path} → {status} =====", flush=True)
            if u.query:
                print(f"  QUERY: {_short(u.query, 300)}", flush=True)

            # Request body
            body = rec.get("post_data") or rec.get("request_body")
            if body:
                if isinstance(body, str):
                    print(f"  REQ BODY: {_short(body, 500)}", flush=True)
                else:
                    print(f"  REQ BODY: {json.dumps(body, ensure_ascii=False)[:500]}", flush=True)

            # Response body (nếu có)
            resp_body = rec.get("response_body") or rec.get("response_text")
            if resp_body:
                if isinstance(resp_body, str):
                    print(f"  RESP BODY: {_short(resp_body, 500)}", flush=True)
                else:
                    print(f"  RESP BODY: {json.dumps(resp_body, ensure_ascii=False)[:500]}", flush=True)

            count += 1
            if count >= 60:
                print("\n[LIMIT 60 records]", flush=True)
                break

    print(f"\n[DONE] Total interesting requests: {count}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
