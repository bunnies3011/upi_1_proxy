"""So sánh HAR event 12 (confirm) response STATE với MY confirm response state.

Cụ thể: submission_attempt, setup_intent presence, status field.
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
    # HAR events
    print("=== HAR EVENTS ===", flush=True)
    confirm_snap = None
    refresh_snap = None
    for snap in iter_snap():
        req = snap["request"]
        url = req.get("url", "")
        m = req.get("method", "")
        if not url.startswith("https://api.stripe.com/v1/payment_pages"):
            continue
        if m == "POST" and url.endswith("/confirm"):
            confirm_snap = snap
        elif m == "GET" and "elements_session_client" in url:
            refresh_snap = snap

    for label, snap in (("CONFIRM", confirm_snap), ("REFRESH", refresh_snap)):
        print(f"\n----- {label} response -----", flush=True)
        if snap is None:
            print(" [miss]", flush=True)
            continue
        body = load_body(snap["response"].get("content", {}))
        try:
            data = json.loads(body)
        except Exception as e:
            print(f"  parse fail: {e}", flush=True)
            continue

        # KEY FIELDS
        for k in ("status", "confirm_status", "payment_status"):
            v = data.get(k)
            print(f"  {k} = {v!r}", flush=True)

        # submission_attempt
        sa = data.get("submission_attempt")
        if sa is None:
            print("  submission_attempt = None (KHÔNG có field)", flush=True)
        else:
            print(f"  submission_attempt = {json.dumps(sa)[:400]}", flush=True)

        # setup_intent / payment_intent
        si = data.get("setup_intent")
        pi = data.get("payment_intent")
        print(f"  setup_intent = {'DICT' if isinstance(si, dict) else si}", flush=True)
        if isinstance(si, dict):
            print(f"    id: {si.get('id')}", flush=True)
            print(f"    status: {si.get('status')}", flush=True)
            print(f"    next_action.type: {(si.get('next_action') or {}).get('type')}", flush=True)
        print(f"  payment_intent = {'DICT' if isinstance(pi, dict) else pi}", flush=True)

        # rqdata / site_key (hCaptcha markers)
        for k in ("rqdata", "site_key"):
            v = data.get(k)
            if isinstance(v, str):
                print(f"  {k} = len={len(v)} preview={v[:60]!r}", flush=True)
            else:
                print(f"  {k} = {v!r}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
