"""Dump toàn bộ chatgpt.com events trong HAR (create_checkout, approve...).

Cần biết:
- HAR có gọi /backend-api/payments/checkout tạo checkout không? Body gì?
- HAR có gọi /backend-api/payments/checkout/approve không?
- Nếu có approve → khi nào (giữa confirm và refresh, hay trước confirm)?
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
    idx = 0
    for snap in iter_snap():
        req = snap["request"]
        url = req.get("url", "")
        m = req.get("method", "")
        if "chatgpt.com" not in url:
            continue
        # Chỉ lấy /backend-api/payments/*
        if "/backend-api/payments" not in url:
            continue
        idx += 1
        resp = snap["response"]
        print(f"\n===== EVENT {idx}: {m} {url[:120]} =====", flush=True)
        print(f"  Status: {resp.get('status')}", flush=True)
        post = load_post(req.get("postData", {}))
        if post:
            print(f"  POST body: {post[:600]}", flush=True)
        resp_body = load_body(resp.get("content", {}))
        if resp_body:
            try:
                data = json.loads(resp_body)
                print(f"  RESP body (JSON):", flush=True)
                print(f"    keys: {list(data.keys())[:20]}", flush=True)
                for k in ("checkout_session_id", "processor_entity", "result", "status", "publishable_key", "ui_mode"):
                    if k in data:
                        v = str(data[k])[:100]
                        print(f"    {k} = {v}", flush=True)
            except Exception:
                print(f"  RESP body (raw): {resp_body[:500]}", flush=True)

    print(f"\n=== TOTAL chatgpt payments events: {idx} ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
