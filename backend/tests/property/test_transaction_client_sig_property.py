"""Property 14: `sig` được truyền nguyên vẹn tới bước initiate transaction.

Sinh chuỗi `sig` ngẫu nhiên, gọi `TransactionClient.initiate(...)` với
`FakeAsyncSession` để capture `call.params["sig"]`. Assert == input.

**Validates: Requirements 4.9**
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from hypothesis import given, settings, strategies as st

from app.payments.ideal.models import DeviceProfile
from app.payments.ideal.transaction_client import TransactionClient
from tests.support.fake_http import FakeAsyncSession, FakeResponse

_VALID_INITIATE_RESPONSE: dict[str, Any] = {
    "view": "INITIAL_VIEW",
    "amount": "10.99",
    "creditorName": "Test",
    "supportedIssuers": [
        {
            "id": "INGBNL2A",
            "deeplinkType": "URL",
            "deeplink": "https://x/y",
            "availabilityStatus": "AVAILABLE",
        }
    ],
}

_FAKE_DEVICE_PROFILE = DeviceProfile(
    language="nl-NL",
    timeZone="Europe/Amsterdam",
    screenWidth=1920,
    screenHeight=1080,
    screenAvailableWidth=1920,
    screenAvailableHeight=1040,
    colorDepth=24,
)

_ENCODED_TX_URL = "abc"

_sig_strategy = st.text(
    min_size=1,
    max_size=100,
    alphabet=st.characters(blacklist_categories=("Cc", "Cs")),
)


@given(sig=_sig_strategy)
@settings(max_examples=100, deadline=None)
def test_initiate_passes_sig_through_query_param_identity(sig: str) -> None:
    """`sig` input xuất hiện NGUYÊN VẸN ở `params["sig"]` — R4.9."""

    def _handler(call):  # noqa: ANN001
        return FakeResponse(200, json_body=_VALID_INITIATE_RESPONSE)

    fake_session = FakeAsyncSession(default_handler=_handler)

    async def _scenario() -> None:
        client = TransactionClient(
            http_client=fake_session,
            logger=logging.getLogger("test.transaction_client.sig"),
        )
        await client.initiate(
            encoded_tx_url=_ENCODED_TX_URL,
            sig=sig,
            device_profile=_FAKE_DEVICE_PROFILE,
        )

    asyncio.run(_scenario())

    assert len(fake_session.calls) == 1, (
        f"initiate() phải gửi đúng 1 request, đã bắt được {len(fake_session.calls)}"
    )
    call = fake_session.calls[0]
    assert call.params is not None and call.params.get("sig") == sig, (
        f"`sig` phải được truyền nguyên vẹn qua params — "
        f"input={sig!r} nhưng captured params={call.params!r}"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
