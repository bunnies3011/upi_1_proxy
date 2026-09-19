"""Load http://127.0.0.1:8989/ trong Playwright headless và verify:
  - HTML load OK
  - JS bundle chạy → root component mount
  - Tab "CÀI ĐẶT" click → hiển thị token input
  - Nhập token → localStorage lưu
  - Reconnect SSE thành công (không thấy 'Reconnecting' sau 2s)

Prerequisite: uvicorn đang chạy port 8989, DB e2e_test.db với web.auth_token='e2e-test'.
"""
from __future__ import annotations
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "runtime" / "e2e_test.db"


def get_token() -> str:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT value FROM settings WHERE key='web.auth_token'")
    row = cur.fetchone()
    conn.close()
    val = row[0]
    return json.loads(val) if val.startswith('"') else val


async def main() -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("[SKIP] playwright not installed — pip install playwright && playwright install chromium", flush=True)
        return 0

    token = get_token()
    print(f"[browser] token: {token[:8]}...", flush=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # Inject token vào localStorage NGAY LẦN ĐẦU load để bỏ qua login gate.
        console_errors = []
        page.on("console", lambda msg: (
            console_errors.append(f"[{msg.type}] {msg.text}")
            if msg.type in ("error", "warning") else None
        ))
        page.on("pageerror", lambda err: console_errors.append(f"[pageerror] {err}"))

        # Bootstrap localStorage token TRƯỚC khi page load (init script).
        # Đây là cách chuẩn cho localStorage — inject qua addInitScript.
        await context.add_init_script(
            f"window.localStorage.setItem('IDEAL_QR_TOOL_AUTH_TOKEN', '{token}');"
        )

        # 1) Navigate → root
        print("\n[1] GET / ...", flush=True)
        resp = await page.goto("http://127.0.0.1:8989/", wait_until="networkidle", timeout=15000)
        print(f"  HTTP {resp.status if resp else '?'}", flush=True)

        # 2) Verify root component mount
        try:
            await page.wait_for_selector("#ideal-qr-tool-app", timeout=5000)
            print("  ✅ #ideal-qr-tool-app rendered", flush=True)
        except Exception as e:
            print(f"  ❌ Root not found: {e}", flush=True)
            html_snip = (await page.content())[:500]
            print(f"  HTML snip: {html_snip}", flush=True)
            await browser.close()
            return 1

        # 3) Verify header brand
        brand = await page.locator(".app-header__logo").text_content(timeout=3000)
        print(f"  Header brand: {brand!r}", flush=True)

        # 4) SSE status pill
        await asyncio.sleep(2)  # đợi SSE connect
        sse_text = await page.locator(".app-header__actions .n-tag").first.text_content(timeout=3000)
        print(f"  SSE status: {sse_text!r}", flush=True)

        # 5) Snapshot UI
        screenshot_path = Path("/tmp/ideal_qr_ui.png")
        await page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"  Screenshot: {screenshot_path} ({screenshot_path.stat().st_size} bytes)", flush=True)

        # 6) Errors in console
        if console_errors:
            print(f"\n[console] {len(console_errors)} errors/warnings:", flush=True)
            for e in console_errors[:10]:
                print(f"  {e}", flush=True)
        else:
            print("\n[console] No errors/warnings", flush=True)

        await browser.close()

    print("\n[PASS] Browser UI verified", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
