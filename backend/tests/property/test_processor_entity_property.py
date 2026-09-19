"""Property test cho `payments/ideal/chatgpt_client.py::ChatgptClient.create_checkout`
— **Property 4: `processor_entity` khác giá trị mong đợi không làm fail flow**.

Kịch bản: sinh chuỗi `processor_entity` non-empty bất kỳ (có thể là giá trị
mong đợi `openai_ie`, có thể khác — cả 2 case đều phải hợp lệ), setup
`httpx.MockTransport` trả JSON response chứa `processor_entity` đó, gọi
`ChatgptClient.create_checkout(session)` và assert:

1. `CheckoutSession.processor_entity` trả về = giá trị hypothesis sinh
   (identity/pass-through — Requirement 1.8 KHÔNG cho phép biến đổi giá trị).
2. `create_checkout` KHÔNG raise exception (đặc biệt khi
   `processor_entity != "openai_ie"` — chỉ log warning theo R1.8, KHÔNG fatal).
3. Kết quả trả về non-None (là 1 `CheckoutSession` hợp lệ).

Cách kiểm chứng no-raise: KHÔNG dùng `pytest.raises` — chạy scenario và nếu
có exception thoát ra thì `asyncio.run` sẽ propagate → pytest tự fail test
với traceback rõ ràng.

**Validates: Requirements 1.8**
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from tests.support.fake_http import FakeAsyncSession, FakeResponse
from hypothesis import given, settings, strategies as st

from app.payments.ideal.chatgpt_client import ChatgptClient
from app.payments.ideal.models import CheckoutSession, SessionBundle

# ---------------------------------------------------------------------------
# Fake AccountSessionCache — chỉ cần match interface method mà
# `ChatgptClient.create_checkout` có thể chạm tới. Thực tế `create_checkout`
# KHÔNG gọi cache trong path thành công (caller `IdealFlowHandler` quản lý
# lifecycle), nhưng vẫn cung cấp no-op để giữ đúng type contract của
# constructor `ChatgptClient(session_cache=...)`.
# ---------------------------------------------------------------------------


class _FakeAccountSessionCache:
    """No-op fake `AccountSessionCache` cho property test — R1.8.

    Chỉ implement 2 method `get`/`save` mà `ChatgptClient` có thể gọi:
    - `get(account_key) -> None` → luôn miss cache.
    - `save(account_key, payload) -> None` → no-op.

    Duck-typing đủ để `ChatgptClient` constructor chấp nhận (Python không
    enforce runtime type check trên `AccountSessionCache` annotation).
    """

    async def get(self, account_key: str) -> None:
        return None

    async def save(self, account_key: str, payload: dict[str, Any]) -> None:
        return None


# ---------------------------------------------------------------------------
# Fixture data — response JSON hợp lệ tối thiểu để `parse_checkout_session()`
# parse thành công (đủ 3 field bắt buộc R1.7 + các field optional có default).
# `processor_entity` bị inject bằng giá trị hypothesis sinh ở mỗi example.
# ---------------------------------------------------------------------------


def _build_checkout_response(processor_entity: str) -> dict[str, Any]:
    """Build response JSON mock với `processor_entity` tuỳ ý."""
    return {
        "checkout_session_id": "cs_abc",
        "publishable_key": "pk_test",
        "processor_entity": processor_entity,
        "client_secret": "sec",
        "status": "open",
        "payment_status": "unpaid",
        "requires_manual_approval": False,
    }


_FAKE_SESSION = SessionBundle(
    email="a@b.c",
    access_token="tok",
    cookies={},
)


# `processor_entity` strategy — text non-empty, ký tự bất kỳ TRỪ control
# chars (Cc) và surrogates (Cs). Bao trùm:
#   - Giá trị mong đợi `"openai_ie"` (subset của alphabet).
#   - Giá trị khác thuộc pattern OpenAI (`openai_llc`, `openai_uk`, ...).
#   - Chuỗi hoàn toàn khác (mã dài, unicode, ký tự đặc biệt).
# Không strip whitespace → check cả case `processor_entity` có space giữa.
_processor_entity_strategy = st.text(
    min_size=1,
    max_size=30,
    alphabet=st.characters(blacklist_categories=("Cc", "Cs")),
)


@given(processor_entity=_processor_entity_strategy)
@settings(max_examples=100, deadline=None)
def test_create_checkout_preserves_arbitrary_processor_entity(
    processor_entity: str,
) -> None:
    """`processor_entity` từ response phải xuất hiện NGUYÊN VẸN trên
    `CheckoutSession.processor_entity` VÀ flow KHÔNG raise dù giá trị khác
    `openai_ie` — Requirement 1.8.
    """

    def _handler(call):  # noqa: ANN001
        return FakeResponse(200, json_body=_build_checkout_response(processor_entity))

    result_container: dict[str, CheckoutSession | None] = {"result": None}

    async def _scenario() -> None:
        fake_session = FakeAsyncSession(default_handler=_handler)
        client = ChatgptClient(
            http_client=fake_session,
            session_cache=_FakeAccountSessionCache(),  # type: ignore[arg-type]
            logger=logging.getLogger("test.chatgpt_client.processor_entity"),
        )
        result_container["result"] = await client.create_checkout(_FAKE_SESSION)

    asyncio.run(_scenario())

    result = result_container["result"]

    # (a) `create_checkout` phải trả về CheckoutSession non-None — R1.8 yêu
    # cầu flow tiếp tục bình thường khi `processor_entity` khác giá trị mong
    # đợi.
    assert result is not None, (
        "`create_checkout` phải trả về `CheckoutSession` non-None khi response "
        "hợp lệ (kể cả khi `processor_entity` khác `openai_ie`) — R1.8."
    )
    assert isinstance(result, CheckoutSession), (
        f"`create_checkout` phải trả về `CheckoutSession`, "
        f"thay vì {type(result).__name__}."
    )

    # (b) Identity mapping: `processor_entity` trên object trả về = input
    # nguyên vẹn (không strip, không normalize, không thay bằng default).
    assert result.processor_entity == processor_entity, (
        "`CheckoutSession.processor_entity` phải giữ nguyên giá trị nhận từ "
        f"response — expected={processor_entity!r} nhưng "
        f"actual={result.processor_entity!r} (R1.8 KHÔNG cho phép biến đổi)."
    )


if __name__ == "__main__":  # pragma: no cover
    # Cho phép chạy file trực tiếp (`python3 test/...py`) để smoke test
    # nhanh cục bộ — production dùng `pytest`.
    pytest.main([__file__, "-v"])
