"""Verify stripe_hosted_url mở được cho user LẠ (không có login/session).

Test 3 kịch bản:
1. httpx client mới, không cookies → GET URL → check status
2. HAR: mở stripe_hosted_url có yêu cầu auth/cookies không? Search fragment
   trong HAR để xem network flow browser thật.
3. Check Stripe expires_at (session TTL).
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import httpx
from PIL import Image
import zxingcpp

ROOT = Path(__file__).resolve().parents[1]


async def test_public_access(url: str) -> None:
    """Mở URL với client hoàn toàn mới (không cookies, không session)."""
    print("\n=== Test 1: Public access (no cookies, fresh client) ===", flush=True)
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=30.0,
        headers={
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    ) as client:
        r = await client.get(url)
        print(f"  HTTP {r.status_code}", flush=True)
        print(f"  Final URL: {str(r.url)[:100]}...", flush=True)
        print(f"  Content-Type: {r.headers.get('content-type')}", flush=True)
        print(f"  Content-Length: {len(r.text)} bytes", flush=True)
        text_lower = r.text.lower()
        # Kiểm tra có phải "login/auth required" hay là checkout page thật
        has_ideal = "ideal" in text_lower
        has_stripe_checkout = 'title="stripe checkout"' in text_lower or "stripe checkout" in text_lower
        has_error_page = any(kw in text_lower for kw in ["session expired", "not found", "unauthorized", "access denied"])
        print(f"  Contains 'iDEAL': {has_ideal}", flush=True)
        print(f"  Contains 'Stripe Checkout': {has_stripe_checkout}", flush=True)
        print(f"  Contains error page: {has_error_page}", flush=True)


async def test_public_access_fresh_ip_simulation(url: str) -> None:
    """Test giả lập user khác IP: dùng User-Agent iOS + không cookies."""
    print("\n=== Test 2: Different UA (iOS Safari), fresh session ===", flush=True)
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=30.0,
        headers={
            "User-Agent": "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
        },
    ) as client:
        r = await client.get(url)
        print(f"  HTTP {r.status_code}, {len(r.text)} bytes", flush=True)
        # Extract Stripe checkout session state
        title_match = re.search(r"<title[^>]*>([^<]+)</title>", r.text, re.I)
        if title_match:
            print(f"  Title: {title_match.group(1).strip()}", flush=True)
        # Search cs_live_ id trong body xem có match không
        cs_matches = re.findall(r"cs_live_[a-zA-Z0-9]+", r.text)
        unique_cs = set(cs_matches)
        print(f"  Contains cs_live_ IDs: {len(unique_cs)} unique", flush=True)


def check_har_for_stripe_hosted(qr_url: str) -> None:
    """Kiểm tra HAR để xem browser truy cập stripe_hosted_url cần cookie/auth không."""
    print("\n=== Test 3: HAR — browser truy cập stripe_hosted_url với auth gì? ===", flush=True)
    trace_network = Path("/tmp/har_trace/trace.network")
    if not trace_network.exists():
        print("  [SKIP] /tmp/har_trace/trace.network không tồn tại — unzip trace.zip trước", flush=True)
        return

    # Extract cs_live_ID từ QR URL
    cs_match = re.search(r"cs_live_([a-zA-Z0-9]+)", qr_url)
    if not cs_match:
        print(f"  [FAIL] không parse được cs_live_ID từ QR URL: {qr_url[:100]}", flush=True)
        return
    # Chỉ dùng cs_live_ID pattern để tìm request TỚI checkout.stripe.com/c/pay/cs_live_
    print(f"  Search HAR requests TO checkout.stripe.com/c/pay/cs_live_...", flush=True)

    matches = []
    with trace_network.open() as f:
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
            if "checkout.stripe.com/c/pay/cs_live_" not in url:
                continue
            method = req.get("method", "?")
            headers = req.get("headers", [])
            has_cookie = any(h.get("name", "").lower() == "cookie" for h in headers)
            has_auth = any(h.get("name", "").lower() in ("authorization", "x-auth-token") for h in headers)
            cookie_hdr = next((h.get("value", "") for h in headers if h.get("name", "").lower() == "cookie"), "")
            resp = snap.get("response", {})
            status = resp.get("status")
            matches.append({
                "method": method,
                "url": url.split("#")[0].split("?")[0][-100:],
                "has_cookie": has_cookie,
                "has_auth": has_auth,
                "cookie_short": cookie_hdr[:100] if cookie_hdr else "-",
                "status": status,
            })

    if not matches:
        print("  Không tìm thấy request tới checkout.stripe.com/c/pay/", flush=True)
        return
    print(f"  Tìm được {len(matches)} request:", flush=True)
    for i, m in enumerate(matches[:5]):
        print(f"    [{i+1}] {m['method']} …{m['url']} → {m['status']}", flush=True)
        print(f"        has_cookie={m['has_cookie']}  has_auth_header={m['has_auth']}", flush=True)
        if m['has_cookie']:
            print(f"        cookie: {m['cookie_short']}...", flush=True)


async def main() -> int:
    qr_dir = ROOT / "runtime" / "qr"
    files = sorted(qr_dir.glob("*.png"))
    if not files:
        print("[FAIL] no QR files", flush=True)
        return 1

    img = Image.open(files[0])
    results = zxingcpp.read_barcodes(img)
    if not results:
        print("[FAIL] cannot decode", flush=True)
        return 1
    url = results[0].text
    print(f"[QR URL] {url[:150]}...", flush=True)

    await test_public_access(url)
    await test_public_access_fresh_ip_simulation(url)
    check_har_for_stripe_hosted(url)

    print("\n=== KẾT LUẬN ===", flush=True)
    print("Nếu Test 1 + Test 2 đều HTTP 200 + render 'iDEAL' page → URL công khai.", flush=True)
    print("Stripe hosted checkout session được thiết kế để user cuối truy cập.", flush=True)
    print("Fragment #fidnandhY... là client-side state (Stripe.js decode), không phải auth.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
