"""Dump full response body của Stripe confirm để phân tích shape.

Mục đích: xem confirm response có `next_action`/`redirect_to_url` không, và
`setup_intent`/`payment_intent` nested như nào — quyết định flow tiếp theo.

Chạy: 1 account thật → login → checkout → init → elements → confirm →
DUMP payload confirm ra `/tmp/ideal_confirm_dump.json`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.bootstrap import bootstrap_services  # noqa: E402
from app.payments.ideal.chatgpt_client import ChatgptClient  # noqa: E402
from app.payments.ideal.profile_generator import IdealProfileGenerator  # noqa: E402
from app.payments.ideal.stripe_client import (  # noqa: E402
    StripeClient,
    _flatten_form,
    _encode_form_urlencoded,
    _BIC_TO_STRIPE_BANK,
    _DEFAULT_STRIPE_BANK,
    _STRIPE_API_BASE,
    _STRIPE_VERSION,
    _STRIPE_JS_ORIGIN,
    _BROWSER_LOCALE_NL,
    _BROWSER_TIMEZONE_NL,
    _PAYMENT_METHOD_TYPE_IDEAL,
)


# Đọc file account đầu (1 dòng)
ACCOUNT_LINE = (ROOT / "runtime" / "accounts_smoke.txt").read_text().strip().splitlines()[0]
EMAIL, PASSWORD, TOTP = ACCOUNT_LINE.split("|")

DUMP_PATH = Path("/tmp/ideal_confirm_dump.json")


class _SimpleLoggerAdapter:
    def __init__(self, logger, secrets):
        self.logger = logger
        self.secrets = secrets

    def info(self, event, /, **kwargs):
        self.logger.info(f"[stripe] {event} {kwargs}")


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    services = await bootstrap_services(
        db_path=ROOT / "runtime" / "e2e_test.db",
        bind_host="127.0.0.1",
        session_cache_dir=ROOT / "runtime" / "session_cache",
        qr_output_dir=ROOT / "runtime" / "qr",
        validate_startup=False,
    )
    logger = logging.getLogger("dump_confirm")

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,nl;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        }
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=30.0, headers=headers
        ) as client:
            chatgpt = ChatgptClient(
                http_client=client,
                session_cache=services.session_cache,
                logger=logger,
            )
            print("[step] login...", flush=True)
            session = await chatgpt.login(EMAIL, PASSWORD, TOTP)
            print(f"[step] login ok, access_token_len={len(session.access_token)}", flush=True)

            print("[step] create_checkout...", flush=True)
            checkout = await chatgpt.create_checkout(session)
            print(
                f"[step] checkout ok: session_id={checkout.checkout_session_id}",
                flush=True,
            )

            stripe = StripeClient(client, services.settings, _SimpleLoggerAdapter(logger, []))
            print("[step] stripe init...", flush=True)
            init = await stripe.init(checkout.checkout_session_id, checkout.publishable_key)
            print(f"[step] init ok, amount={init.amount}", flush=True)

            print("[step] elements_sessions...", flush=True)
            elements = await stripe.elements_sessions(
                checkout.checkout_session_id,
                checkout.publishable_key,
                amount=init.amount,
            )
            print(f"[step] elements ok, session_id={elements.session_id}", flush=True)

            print("[step] generate billing profile...", flush=True)
            billing = IdealProfileGenerator().generate(EMAIL)

            print("[step] confirm...", flush=True)
            confirm_result = await stripe.confirm(
                checkout.checkout_session_id,
                checkout.publishable_key,
                elements.session_id,
                billing,
                init_checksum=init.init_checksum,
                amount=init.amount,
                elements_config_id=elements.config_id,
                init_config_id=init.config_id,
                default_issuer_bic="RABONL2U",
            )
            print(f"[step] confirm ok, top-level keys={sorted(confirm_result.keys())[:20]}...", flush=True)

            # DUMP toàn bộ confirm response
            DUMP_PATH.write_text(json.dumps(confirm_result, indent=2, ensure_ascii=False, default=str))
            print(f"[dump] confirm payload → {DUMP_PATH} ({DUMP_PATH.stat().st_size} bytes)", flush=True)

            # Xem 1 số field quan trọng
            print("\n[analysis]", flush=True)
            for key in [
                "status", "payment_status", "mode", "capture_method",
                "setup_future_usage", "setup_intent", "payment_intent",
                "redirect_on_completion", "return_url", "stripe_hosted_url", "url",
                "submit_type", "ui_mode",
            ]:
                val = confirm_result.get(key)
                if isinstance(val, (dict, list)):
                    val = f"<{type(val).__name__}> keys={list(val.keys()) if isinstance(val, dict) else len(val)}"
                elif isinstance(val, str) and len(val) > 100:
                    val = val[:100] + "..."
                print(f"  {key} = {val!r}", flush=True)

            # Search for redirect URL trong bất cứ đâu
            print("\n[search redirect_to_url in payload]", flush=True)
            def _walk(obj, path=""):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        new_path = f"{path}.{k}" if path else k
                        if "redirect" in k.lower() or "next_action" in k.lower():
                            print(f"  {new_path} = {str(v)[:200]!r}", flush=True)
                        _walk(v, new_path)
                elif isinstance(obj, list):
                    for i, item in enumerate(obj[:3]):
                        _walk(item, f"{path}[{i}]")
            _walk(confirm_result)
    finally:
        await services.db_engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
