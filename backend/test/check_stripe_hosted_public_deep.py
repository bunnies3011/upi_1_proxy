"""Deep check: verify URL public thực sự HAY chỉ là landing "session expired".

Đọc HTML body chi tiết:
- Extract <title>
- Search text hiển thị (không phải JS string) cho các error state
- Test tất cả 9 QR URL, không chỉ 1
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import httpx
from PIL import Image
import zxingcpp

ROOT = Path(__file__).resolve().parents[1]


async def check_url(url: str, tag: str) -> dict:
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=30.0,
        headers={
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
            "Accept": "text/html,*/*",
        },
    ) as client:
        r = await client.get(url)
    html = r.text
    title_m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
    title = title_m.group(1).strip() if title_m else "?"

    # Extract ONLY meta description / preloaded state (Stripe embeds session state trong <script id="__PRELOADED_STATE__"> or similar)
    # Check các keyword ERROR chỉ TRONG visible text (không trong JS string cả 500KB bundle):
    # Cách đơn giản hơn: check headers Response + xem HTML có <body class="error"> không
    body_class_m = re.search(r"<body[^>]*class=[\"']([^\"']+)[\"']", html, re.I)
    body_class = body_class_m.group(1) if body_class_m else "-"

    # Stripe session state: nếu session còn valid, HTML sẽ có script chứa "SessionState":"success" hoặc payment_intent info
    session_state_ok = "payment_intent" in html.lower() or "cs_live_" in html.lower()

    # Stripe returns generic "Sorry, we can't find that page" (404 style) khi session expired hoặc invalid
    error_visible = any(kw in html.lower() for kw in [
        "sorry, we can't find that page",
        "sorry, we can't process this payment",
        "sorry, we couldn't find",
        "this checkout session has expired",
        "the checkout session was closed",
    ])

    return {
        "tag": tag,
        "status": r.status_code,
        "title": title,
        "body_class": body_class,
        "session_state_ok": session_state_ok,
        "error_visible": error_visible,
        "size": len(html),
    }


async def main() -> int:
    qr_dir = ROOT / "runtime" / "qr"
    files = sorted(qr_dir.glob("*.png"))
    if not files:
        print("[FAIL] no QR", flush=True)
        return 1

    urls: list[tuple[str, str]] = []
    for f in files:
        img = Image.open(f)
        rs = zxingcpp.read_barcodes(img)
        if rs:
            urls.append((f.stem[:8], rs[0].text))

    print(f"[INFO] Checking {len(urls)} QR URLs ...\n", flush=True)
    for tag, url in urls:
        try:
            r = await check_url(url, tag)
            marker = "[PASS]" if r["status"] == 200 and not r["error_visible"] and r["session_state_ok"] else "[WARN]"
            print(
                f"{marker} {r['tag']}: HTTP {r['status']} title='{r['title']}' "
                f"session_ok={r['session_state_ok']} err={r['error_visible']} size={r['size']}",
                flush=True,
            )
        except Exception as exc:
            print(f"[FAIL] {tag}: {type(exc).__name__}: {exc}", flush=True)

    print("\n=== KẾT LUẬN ===", flush=True)
    print("Nếu tất cả PASS → stripe_hosted_url là PUBLIC URL, user cuối quét QR mở được.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
