"""Truy cập URL pay.ideal.nl user gửi, POST /initiate, tìm QR content nguồn.

Strategy:
1. GET HTML page → tìm main.js chunk (JS bundle chứa logic render QR)
2. Fetch main.js → grep pattern qrcode/canvas với endpoint
3. Nếu không thấy → decode QR trong ảnh user gửi (không có ảnh gốc, phải suy luận)

Fallback: nếu QR content = URL `tx.ideal.nl/2/{tx_id}?sig=...` → verify bằng
cách gen QR từ URL đó và check kích thước phù hợp với ảnh (1000x1000 canvas
với ~20 module rows/cols)
"""

from __future__ import annotations
import asyncio
import re
import sys
from pathlib import Path
from urllib.parse import unquote

import httpx


URL = (
    "https://pay.ideal.nl/transactions/"
    "https%3A%2F%2Ftx.ideal.nl%2F2%2FAPR6Z4ZZQQUYLYAARZ6PDZFPU44"
    "?sig=CGBCQEIIA75FEJ7WTU5OLTT3AEKXVIDVK62Y2DYGILOIFHFR6DLOLGPIWOYOQEIB7HNNK6RNBMMTHMVEW4YCCE6SPATLV2LNDAP7NGDNDIDQWWCMTFE"
)


async def main() -> int:
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=30.0,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/141.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
        },
    ) as client:
        # 1) GET page
        print("=== [1] GET pay.ideal.nl/transactions/... ===", flush=True)
        r = await client.get(URL)
        print(f"  status: {r.status_code}", flush=True)
        print(f"  set-cookie: {r.headers.get('set-cookie', '-')[:100]}...", flush=True)
        html = r.text
        print(f"  html size: {len(html)}", flush=True)

        # Extract main.js chunk hash
        main_js_match = re.search(r'/static/js/(main\.[a-f0-9]+\.js)', html)
        system_js_match = re.search(r'/static/js/(system\.[a-f0-9]+\.js)', html)
        print(f"  main.js: {main_js_match.group(1) if main_js_match else '-'}", flush=True)
        print(f"  system.js: {system_js_match.group(1) if system_js_match else '-'}", flush=True)

        # 2) POST /api/v1/transactions/{encoded}/initiate
        print("\n=== [2] POST /api/v1/transactions/{encoded}/initiate ===", flush=True)
        # Extract encoded_tx from URL path
        path_match = re.search(r"/transactions/([^?]+)", URL)
        encoded_tx = path_match.group(1)
        initiate_url = f"https://pay.ideal.nl/api/v1/transactions/{encoded_tx}/initiate"
        r2 = await client.post(
            initiate_url,
            json={
                "deviceInfo": {
                    "language": "en-US",
                    "timeZone": "Asia/Ho_Chi_Minh",
                    "screenWidth": 1440,
                    "screenHeight": 800,
                    "screenAvailableWidth": 1440,
                    "screenAvailableHeight": 885,
                    "colorDepth": 30,
                },
                "httpReferrer": "https://chatgpt.com/",
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Origin": "https://pay.ideal.nl",
                "Referer": URL,
            },
        )
        print(f"  status: {r2.status_code}", flush=True)
        print(f"  content-type: {r2.headers.get('content-type', '-')}", flush=True)
        if 200 <= r2.status_code < 300:
            try:
                data = r2.json()
                print(f"  keys: {list(data.keys())}", flush=True)
                for k in ("qr", "qrCode", "qrPayload", "qrContent", "wero", "amount",
                          "creditorName", "view", "reference", "expiresAt", "expiresIn",
                          "sessionId"):
                    if k in data:
                        v = data[k]
                        v_str = str(v)[:200]
                        print(f"  {k} = {v_str}", flush=True)
            except Exception as e:
                print(f"  Failed to parse json: {e}", flush=True)
                print(f"  body[:500]: {r2.text[:500]}", flush=True)
        else:
            print(f"  body[:300]: {r2.text[:300]}", flush=True)

        # 3) Look for /qr endpoint on pay.ideal.nl
        print("\n=== [3] Try /api/v1/transactions/{encoded}/qr (guess) ===", flush=True)
        qr_url = f"https://pay.ideal.nl/api/v1/transactions/{encoded_tx}/qr"
        try:
            r3 = await client.get(
                qr_url,
                headers={"Accept": "*/*", "Referer": URL},
            )
            print(f"  {qr_url[:100]}...", flush=True)
            print(f"  status: {r3.status_code} content-type: {r3.headers.get('content-type', '-')}", flush=True)
            if 200 <= r3.status_code < 300 and len(r3.content) < 100000:
                print(f"  body[:300]: {r3.text[:300] if r3.headers.get('content-type', '').startswith('text') else '(binary)'}", flush=True)
        except Exception as e:
            print(f"  exc: {e}", flush=True)

        # 4) Fetch main.js and grep for QR pattern
        if main_js_match:
            print(f"\n=== [4] Fetch main.js chunk ===", flush=True)
            js_url = f"https://pay.ideal.nl/static/js/{main_js_match.group(1)}"
            r4 = await client.get(js_url)
            js = r4.text
            print(f"  {js_url}", flush=True)
            print(f"  size: {len(js)}", flush=True)
            # Grep for qr-related snippets
            patterns = [
                r"qr[A-Z][a-zA-Z]*",   # qrCode, qrPayload, qrContent, qrData
                r"[qQ]rCode",
                r"payloadUri",
                r"tx\\?\.ideal\\?\.nl",
                r"canvas",
                r"[Ww]ero",
                r"createQR|generateQR|renderQR",
                r"qrcode.js|qrcodejs|qr\\.min\\.js",
            ]
            for p in patterns:
                matches = re.findall(p, js)
                if matches:
                    uniq = list(set(matches))[:10]
                    print(f"  pattern {p}: {len(matches)} hits, unique: {uniq}", flush=True)
            # Search context around 'qrCode' or 'qrPayload'
            for kw in ("qrCode", "qrPayload", "createElement(\"canvas\")",
                       "'canvas'", "qr-code-image", "tx.ideal.nl"):
                idx = js.find(kw)
                if idx >= 0:
                    lo = max(0, idx - 100)
                    hi = min(len(js), idx + 300)
                    print(f"  ~ {kw!r} @{idx}: ...{js[lo:hi]}...", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
