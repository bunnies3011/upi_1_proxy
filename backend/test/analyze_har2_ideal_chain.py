"""Analyze HAR mới để trace chain: Stripe confirm -> pay.ideal.nl QR page.

Cần tìm chuỗi request từ khi user click "iDEAL" -> đến khi trang pay.ideal.nl load.
Đặc biệt tìm:
  - Response confirm chứa gì (redirect url? hooks.stripe.com?)
  - refresh_poll pattern
  - hooks.stripe.com/redirect/authenticate/src_ ...
  - tx.ideal.nl -> pay.ideal.nl chain
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote

TRACE = Path("/tmp/har_trace2/trace.network")


def iter_requests():
    with TRACE.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "resource-snapshot":
                continue
            snap = rec.get("snapshot", {})
            req = snap.get("request", {})
            resp = snap.get("response", {})
            url = req.get("url", "")
            yield {
                "url": url,
                "method": req.get("method", ""),
                "status": resp.get("status"),
                "post": req.get("postData", ""),
                "resp_headers": resp.get("headers", []),
                "resp_body": resp.get("content", {}).get("text", "")
                    if isinstance(resp.get("content"), dict) else "",
            }


def main() -> int:
    # Interesting hosts theo thứ tự trong chain
    hosts_interest = [
        "api.stripe.com",         # /v1/payment_pages/... confirm + refresh
        "hooks.stripe.com",       # /redirect/authenticate/src_xxx
        "tx.ideal.nl",            # transaction initiate
        "pay.ideal.nl",           # QR page render
    ]
    events: list[dict] = []
    for r in iter_requests():
        for h in hosts_interest:
            if h in r["url"]:
                events.append(r)
                break

    print(f"[INFO] {len(events)} events matching iDEAL/Stripe chain\n", flush=True)

    # Chỉ giữ những request có ý nghĩa cho chain
    # Chỉ log unique path để dễ đọc
    seen = set()
    for i, r in enumerate(events):
        u = r["url"]
        # Rút gọn path để dễ đọc
        key = u.split("?")[0][:200]
        # In tất cả nhưng đánh dấu trùng
        marker = "  " if key in seen else "* "
        seen.add(key)
        # print đầy đủ nhưng cắt để dễ đọc
        u_show = u if len(u) <= 200 else u[:180] + "..."
        print(f"{marker}[{i:03d}] {r['method']:5s} {r['status']}  {u_show}", flush=True)

    # Sau đó tìm CHÍNH XÁC:
    # 1) response Stripe confirm có payload gì (chứa next_action.redirect_to_url ?)
    # 2) URL /hooks.stripe.com/redirect
    # 3) URL /tx.ideal.nl/
    # 4) URL /pay.ideal.nl/transactions/
    print("\n=== SPECIFIC CHAIN ===\n", flush=True)
    for keyword, label in [
        ("payment_pages.*confirm", "Stripe confirm"),
        ("payment_pages.*refresh", "Stripe refresh_poll"),
        ("hooks.stripe.com/redirect", "hooks.stripe.com redirect"),
        ("tx.ideal.nl", "tx.ideal.nl"),
        ("pay.ideal.nl/transactions", "pay.ideal.nl transactions"),
        ("pay.ideal.nl/(v1|api)", "pay.ideal.nl API"),
    ]:
        pat = re.compile(keyword)
        print(f"-- {label} --", flush=True)
        for i, r in enumerate(events):
            if pat.search(r["url"]):
                body_snip = (r["resp_body"] or "")[:400].replace("\n", " ") if r["resp_body"] else ""
                print(f"  [{i}] {r['method']} {r['status']} {r['url'][:180]}", flush=True)
                if r["post"]:
                    post_s = r["post"] if isinstance(r["post"], str) else json.dumps(r["post"])
                    print(f"        POST: {post_s[:200]}", flush=True)
                if body_snip:
                    print(f"        RESP[0:400]: {body_snip}", flush=True)
        print("", flush=True)

    # Cụ thể: extract response body của refresh_poll để xem có redirect_to_url không
    print("=== SEARCH RESPONSE BODY chứa 'hooks.stripe.com' hoặc 'pay.ideal.nl' ===\n", flush=True)
    for i, r in enumerate(events):
        body = r.get("resp_body") or ""
        if not body:
            continue
        if "hooks.stripe.com" in body or "pay.ideal.nl" in body or "tx.ideal.nl" in body:
            print(f"  [{i}] {r['method']} {r['status']} {r['url'][:150]}", flush=True)
            # Print bit context around any of the keywords
            for kw in ["hooks.stripe.com", "pay.ideal.nl", "tx.ideal.nl", "redirect_to_url", "next_action"]:
                pos = body.find(kw)
                if pos != -1:
                    lo = max(0, pos - 30)
                    hi = min(len(body), pos + 200)
                    print(f"        ~{kw}: ...{body[lo:hi]}...", flush=True)
            print("", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
