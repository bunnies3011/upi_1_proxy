"""Print body cụ thể của request/response quan trọng."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlparse

HAR_DIR = Path(
    "/Volumes/SSD/Developments/TOOL/gpt_signup_hybrid/runtime/research_logs/web_record_20260704-000744_manual"
)
REQ_FILE = HAR_DIR / "requests.jsonl"


def main() -> int:
    # Đọc tất cả records vào memory
    records = []
    with REQ_FILE.open() as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    print(f"Total records: {len(records)}", flush=True)
    # Distinguish phase='request' vs 'response'
    phases = {}
    for r in records:
        p = r.get("phase", "?")
        phases[p] = phases.get(p, 0) + 1
    print(f"Phases: {phases}", flush=True)

    # Filter theo API iDEAL flow, phân biệt request vs response
    def is_ideal_flow_url(url):
        if not url:
            return False
        u = urlparse(url)
        interesting_paths = (
            "/backend-api/payments/checkout",  # checkout, snapshot, approve
            "/v1/payment_pages/",  # init, confirm, refresh
            "/v1/elements/sessions",
            "/api/v1/transactions/",  # pay.ideal.nl
        )
        return u.netloc in ("api.stripe.com", "chatgpt.com", "pay.ideal.nl") and any(p in u.path for p in interesting_paths)

    matched = [r for r in records if is_ideal_flow_url(r.get("url", ""))]
    print(f"Matched: {len(matched)}", flush=True)

    # Print từng cái với body FULL
    for i, r in enumerate(matched[:40]):
        u = urlparse(r["url"])
        method = r.get("method", "?")
        status = r.get("status")
        phase = r.get("phase", "?")
        rid = r.get("rid", "?")

        # Rút gọn URL
        path_short = u.path
        if "cs_live_" in path_short:
            # replace session id với placeholder để log gọn
            parts = path_short.split("cs_live_")
            if len(parts) == 2:
                # rest = parts[1].split("/", 1)  # tách phần sau id
                path_short = parts[0] + "cs_live_<ID>" + (
                    "/" + parts[1].split("/", 1)[1] if "/" in parts[1] else ""
                )

        print(f"\n===== [{i+1}] rid={rid} phase={phase} {method} {u.netloc}{path_short} → {status} =====", flush=True)

        body = r.get("body")
        if body is not None:
            if isinstance(body, str):
                # Try parse JSON
                try:
                    parsed = json.loads(body)
                    print(f"  BODY (json):\n{json.dumps(parsed, indent=2)[:2000]}", flush=True)
                except json.JSONDecodeError:
                    # Form-encoded or plain
                    print(f"  BODY (str, len={len(body)}):", flush=True)
                    for chunk in body.split("&")[:30]:
                        print(f"    {chunk[:250]}", flush=True)
            else:
                print(f"  BODY ({type(body).__name__}): {json.dumps(body, indent=2)[:2000]}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
