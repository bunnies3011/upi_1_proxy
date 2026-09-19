"""R2.7: Backend_Service KHÔNG gọi `POST /v1/consumers/sessions/lookup`.

Endpoint thuộc Stripe Link (consumer sign-in) — không cần cho luồng iDEAL.
Two-tier defense-in-depth:

Tầng 1 (compile-time): grep string `/v1/consumers/sessions/lookup` trong
mọi source `payments/ideal/*.py` — không xuất hiện.

Tầng 2 (runtime): chạy đầy đủ 5 method public của `StripeClient` (init,
elements_sessions, confirm, refresh_poll, follow_redirect) qua
`FakeAsyncSession` catch-all pattern, record mọi URL, assert không có URL
nào chứa lookup path.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from app.payments.ideal.models import BillingAddress
from app.payments.ideal.stripe_client import StripeClient
from tests.support.fake_http import FakeAsyncSession, FakeResponse

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _BACKEND_ROOT / "app"

_SOURCE_FILES_TO_SCAN: tuple[Path, ...] = (
    _APP_ROOT / "payments" / "ideal" / "stripe_client.py",
    _APP_ROOT / "payments" / "ideal" / "chatgpt_client.py",
    _APP_ROOT / "payments" / "ideal" / "transaction_client.py",
    _APP_ROOT / "payments" / "ideal" / "flow.py",
    _APP_ROOT / "payments" / "ideal" / "__init__.py",
)

_STRIPE_LOOKUP_PATH: str = "/v1/consumers/sessions/lookup"


def test_no_source_file_contains_stripe_lookup_path() -> None:
    """Grep source: `/v1/consumers/sessions/lookup` không xuất hiện.

    **Validates: Requirements 2.7**
    """
    violations: list[tuple[Path, int, str]] = []
    for source_path in _SOURCE_FILES_TO_SCAN:
        assert source_path.exists()
        with source_path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                if _STRIPE_LOOKUP_PATH in line:
                    violations.append((source_path, line_no, line.rstrip()))

    assert not violations, (
        "R2.7 cấm Backend_Service gọi 'POST api.stripe.com/v1/consumers/"
        "sessions/lookup'. Phát hiện chuỗi này trong source:\n"
        + "\n".join(
            f"  {path.name}:{line_no}: {content!r}"
            for path, line_no, content in violations
        )
    )


class _FakeSettings:
    """Trả `None` cho mọi key → StripeClient fallback default."""

    async def get(self, key: str) -> Any | None:  # noqa: ARG002
        return None


def _make_billing() -> BillingAddress:
    return BillingAddress(
        name="Jan Test",
        email="jan@example.com",
        address={
            "country": "NL",
            "line1": "Teststraat 1",
            "line2": "",
            "city": "Amsterdam",
            "postal_code": "1000 AA",
            "state": "",
        },
    )


def _build_happy_path_session() -> FakeAsyncSession:
    """FakeAsyncSession với catch-all routes cho Stripe happy-path.

    KHÔNG đăng ký route cho `/v1/consumers/sessions/lookup` — nếu code
    lỡ gọi, session raise AssertionError → test fail rõ ràng (mạnh hơn
    check string trong `.urls_called()`).
    """
    s = FakeAsyncSession()
    cs_id = "cs_test"
    isolated_id = "cs_isolated"
    for pk in (cs_id, isolated_id):
        # POST init
        s.route(
            "POST",
            f"https://api.stripe.com/v1/payment_pages/{pk}/init",
            FakeResponse(200, json_body={"init_checksum": "chk_ok", "config_id": "cfg_ok"}),
        )
        # POST confirm
        s.route(
            "POST",
            f"https://api.stripe.com/v1/payment_pages/{pk}/confirm",
            FakeResponse(
                200,
                json_body={
                    "payment_intent": None,
                    "setup_intent": {"id": "seti_test"},
                    "status": "requires_action",
                },
            ),
        )
        # POST update_billing (bare id)
        s.route(
            "POST",
            f"https://api.stripe.com/v1/payment_pages/{pk}",
            FakeResponse(200, json_body={}),
        )
        # GET refresh_poll
        s.route(
            "GET",
            f"https://api.stripe.com/v1/payment_pages/{pk}",
            FakeResponse(
                200,
                json_body={
                    "setup_intent": {
                        "next_action": {
                            "redirect_to_url": {
                                "url": "https://pm-redirects.stripe.com/authorize/fake"
                            }
                        }
                    }
                },
            ),
        )
    # elements/sessions
    s.route(
        "GET",
        "https://api.stripe.com/v1/elements/sessions",
        FakeResponse(200, json_body={"session_id": "es_test_ok"}),
    )
    # follow_redirect → 302 with Location (stripe_client parses Location header)
    s.route(
        "GET",
        "https://pm-redirects.stripe.com/authorize/fake",
        FakeResponse(
            200,
            headers={
                "location": (
                    "https://pay.ideal.nl/transactions/"
                    "encoded_tx_url_test?sig=sig_test"
                )
            },
            url="https://pay.ideal.nl/transactions/encoded_tx_url_test?sig=sig_test",
        ),
    )
    return s


def test_stripe_client_full_happy_path_never_calls_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5 method public → 0 URL nào chứa `/v1/consumers/sessions/lookup`.

    **Validates: Requirements 2.7**
    """
    fake_session = _build_happy_path_session()
    monkeypatch.setattr(
        "app.core.http_client.create_async_client",
        lambda **kw: fake_session,
    )

    async def _run_full_flow() -> None:
        client = StripeClient(
            http_client=fake_session,
            settings=_FakeSettings(),  # type: ignore[arg-type]
            logger=logging.getLogger("test.stripe_no_lookup"),
        )
        await client.init(
            checkout_session_id="cs_test",
            publishable_key="pk_test",
        )
        await client.elements_sessions(
            checkout_session_id="cs_test",
            publishable_key="pk_test",
        )
        await client.confirm(
            checkout_session_id="cs_test",
            publishable_key="pk_test",
            elements_session_id="es_test_ok",
            billing=_make_billing(),
        )
        await client.refresh_poll(
            checkout_session_id="cs_test",
            publishable_key="pk_test",
        )
        await client.follow_redirect(
            "https://pm-redirects.stripe.com/authorize/fake"
        )

    asyncio.run(_run_full_flow())

    urls = fake_session.urls_called()
    assert len(urls) >= 5, (
        f"Kỳ vọng ít nhất 5 HTTP call cho 5 method StripeClient, "
        f"nhận {len(urls)}: {urls!r}"
    )
    lookup_calls = [url for url in urls if _STRIPE_LOOKUP_PATH in url]
    assert not lookup_calls, (
        f"R2.7: Backend_Service KHÔNG được gọi '{_STRIPE_LOOKUP_PATH}'. "
        f"Phát hiện {len(lookup_calls)} call:\n"
        + "\n".join(f"  {url}" for url in lookup_calls)
    )


def test_stripe_client_init_alone_does_not_call_lookup() -> None:
    """Isolated: `StripeClient.init(...)` KHÔNG chain gọi consumer session.

    **Validates: Requirements 2.7**
    """
    fake_session = _build_happy_path_session()

    async def _run_init_only() -> None:
        client = StripeClient(
            http_client=fake_session,
            settings=_FakeSettings(),  # type: ignore[arg-type]
            logger=logging.getLogger("test.stripe_init_no_lookup"),
        )
        await client.init(
            checkout_session_id="cs_isolated",
            publishable_key="pk_isolated",
        )

    asyncio.run(_run_init_only())
    urls = fake_session.urls_called()
    assert len(urls) == 1, f"init phải gọi ĐÚNG 1 request, nhận {len(urls)}: {urls!r}"
    assert _STRIPE_LOOKUP_PATH not in urls[0]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
