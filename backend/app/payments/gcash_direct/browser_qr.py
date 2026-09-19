"""Browser QR extraction for GCash native checkout.

GCash's Adyen redirect link is not the final scannable payment QR. The QR that
actually works is rendered inside a browser session that carries the ChatGPT
account cookies, so this module opens the redirect in Playwright, extracts the
GCash-rendered `#qrcode img` PNG, and keeps the checkout tab alive for the
phone-side scan callback.
"""

from __future__ import annotations

import base64
import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import unquote, urlparse

from app.payments._chatgpt.models import SessionBundle
from app.payments.upi_direct.network_safety import atomic_write_png, validate_and_reencode_png

_CHATGPT_HOME = "https://chatgpt.com/"
_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0"
)
_MIN_QR_SIZE_PX = 160
_ACTIVE_BROWSER_HOLDS: deque[asyncio.Task[None]] = deque()
_GCASH_SCAN_SUCCESS_MARKERS = (
    "online payment linking successful",
)
_CHATGPT_PLUS_SUCCESS_MARKERS = (
    "you are now subscribed to chatgpt plus",
    "successfully subscribed",
)


class GcashBrowserQrError(Exception):
    """Raised when the browser path cannot produce a QR PNG."""


@dataclass(frozen=True)
class GcashBrowserQrResult:
    artifact_path: Path | None
    final_url: str | None


async def capture_gcash_browser_qr(
    *,
    payment_link: str,
    session: SessionBundle,
    job_id: str,
    output_dir: Path,
    proxy_url: str | None,
    timeout_seconds: int,
    headless: bool,
    capture_qr: bool,
    hold_seconds: int,
    hold_max_active: int,
    logger: logging.LoggerAdapter,
    plus_signal_callback: Callable[[str, str], Awaitable[Any]] | None = None,
) -> GcashBrowserQrResult:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise GcashBrowserQrError("playwright_missing") from exc

    started = time.monotonic()
    timeout_ms = max(5, int(timeout_seconds)) * 1000
    proxy = _parse_proxy_for_playwright(proxy_url)

    pw_manager = async_playwright()
    pw = await pw_manager.start()
    browser: Any | None = None
    keepalive_started = False
    try:
        browser = await _launch_chromium(pw, headless=headless, proxy=proxy)
        context = await browser.new_context(
            viewport={"width": 1360, "height": 900},
            screen={"width": 1360, "height": 900},
            device_scale_factor=1,
            is_mobile=False,
            has_touch=False,
            user_agent=_DESKTOP_UA,
            locale="en-PH",
            timezone_id="Asia/Manila",
        )
        await context.add_cookies(_playwright_cookies(session.cookies))
        page = await context.new_page()
        page.set_default_timeout(timeout_ms)
        page.set_default_navigation_timeout(timeout_ms)

        logger.info(
            "gcash_browser begin timeout=%ss proxy=%s",
            max(5, int(timeout_seconds)),
            "on" if proxy else "direct",
        )
        await page.goto(_CHATGPT_HOME, wait_until="domcontentloaded", timeout=timeout_ms)
        session_status = await _read_browser_session_status(page)
        logger.info("gcash_browser session_status=%s", session_status)

        await page.goto(payment_link, wait_until="commit", timeout=timeout_ms)
        final_url = await _wait_for_non_adyen_url(page, timeout_ms=min(timeout_ms, 10000))
        logger.info("gcash_browser opened url=%s", _short_url(final_url))
        if not capture_qr:
            logger.info("gcash_browser_qr skipped capture=false")
            if hold_seconds > 0 and hold_max_active > 0:
                logger.info(
                    "gcash_browser_hold schedule seconds=%s max_active=%s",
                    hold_seconds,
                    hold_max_active,
                )
                _schedule_browser_hold(
                    playwright=pw,
                    browser=browser,
                    context=context,
                    page=page,
                    job_id=job_id,
                    hold_seconds=hold_seconds,
                    max_active=hold_max_active,
                    logger=logger,
                    plus_signal_callback=plus_signal_callback,
                )
                keepalive_started = True
            return GcashBrowserQrResult(artifact_path=None, final_url=final_url)

        try:
            png_bytes = await _wait_and_extract_qr_png(page, timeout_ms=timeout_ms)
        except GcashBrowserQrError as exc:
            logger.warning("gcash_browser_qr failed: %s", exc)
            return GcashBrowserQrResult(artifact_path=None, final_url=final_url)
        reencoded = validate_and_reencode_png(png_bytes)
        artifact = atomic_write_png(output_dir / f"{job_id}.png", reencoded)
        logger.info(
            "gcash_browser_qr ok artifact=%s elapsed=%.1fs",
            artifact,
            time.monotonic() - started,
        )
        if hold_seconds > 0 and hold_max_active > 0:
            logger.info(
                "gcash_browser_hold schedule seconds=%s max_active=%s",
                hold_seconds,
                hold_max_active,
            )
            _schedule_browser_hold(
                playwright=pw,
                browser=browser,
                context=context,
                page=page,
                job_id=job_id,
                hold_seconds=hold_seconds,
                max_active=hold_max_active,
                logger=logger,
                plus_signal_callback=plus_signal_callback,
            )
            keepalive_started = True
        return GcashBrowserQrResult(artifact_path=artifact, final_url=final_url)
    finally:
        if not keepalive_started:
            if browser is not None:
                await browser.close()
            await pw.stop()


