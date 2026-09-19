"""Network safety unit tests for UPI Direct QR URL policy."""

from __future__ import annotations

import pytest

from app.payments.upi_direct.errors import QrFetchError
from app.payments.upi_direct.network_safety import (
    is_allowed_hosted_instructions_url,
    is_allowed_qr_png_url,
    resolve_public_ips,
    validate_and_reencode_png,
)


def test_reject_suffix_and_userinfo() -> None:
    assert not is_allowed_qr_png_url("https://qr.stripe.com.evil/x.png")
    assert not is_allowed_qr_png_url("https://not-qr.stripe.com/x.png")
    assert not is_allowed_hosted_instructions_url(
        "https://payments.stripe.com.evil/upi/instructions/x"
    )


async def test_resolve_localhost_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_getaddrinfo(host, port, type=0):  # noqa: A002
        return [(0, 0, 0, "", ("127.0.0.1", port))]

    loop = pytest.importorskip("asyncio").get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _fake_getaddrinfo)
    with pytest.raises(QrFetchError, match="non_public"):
        await resolve_public_ips("qr.stripe.com")


def test_validate_png_rejects_non_png() -> None:
    with pytest.raises(QrFetchError, match="not_png"):
        validate_and_reencode_png(b"not-a-png")


def test_extract_intent_state_from_hosted_html() -> None:
    from app.payments.upi_direct.network_safety import extract_intent_state_from_hosted_html
    import base64
    import json

    payload = {"intent_state": "succeeded", "mobile_auth_url": "upi://pay?pa=test"}
    b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    html = f'<html><head><meta id="payload" data-message="{b64}"></head></html>'.encode()
    assert extract_intent_state_from_hosted_html(html) == "succeeded"

    payload_pending = {"intent_state": "requires_payment_method"}
    b64_pending = base64.b64encode(json.dumps(payload_pending).encode()).decode()
    html_pending = f'<html><head><meta id="payload" data-message="{b64_pending}"></head></html>'.encode()
    assert extract_intent_state_from_hosted_html(html_pending) == "requires_payment_method"

    assert extract_intent_state_from_hosted_html(b"<html>no meta</html>") is None
