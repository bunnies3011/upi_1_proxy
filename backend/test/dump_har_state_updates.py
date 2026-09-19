"""Dump body của các POST /v1/payment_pages/{id} (không /init /confirm) — hiểu
Stripe state UPDATE calls giữa init và confirm. Compare cookie flow.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import parse_qsl

HAR_TRACE_DIR = Path("/tmp/har_trace")
RESOURCES = HAR_TRACE_DIR / "resources"
NETWORK = HAR_TRACE_DIR / "trace.network"


def main() -> int:
    with NETWORK.open() as f:
        idx = 0
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "resource-snapshot":
                continue
            snap = rec.get("snapshot", {})
            req = snap.get("request", {})
            url = req.get("url", "")
            method = req.get("method", "?")
            if not url or "payment_pages/" not in url:
                continue
            # Chỉ POST /v1/payment_pages/{id} (không /init /confirm)
            if not (method == "POST" and "/payment_pages/cs_live_" in url):
                continue
            path = url.split("?")[0].split("payment_pages/", 1)[1]
            if "/init" in path or "/confirm" in path:
                continue

            idx += 1
            post_data = req.get("postData", {})
            body = post_data.get("text", "")
            params = post_data.get("params", [])
            headers_list = req.get("headers", [])
            headers = {h.get("name", "").lower(): h.get("value", "") for h in headers_list}
            cookie_hdr = headers.get("cookie", "")

            print(f"\n[{idx}] POST payment_pages/<id>", flush=True)
            print(f"    body len: {post_data.get('size', 0)}b", flush=True)
            if cookie_hdr:
                cookie_names = [c.split("=", 1)[0].strip() for c in cookie_hdr.split(";")]
                print(f"    cookies (names only, {len(cookie_names)}): {sorted(set(cookie_names))}", flush=True)

            if params:
                # In các param quan trọng — tax_region, billing_address, ...
                for p in params:
                    name = p.get("name", "")
                    val = p.get("value", "")
                    if (
                        name.startswith("tax_region")
                        or name.startswith("payment_method_data")
                        or name.startswith("billing")
                        or name in {"currency", "type"}
                    ):
                        v = val if len(val) < 80 else val[:77] + "..."
                        print(f"      {name} = {v!r}", flush=True)
            elif body:
                pairs = parse_qsl(body, keep_blank_values=True)
                for k, v in pairs:
                    if (
                        k.startswith("tax_region")
                        or k.startswith("payment_method_data")
                        or k.startswith("billing")
                    ):
                        v_short = v if len(v) < 80 else v[:77] + "..."
                        print(f"      {k} = {v_short!r}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
