"""Diff body confirm HAR event 12 vs body confirm flow của mình.

So sánh field-by-field để tìm field thiếu / khác giá trị.
"""
from __future__ import annotations
import json
from pathlib import Path
from urllib.parse import parse_qs

HAR_TRACE = Path("/tmp/har_trace2/trace.network")
HAR_RES = Path("/tmp/har_trace2/resources")
MINE_DIR = Path("/tmp/ideal_confirm_dump")


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
            p = HAR_RES / f"{sha}{ext}"
            if p.exists():
                return p.read_text(errors="replace")
    return ""


def iter_snap():
    with HAR_TRACE.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") == "resource-snapshot":
                yield rec["snapshot"]


def get_har_confirm_body():
    for snap in iter_snap():
        req = snap["request"]
        url = req.get("url", "")
        m = req.get("method", "")
        if m == "POST" and url.endswith("/confirm") and "payment_pages" in url:
            return load_post(req.get("postData", {}))
    return ""


def parse_flat(body: str) -> dict:
    parsed = parse_qs(body, keep_blank_values=True)
    # Convert list values to str (first element)
    return {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}


def main() -> int:
    har = get_har_confirm_body()
    mine_files = sorted(MINE_DIR.glob("confirm_body_*.txt"))
    if not har:
        print("[FAIL] HAR confirm body not found", flush=True)
        return 1
    if not mine_files:
        print("[FAIL] Mine confirm body not found", flush=True)
        return 1

    mine = mine_files[0].read_text()
    print(f"[HAR]  {len(har)} chars", flush=True)
    print(f"[MINE] {len(mine)} chars ({mine_files[0].name})", flush=True)

    har_p = parse_flat(har)
    mine_p = parse_flat(mine)

    har_keys = set(har_p.keys())
    mine_keys = set(mine_p.keys())

    print(f"\n=== HAR field count: {len(har_keys)}", flush=True)
    print(f"=== MINE field count: {len(mine_keys)}", flush=True)

    only_har = sorted(har_keys - mine_keys)
    only_mine = sorted(mine_keys - har_keys)
    both = har_keys & mine_keys

    print(f"\n=== Fields ONLY in HAR ({len(only_har)}) ===", flush=True)
    for k in only_har:
        v = har_p[k]
        v_s = (v if isinstance(v, str) else str(v))[:150]
        print(f"  {k} = {v_s!r}", flush=True)

    print(f"\n=== Fields ONLY in MINE ({len(only_mine)}) ===", flush=True)
    for k in only_mine:
        v = mine_p[k]
        v_s = (v if isinstance(v, str) else str(v))[:150]
        print(f"  {k} = {v_s!r}", flush=True)

    # Fields in both but different values
    print(f"\n=== Fields in BOTH but DIFFERENT values ===", flush=True)
    diff_count = 0
    for k in sorted(both):
        vh = har_p[k]
        vm = mine_p[k]
        if vh != vm:
            vh_s = (vh if isinstance(vh, str) else str(vh))[:100]
            vm_s = (vm if isinstance(vm, str) else str(vm))[:100]
            print(f"  {k}", flush=True)
            print(f"    HAR:  {vh_s!r}", flush=True)
            print(f"    MINE: {vm_s!r}", flush=True)
            diff_count += 1
    print(f"\n  Total diff: {diff_count}", flush=True)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
