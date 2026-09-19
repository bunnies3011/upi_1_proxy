"""Phân tích chi tiết HAR — dump full body cho từng loại request quan trọng."""

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
    # Đọc 1 record đầu để hiểu schema
    with REQ_FILE.open() as f:
        first = json.loads(f.readline())
    print("=== SCHEMA của record 1 ===", flush=True)
    for k, v in first.items():
        if isinstance(v, (str, int, float, bool, type(None))):
            display = f"{type(v).__name__}({str(v)[:100]!r})"
        else:
            display = f"{type(v).__name__}(...)"
        print(f"  {k}: {display}", flush=True)
    print(flush=True)

    # Lọc records theo request quan trọng
    target_ranges = [
        # POST payment_pages/{id} (không init/confirm) — line 1312-1346
        (1312, 1360, "payment_pages/{id} state update"),
        # checkout/snapshot
        (1335, 1345, "chatgpt snapshot"),
        # confirm
        (1359, 1360, "stripe confirm"),
        # approve
        (1363, 1364, "chatgpt approve"),
        # GET payment_pages/{id} — sau approve
        (1365, 1366, "GET stripe refresh (post-approve)"),
        # pay.ideal.nl initiate
        (1410, 1424, "pay.ideal.nl"),
    ]

    line_num = 0
    with REQ_FILE.open() as f:
        for line in f:
            line_num += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            for lo, hi, tag in target_ranges:
                if lo <= line_num <= hi:
                    url = rec.get("url", "")
                    if not url:
                        break
                    u = urlparse(url)
                    method = rec.get("method", "?")
                    status = rec.get("status")
                    if status is None:
                        continue  # skip pending, chỉ lấy response records
                    print(f"\n=== line#{line_num} [{tag}] {method} {u.netloc}{u.path[:80]} → {status} ===", flush=True)

                    # Print all fields with body content
                    for k in [
                        "post_data", "request_body", "request_body_text",
                        "response_body", "response_text", "body",
                    ]:
                        v = rec.get(k)
                        if v:
                            if isinstance(v, str):
                                print(f"  {k}:", flush=True)
                                for line_of_v in v.split("\n")[:15]:
                                    print(f"    {line_of_v[:200]}", flush=True)
                            elif isinstance(v, (dict, list)):
                                s = json.dumps(v, ensure_ascii=False, indent=2)[:1500]
                                print(f"  {k}: {s}", flush=True)
                    break
    return 0


if __name__ == "__main__":
    sys.exit(main())
