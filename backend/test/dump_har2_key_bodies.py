"""Dump response body của các event key trong HAR2:
  event 012: POST /confirm
  event 013: GET /payment_pages/{id}?elements_session_client[...]
  event 035: POST /api/v1/transactions/{encoded}/initiate
  event 038: PUT /api/v1/transactions/initiate
  event 052: GET /api/v1/transactions/{encoded}/status

Response body của Playwright HAR: có thể là raw string trong content.text
hoặc link tới resources/ folder (sha1). Cần đọc cả 2 case.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

TRACE = Path("/tmp/har_trace2/trace.network")
RES = Path("/tmp/har_trace2/resources")


def load_body(content: dict) -> str:
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
                try:
                    return p.read_text(errors="replace")
                except Exception as e:
                    return f"[read err {e}]"
    return ""


def load_post(post: dict | str) -> str:
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
                try:
                    return p.read_text(errors="replace")
                except Exception as e:
                    return f"[read err {e}]"
    # sometimes params list ...
    params = post.get("params") or []
    if params:
        return json.dumps(params)
    return ""


def iter_snapshots():
    with TRACE.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "resource-snapshot":
                continue
            yield rec["snapshot"]


# Filter same as analyzer
def main() -> int:
    hosts_interest = ["api.stripe.com", "hooks.stripe.com", "tx.ideal.nl", "pay.ideal.nl"]
    events = []
    for snap in iter_snapshots():
        u = snap["request"].get("url", "")
        if any(h in u for h in hosts_interest):
            events.append(snap)

    # Print bodies for these event indices
    interested_indices = [12, 13, 35, 38, 52]
    for idx in interested_indices:
        if idx >= len(events):
            print(f"[MISS] event {idx}", flush=True)
            continue
        snap = events[idx]
        req = snap["request"]
        resp = snap["response"]
        print(f"\n========== EVENT {idx}: {req['method']} {resp.get('status')} ==========", flush=True)
        print(f"URL: {req['url']}", flush=True)
        post_body = load_post(req.get("postData", {}))
        if post_body:
            print(f"--- POST BODY ---\n{post_body[:2000]}", flush=True)
        resp_body = load_body(resp.get("content", {}))
        if resp_body:
            # Try pretty-print JSON
            try:
                parsed = json.loads(resp_body)
                pretty = json.dumps(parsed, indent=2)
                print(f"--- RESP BODY (JSON) ---\n{pretty[:4000]}", flush=True)
            except Exception:
                print(f"--- RESP BODY ---\n{resp_body[:2000]}", flush=True)
        else:
            print("--- RESP BODY: (empty) ---", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
