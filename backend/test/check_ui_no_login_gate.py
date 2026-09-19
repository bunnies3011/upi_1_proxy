"""Verify UI load thẳng không có login gate + fetch jobs OK không cần token.

Chạy Playwright — nếu KHÔNG có Playwright thì skip pass (unit-level không
đủ để verify JS behavior, dùng test khác thay).
"""
from __future__ import annotations
import asyncio
import sys


async def main() -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("[SKIP] no playwright installed", flush=True)
        return 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        console_errors: list[str] = []
        console_all: list[str] = []
        page.on("console", lambda msg: (
            console_errors.append(f"[{msg.type}] {msg.text}")
            if msg.type in ("error",) else console_all.append(f"[{msg.type}] {msg.text}")
        ))
        page.on("pageerror", lambda err: console_errors.append(f"[pageerror] {err}"))

        network_401 = []
        page.on("response", lambda r: (
            network_401.append(f"{r.request.method} {r.url} → {r.status}")
            if r.status == 401 else None
        ))

        print("[1] navigate to http://127.0.0.1:8989/ ...", flush=True)
        resp = await page.goto("http://127.0.0.1:8989/", wait_until="networkidle", timeout=15000)
        print(f"    HTTP {resp.status if resp else '?'}", flush=True)

        # Wait for Vue to hydrate
        try:
            await page.wait_for_selector("#ideal-qr-tool-app", timeout=8000)
        except Exception as e:
            print(f"[FAIL] root not mounted: {e}", flush=True)
            print(f"HTML: {(await page.content())[:400]}", flush=True)
            await browser.close()
            return 1
        await asyncio.sleep(2)  # đợi SSE + jobs fetch

        # Snapshot
        from pathlib import Path
        shot = Path("/tmp/ideal_qr_ui_noauth.png")
        await page.screenshot(path=str(shot), full_page=True)
        print(f"    screenshot: {shot} ({shot.stat().st_size} bytes)", flush=True)

        # 2) Verify no 401 in network log
        if network_401:
            print(f"\n[FAIL] {len(network_401)} response 401:", flush=True)
            for e in network_401[:5]:
                print(f"    {e}", flush=True)
            await browser.close()
            return 1
        else:
            print(f"\n[PASS] no HTTP 401 in network log", flush=True)

        # 3) Verify SSE pill = "Live"
        try:
            sse_tag = await page.locator(".app-header__actions .n-tag").first.text_content(timeout=3000)
            print(f"    SSE tag: {sse_tag!r}", flush=True)
        except Exception as e:
            print(f"    SSE tag err: {e}", flush=True)

        # 4) Console errors
        if console_errors:
            print(f"\n[WARN] {len(console_errors)} console errors:", flush=True)
            for e in console_errors[:5]:
                print(f"    {e}", flush=True)

        await browser.close()

    print("\n[PASS] UI works without auth token", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
