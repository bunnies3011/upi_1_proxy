"""Dump toàn bộ setup_intent.next_action từ event 13 để biết chính xác field nào chứa URL redirect."""
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
    hosts = ["api.stripe.com"]
    events = []
    for snap in iter_snap():
        u = snap["request"].get("url", "")
        if any(h in u for h in hosts):
            events.append(snap)

    # events 12 (confirm) + 13 (refresh) + 4-11 (updates before confirm)
    # Nhưng theo array `events` (filter chỉ api.stripe.com), index có thể lệch
    # Tìm chính xác: confirm là POST endswith /confirm; refresh là GET
    confirm_snap = None
    refresh_snap = None
    for snap in events:
        u = snap["request"].get("url", "")
        m = snap["request"].get("method", "")
        if u.endswith("/confirm") and m == "POST":
            confirm_snap = snap
        elif m == "GET" and "elements_session_client" in u:
            refresh_snap = snap

    for label, snap in (("CONFIRM", confirm_snap), ("REFRESH", refresh_snap)):
        print(f"\n===== {label} =====", flush=True)
        if snap is None:
            print("  [MISS]", flush=True)
            continue
        body = load_body(snap["response"].get("content", {}))
        try:
            data = json.loads(body)
        except Exception as e:
            print(f"  parse fail: {e}", flush=True)
            continue

        # Print full setup_intent
        si = data.get("setup_intent")
        if si:
            print("--- setup_intent (full) ---", flush=True)
            print(json.dumps(si, indent=2)[:5000], flush=True)
        else:
            print("  no setup_intent", flush=True)

        # Also print payment_intent
        pi = data.get("payment_intent")
        if pi:
            print("\n--- payment_intent (partial) ---", flush=True)
            print(json.dumps(pi, indent=2)[:3000], flush=True)

        # Print consumer state
        for k in ("post_confirm_action", "post_payment_action", "confirmation_secret",
                  "expires_at", "status", "confirm_status"):
            if k in data:
                v = data[k]
                if isinstance(v, (dict, list)):
                    print(f"  {k} = {json.dumps(v)[:400]}", flush=True)
                else:
                    print(f"  {k} = {v}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
