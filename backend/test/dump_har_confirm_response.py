"""Dump full response HAR của POST /confirm — hiểu Stripe trả gì để có
next_action/redirect_to_url. Compare với response code hiện tại đang lấy.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HAR_TRACE_DIR = Path("/tmp/har_trace")
RESOURCES = HAR_TRACE_DIR / "resources"
NETWORK = HAR_TRACE_DIR / "trace.network"


def main() -> int:
    if not NETWORK.exists():
        print(f"[FATAL] {NETWORK} không tồn tại — cần unzip trace.zip trước", flush=True)
        return 1

    # Tìm tất cả request/response confirm + payment_pages GET/POST
    print("=== INTERESTING REQ/RESP ===", flush=True)
    idx = 0
    with NETWORK.open() as f:
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
            if not url:
                continue
            method = req.get("method", "?")
            if not (
                "payment_pages/" in url
                or "/checkout/approve" in url
                or "pay.ideal.nl/api/v1/transactions" in url
            ):
                continue

            resp = snap.get("response", {})
            status = resp.get("status")
            content = resp.get("content", {})
            sha1 = content.get("_sha1", "").replace(".json", "").replace(".dat", "")
            mime = content.get("mimeType", "")

            idx += 1
            path_short = url.split("?")[0]
            if "cs_live_" in path_short:
                parts = path_short.split("cs_live_")
                if len(parts) == 2:
                    tail = parts[1].split("/", 1)
                    path_short = parts[0] + "cs_live_<ID>" + (
                        "/" + tail[1] if len(tail) > 1 else ""
                    )
            print(
                f"\n[{idx}] {method} {path_short} → {status} ({content.get('size', 0)}b {mime})",
                flush=True,
            )
            print(f"    resp_sha1: {sha1}", flush=True)

            # Load response body
            for suffix in (".json", ".dat", ""):
                path = RESOURCES / f"{sha1}{suffix}"
                if path.exists():
                    try:
                        text = path.read_text(encoding="utf-8", errors="ignore")
                    except Exception:
                        continue
                    if "/confirm" in url or (
                        method == "GET" and "payment_pages/" in url and "?" in url
                    ):
                        # Focus: confirm + refresh_poll response — cần intent
                        try:
                            body = json.loads(text)
                        except json.JSONDecodeError:
                            print(f"    [BODY] non-json ({len(text)}b): {text[:200]}", flush=True)
                            break
                        _print_intent_analysis(body, indent="    ")
                    break
    return 0


def _print_intent_analysis(body: dict, *, indent: str = "  ") -> None:
    """Print key fields liên quan intent + redirect."""
    if not isinstance(body, dict):
        print(f"{indent}[not dict]", flush=True)
        return
    for key in [
        "id", "status", "payment_status", "mode",
        "setup_intent", "payment_intent",
        "return_url", "stripe_hosted_url", "url",
        "ui_mode", "config_id", "init_checksum",
    ]:
        val = body.get(key)
        if isinstance(val, dict):
            keys = sorted(val.keys())
            # Nếu có next_action, print chi tiết
            na = val.get("next_action")
            if na:
                na_type = na.get("type") if isinstance(na, dict) else None
                redirect = na.get("redirect_to_url") if isinstance(na, dict) else None
                redirect_url = (
                    redirect.get("url") if isinstance(redirect, dict) else None
                )
                print(f"{indent}{key}: <dict> next_action.type={na_type!r}", flush=True)
                if redirect_url:
                    print(f"{indent}  next_action.redirect_to_url.url = {redirect_url!r}", flush=True)
            else:
                s = str(val)[:100]
                print(f"{indent}{key}: <dict, {len(keys)} keys>: {keys[:15]}", flush=True)
        elif isinstance(val, str):
            s = val if len(val) < 100 else val[:97] + "..."
            print(f"{indent}{key} = {s!r}", flush=True)
        elif val is not None:
            print(f"{indent}{key} = {val!r}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