async def _launch_chromium(
    pw: Any,
    *,
    headless: bool,
    proxy: dict[str, str] | None,
) -> Any:
    launch_base: dict[str, Any] = {
        "headless": headless,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if proxy:
        launch_base["proxy"] = proxy

    errors: list[str] = []
    for channel in (None, "msedge", "chrome"):
        kwargs = dict(launch_base)
        if channel:
            kwargs["channel"] = channel
        try:
            return await pw.chromium.launch(**kwargs)
        except Exception as exc:  # noqa: BLE001 - try next installed browser.
            errors.append(f"{channel or 'bundled'}:{type(exc).__name__}:{exc}")
    raise GcashBrowserQrError("browser_launch_failed " + " | ".join(errors)[-500:])


def _schedule_browser_hold(
    *,
    playwright: Any,
    browser: Any,
    context: Any,
    page: Any,
    job_id: str,
    hold_seconds: int,
    max_active: int,
    logger: logging.LoggerAdapter,
    plus_signal_callback: Callable[[str, str], Awaitable[Any]] | None,
) -> None:
    while len(_ACTIVE_BROWSER_HOLDS) >= max(1, int(max_active)):
        oldest = _ACTIVE_BROWSER_HOLDS.popleft()
        if not oldest.done():
            logger.info(
                "gcash_browser_hold limit cancel_oldest active=%s max=%s",
                len(_ACTIVE_BROWSER_HOLDS) + 1,
                max_active,
            )
            oldest.cancel()

    task = asyncio.create_task(
        _hold_browser_for_callback(
            playwright=playwright,
            browser=browser,
            context=context,
            page=page,
            job_id=job_id,
            hold_seconds=hold_seconds,
            logger=logger,
            plus_signal_callback=plus_signal_callback,
        ),
        name="gcash-browser-hold",
    )
    _ACTIVE_BROWSER_HOLDS.append(task)

    def _done(done: asyncio.Task[None]) -> None:
        try:
            _ACTIVE_BROWSER_HOLDS.remove(done)
        except ValueError:
            pass
        try:
            done.result()
        except asyncio.CancelledError:
            logger.info("gcash_browser_hold cancelled")
        except Exception as exc:  # noqa: BLE001 - background cleanup must not crash.
            logger.warning("gcash_browser_hold error: %s:%s", type(exc).__name__, exc)

    task.add_done_callback(_done)


async def _hold_browser_for_callback(
    *,
    playwright: Any,
    browser: Any,
    context: Any,
    page: Any,
    job_id: str,
    hold_seconds: int,
    logger: logging.LoggerAdapter,
    plus_signal_callback: Callable[[str, str], Awaitable[Any]] | None,
) -> None:
    hold = max(0, int(hold_seconds))
    logger.info("gcash_browser_hold begin seconds=%s", hold)
    deadline = time.monotonic() + hold
    last_urls: dict[int, str] = {}
    completion_seen_at: float | None = None
    completion_signal_sent = False
    scan_success_logged = False
    try:
        while time.monotonic() < deadline:
            try:
                pages = list(getattr(context, "pages", ())) or [page]
                for browser_page in pages:
                    targets = [browser_page, *list(getattr(browser_page, "frames", ()))]
                    for target in targets:
                        current_url = str(getattr(target, "url", ""))
                        target_key = id(target)
                        if current_url != last_urls.get(target_key):
                            last_urls[target_key] = current_url
                            logger.info(
                                "gcash_browser_hold url=%s", _short_url(current_url)
                            )

                        text = await _page_text(target)
                        if (
                            not scan_success_logged
                            and _has_gcash_scan_success_text(text)
                        ):
                            scan_success_logged = True
                            logger.info(
                                "gcash_browser_hold scan_success url=%s",
                                _short_url(current_url),
                            )

                        if _is_completion_url(current_url, text):
                            if completion_seen_at is None:
                                completion_seen_at = time.monotonic()
                                if (
                                    plus_signal_callback is not None
                                    and not completion_signal_sent
                                ):
                                    completion_signal_sent = True
                                    logger.info(
                                        "gcash_browser_hold completion_signal url=%s",
                                        _short_url(current_url),
                                    )
                                    try:
                                        await plus_signal_callback(job_id, current_url)
                                    except Exception as exc:  # noqa: BLE001
                                        logger.warning(
                                            "gcash_browser_hold completion_signal failed: %s:%s",
                                            type(exc).__name__,
                                            exc,
                                        )
                            elif time.monotonic() - completion_seen_at >= 20:
                                logger.info("gcash_browser_hold completion_seen closing")
                                return

            except Exception as exc:  # noqa: BLE001 - page may close itself.
                logger.info("gcash_browser_hold page_closed type=%s", type(exc).__name__)
                break
            await page.wait_for_timeout(1000)
    finally:
        try:
            await browser.close()
        finally:
            await playwright.stop()
        logger.info("gcash_browser_hold closed")


def _is_completion_url(url: str, text: str = "") -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host != "chatgpt.com":
        return False
    if parsed.path == "/payments/success" or "plus_onboarding" in parsed.fragment:
        return True
    normalized_text = " ".join(text.lower().split())
    return any(marker in normalized_text for marker in _CHATGPT_PLUS_SUCCESS_MARKERS)


def _is_gcash_url(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() == "m.gcash.com"


def _has_gcash_scan_success_text(text: str) -> bool:
    normalized_text = " ".join(text.lower().split())
    return any(marker in normalized_text for marker in _GCASH_SCAN_SUCCESS_MARKERS)


async def _page_text(page: Any) -> str:
    try:
        locator = getattr(page, "locator", None)
        if not callable(locator):
            return ""
        body = locator("body")
        inner_text = getattr(body, "inner_text", None)
        if not callable(inner_text):
            return ""
        return str(await inner_text(timeout=800))
    except Exception:
        return ""


def _playwright_cookies(cookies: dict[str, str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for name, value in cookies.items():
        if not name or not value:
            continue
        if not (
            name.startswith("__Secure-next-auth.session-token")
            or name.startswith("__Host-next-auth.csrf-token")
            or name.startswith("__Secure-next-auth.csrf-token")
            or name in {"oai-did", "oai-sc", "cf_clearance"}
        ):
            continue
        result.append(
            {
                "name": name,
                "value": value,
                "url": _CHATGPT_HOME,
                "httpOnly": name.startswith("__Host-") or name.startswith("__Secure-"),
                "secure": True,
                "sameSite": "Lax",
            }
        )
    return result


def _parse_proxy_for_playwright(proxy_url: str | None) -> dict[str, str] | None:
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    if not parsed.scheme or not parsed.hostname:
        raise GcashBrowserQrError("invalid_browser_proxy")
    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    proxy: dict[str, str] = {"server": server}
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
    if parsed.password:
        proxy["password"] = unquote(parsed.password)
    return proxy


async def _read_browser_session_status(page: Any) -> str:
    try:
        status = await page.evaluate(
            """async () => {
                const r = await fetch('/api/auth/session', { credentials: 'include' });
                if (!r.ok) return `http_${r.status}`;
                const data = await r.json().catch(() => null);
                return data && data.accessToken ? 'ok' : 'no_access_token';
            }"""
        )
    except Exception as exc:  # noqa: BLE001
        return f"unknown:{type(exc).__name__}"
    return status if isinstance(status, str) else "unknown"


async def _wait_and_extract_qr_png(
    page: Any,
    *,
    timeout_ms: int,
) -> bytes:
    deadline = time.monotonic() + timeout_ms / 1000
    last_url = ""
    last_data_uri: str | None = None
    stable_seen_at: float | None = None
    while time.monotonic() < deadline:
        last_url = page.url
        for frame in page.frames:
            try:
                data_uri = await _extract_gcash_qr_data_uri(frame)
            except Exception:  # noqa: BLE001 - cross-origin/detached frames can race.
                continue
            if data_uri:
                if data_uri == last_data_uri:
                    if stable_seen_at is None:
                        stable_seen_at = time.monotonic()
                    elif time.monotonic() - stable_seen_at >= 1.0:
                        return _decode_png_data_uri(data_uri)
                else:
                    last_data_uri = data_uri
                    stable_seen_at = time.monotonic()
        await page.wait_for_timeout(750)
    raise GcashBrowserQrError(f"qr_data_uri_not_found url={_short_url(last_url)}")


async def resolve_gcash_browser_link(
    *,
    payment_link: str,
    session: SessionBundle,
    proxy_url: str | None,
    timeout_seconds: int,
    headless: bool,
    logger: logging.LoggerAdapter,
) -> str:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise GcashBrowserQrError("playwright_missing") from exc

    timeout_ms = max(5, int(timeout_seconds)) * 1000
    proxy = _parse_proxy_for_playwright(proxy_url)

    async with async_playwright() as pw:
        browser = await _launch_chromium(pw, headless=headless, proxy=proxy)
        try:
            context = await browser.new_context(
                viewport={"width": 1360, "height": 900},
                screen={"width": 1360, "height": 900},
                device_scale_factor=1,
                is_mobile=False,
                has_touch=False,
                user_agent=_DESKTOP_UA,
                locale="en-PH",
                timezone_id="Asia/Manila",
            )
            await context.add_cookies(_playwright_cookies(session.cookies))
            page = await context.new_page()
            page.set_default_timeout(timeout_ms)
            page.set_default_navigation_timeout(timeout_ms)

            logger.info(
                "gcash_browser_link begin timeout=%ss proxy=%s",
                max(5, int(timeout_seconds)),
                "on" if proxy else "direct",
            )
            await page.goto(_CHATGPT_HOME, wait_until="domcontentloaded", timeout=timeout_ms)
            session_status = await _read_browser_session_status(page)
            logger.info("gcash_browser_link session_status=%s", session_status)

            await page.goto(payment_link, wait_until="commit", timeout=timeout_ms)
            final_url = await _wait_for_non_adyen_url(page, timeout_ms=timeout_ms)
            logger.info("gcash_browser_link resolved url=%s", _short_url(final_url))
            return final_url
        finally:
            await browser.close()


async def _wait_for_non_adyen_url(page: Any, *, timeout_ms: int) -> str:
    deadline = time.monotonic() + timeout_ms / 1000
    last_url = page.url
    while time.monotonic() < deadline:
        last_url = page.url
        host = urlparse(last_url).hostname or ""
        if host and "checkoutshopper-live.adyen.com" not in host:
            return last_url
        await page.wait_for_timeout(300)
    return last_url


async def _extract_gcash_qr_data_uri(page: Any) -> str | None:
    return await page.evaluate(
        """() => {
            const selectors = [
                '#qrcode img[src^="data:image/png;base64,"]',
                '.qr-container #qrcode img[src^="data:image/png;base64,"]',
                '.qr-section #qrcode img[src^="data:image/png;base64,"]',
                '.instructions-container #qrcode img[src^="data:image/png;base64,"]'
            ];
            for (const selector of selectors) {
                const el = document.querySelector(selector);
                if (!el) continue;
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') continue;
                if (rect.width < 160 || rect.height < 160) continue;
                const ratio = rect.width / Math.max(rect.height, 1);
                if (ratio < 0.72 || ratio > 1.28) continue;
                return el.getAttribute('src');
            }
            return null;
        }"""
    )


def _decode_png_data_uri(data_uri: str) -> bytes:
    prefix = "data:image/png;base64,"
    if not data_uri.startswith(prefix):
        raise GcashBrowserQrError("qr_src_not_png_data_uri")
    try:
        raw = base64.b64decode(data_uri[len(prefix) :], validate=True)
    except ValueError as exc:
        raise GcashBrowserQrError("qr_data_uri_invalid_base64") from exc
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise GcashBrowserQrError("qr_data_uri_not_png")
    return raw


def _short_url(url: str) -> str:
    if len(url) <= 140:
        return url
    return url[:120] + "..." + url[-16:]


__all__ = [
    "GcashBrowserQrError",
    "GcashBrowserQrResult",
    "capture_gcash_browser_qr",
    "resolve_gcash_browser_link",
]
