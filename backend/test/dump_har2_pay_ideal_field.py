"""Tìm CHÍNH XÁC field nào trong response event 13 chứa 'pay.ideal.nl'.

Cũng dump full response body event 12 (confirm) — có thể URL đã có ngay ở confirm.
"""
from __future__ import annotations
import json
import re
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


def find_key_path(obj, target: str, path: list[str] | None = None):
    """Recursively yield paths where obj value == target or contains target."""
    if path is None:
        path = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from find_key_path(v, target, path + [k])
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from find_key_path(v, target, path + [f"[{i}]"])
    else:
        if isinstance(obj, str) and target in obj:
            yield (path, obj)


def main() -> int:
    hosts_interest = ["api.stripe.com", "hooks.stripe.com", "tx.ideal.nl", "pay.ideal.nl"]
    events = []
    for snap in iter_snap():
        u = snap["request"].get("url", "")
        if any(h in u for h in hosts_interest):
            events.append(snap)

    for idx in (12, 13):
        snap = events[idx]
        req = snap["request"]
        resp = snap["response"]
        body = load_body(resp.get("content", {}))
        print(f"\n===== EVENT {idx}: {req['method']} {req['url'][:80]} =====", flush=True)
        try:
            data = json.loads(body)
        except Exception as e:
            print(f"  [FAIL] parse json: {e}", flush=True)
            continue

        # Search "pay.ideal.nl" trong toàn bộ tree
        hits = list(find_key_path(data, "pay.ideal.nl"))
        print(f"  Hits for 'pay.ideal.nl': {len(hits)}", flush=True)
        for path, val in hits[:20]:
            print(f"    .{'.'.join(path)} = {val[:200]}", flush=True)

        # Search "tx.ideal.nl"
        hits2 = list(find_key_path(data, "tx.ideal.nl"))
        print(f"  Hits for 'tx.ideal.nl': {len(hits2)}", flush=True)
        for path, val in hits2[:20]:
            print(f"    .{'.'.join(path)} = {val[:200]}", flush=True)

        # Search "hooks.stripe.com"
        hits3 = list(find_key_path(data, "hooks.stripe.com"))
        print(f"  Hits for 'hooks.stripe.com': {len(hits3)}", flush=True)
        for path, val in hits3[:5]:
            print(f"    .{'.'.join(path)} = {val[:200]}", flush=True)

        # Search "next_action" / "redirect_to_url" / "ideal"
        for kw in ("next_action", "redirect_to_url", "src_"):
            paths_kw = []
            def scan(o, p=[]):
                if isinstance(o, dict):
                    for k, v in o.items():
                        if k == kw:
                            paths_kw.append((p + [k], v))
                        scan(v, p + [k])
                elif isinstance(o, list):
                    for i, v in enumerate(o):
                        scan(v, p + [f"[{i}]"])
            scan(data)
            if paths_kw:
                print(f"  Field '{kw}':", flush=True)
                for path, val in paths_kw[:5]:
                    val_s = json.dumps(val)[:300] if not isinstance(val, str) else val[:300]
                    print(f"    .{'.'.join(path)} = {val_s}", flush=True)

        # Dump top-level state fields interesting
        for k in ("payment_intent", "payment_page", "status", "confirm_status",
                  "checkout_session", "session_state", "post_confirm_action",
                  "post_payment_action", "redirect"):
            if k in data:
                v = data[k]
                if isinstance(v, (dict, list)):
                    v_s = json.dumps(v)[:400]
                else:
                    v_s = str(v)[:200]
                print(f"  top.{k} = {v_s}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
