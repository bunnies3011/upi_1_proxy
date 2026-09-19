"""Dump FULL response của các event pay.ideal.nl để tìm QR payload:
  event 35: POST /api/v1/transactions/{encoded}/initiate
  event 38: PUT /api/v1/transactions/initiate
  event 52: GET /api/v1/transactions/{encoded}/status

Cũng dump event 14 HTML để xem có QR nội dung nhúng inline không.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

TRACE = Path("/tmp/har_trace2/trace.network")
RES = Path("/tmp/har_trace2/resources")


def load_body(content):
    if not isinstance(content, dict):
        return ""
    text = content.get("text") or ""
    if text:
        return text if isinstance(text, str) else json.dumps(text)
    sha = content.get("_sha1")
    if sha:
        for ext in (".json", ".txt", ".html", ".bin", ""):
            p = RES / f"{sha}{ext}"
            if p.exists():
                return p.read_text(errors="replace")
    return ""


def load_post(post):
    if isinstance(post, str):
        return post
    if not isinstance(post, dict):
        return ""
    text = post.get("text") or ""
    if text:
        return text if isinstance(text, str) else json.dumps(text)
    sha = post.get("_sha1")
    if sha:
        for ext in (".json", ".txt", ".html", ".bin", ""):
            p = RES / f"{sha}{ext}"
            if p.exists():
                return p.read_text(errors="replace")
    return ""


def iter_snap():
    with TRACE.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") == "resource-snapshot":
                yield rec["snapshot"]


def main() -> int:
    events = []
    for snap in iter_snap():
        u = snap["request"].get("url", "")
        if "pay.ideal.nl" in u or "api.stripe.com" in u:
            events.append(snap)

    # Tìm các events pay.ideal.nl
    for snap in events:
        u = snap["request"].get("url", "")
        m = snap["request"].get("method", "")
        if "pay.ideal.nl" not in u:
            continue
        # Chỉ xử lý transactions/initiate/status endpoints, bỏ static assets
        if not any(kw in u for kw in [
            "/transactions/",
            "/api/v1/transactions",
            "/status",
            "/initiate",
        ]):
            continue
        if any(kw in u for kw in ["/static/", ".css", ".woff2", ".svg", ".png"]):
            continue
        print(f"\n========== {m} {u[:150]} ==========", flush=True)

        resp = snap["response"]
        print(f"  Status: {resp.get('status')}", flush=True)
        # response headers
        for h in (resp.get("headers") or [])[:20]:
            print(f"  H: {h.get('name')} = {(h.get('value') or '')[:120]}", flush=True)

        post_body = load_post(snap["request"].get("postData", {}))
        if post_body:
            print(f"  POST body ({len(post_body)} chars):", flush=True)
            print(f"    {post_body[:500]}", flush=True)

        resp_body = load_body(resp.get("content", {}))
        if resp_body:
            print(f"  RESP body ({len(resp_body)} chars):", flush=True)
            # Try JSON
            try:
                data = json.loads(resp_body)
                # Dump full JSON compactly
                pretty = json.dumps(data, indent=2)
                # Trim to 4000 chars, otherwise full
                if len(pretty) < 6000:
                    print(f"    {pretty}", flush=True)
                else:
                    print(f"    {pretty[:6000]}\n    ... (truncated)", flush=True)
            except Exception:
                print(f"    [raw] {resp_body[:2000]}", flush=True)
        else:
            print(f"  RESP body: EMPTY (or blob content only)", flush=True)

    # Check HTML event 14 for QR-related keywords
    print("\n\n=== SEARCH HTML page pay.ideal.nl for QR-related keywords ===", flush=True)
    for snap in events:
        u = snap["request"].get("url", "")
        if "pay.ideal.nl/transactions/" not in u or "/api/" in u:
            continue
        body = load_body(snap["response"].get("content", {}))
        if not body:
            continue
        # Search for tokens hinting inline QR content or endpoint
        for kw in ["qr", "canvas", "wero", "sepa", "sct", "spc", "payconiq", "epc", "svg"]:
            count = body.lower().count(kw)
            if count > 0:
                print(f"  [{u[:80]}] '{kw}' × {count}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
