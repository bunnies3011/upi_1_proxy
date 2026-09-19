"""Dump POST body của events [004-011] trước /confirm.

HAR có 8 POSTs update `/payment_pages/{id}` từ browser thực tế (progressive
save khi user gõ). Flow của mình CHỈ có 1 update_billing → có thể thiếu
1 số key field khiến Stripe không attach setup_intent sau confirm.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote_plus

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
        for ext in (".txt", ".json", ".html", ".bin", ""):
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
        for ext in (".txt", ".json", ".html", ".bin", ""):
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
        if "api.stripe.com" not in u:
            continue
        events.append(snap)

    # Chỉ giữ POSTs tới /v1/payment_pages/{id} (KHÔNG /init, /confirm, /update):
    # request kiểu progressive update.
    print("=== POSTs tới /payment_pages/{id} (not init/confirm/update endpoints) ===\n", flush=True)
    idx = 0
    for snap in events:
        req = snap["request"]
        url = req.get("url", "")
        m = req.get("method", "")
        # Match /payment_pages/cs_live_XXX (exact — no trailing suffix)
        # Stripe path pattern: /v1/payment_pages/cs_live_XXX
        # 8 updates: url ends exact with checkout_session_id
        if m != "POST":
            continue
        if "/payment_pages/" not in url:
            continue
        if any(url.endswith(sfx) or f"{sfx}?" in url for sfx in ["/init", "/confirm"]):
            continue

        body = load_post(req.get("postData", {}))
        resp = snap["response"]
        # Parse form fields
        parsed = parse_qs(body, keep_blank_values=True)
        # Print field names + short values
        print(f"--- Event {idx}: POST {url[:100]} → {resp.get('status')} ---", flush=True)
        print(f"    field_count: {len(parsed)}", flush=True)
        # Print top-level field GROUP (before first `[`)
        groups = {}
        for k in parsed.keys():
            top = k.split("[", 1)[0]
            groups.setdefault(top, []).append(k)
        for top, fields in groups.items():
            print(f"    - {top}: {len(fields)} field(s)", flush=True)
            for f in fields[:3]:
                v = parsed[f]
                v_s = str(v[0])[:80] if v else "?"
                print(f"       {f} = {v_s!r}", flush=True)
        idx += 1
        print("", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
