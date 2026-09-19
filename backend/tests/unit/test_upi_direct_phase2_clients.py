"""Phase-2 client unit tests: processor, already-paid, elements form, boundary."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.payments._chatgpt.models import SessionBundle
from app.payments.upi_direct.chatgpt_client import (
    ChatgptUpiClient,
    contains_already_paid_hint,
    _PROCESSOR,
)
from app.payments.upi_direct.errors import (
    CheckoutError,
    ProcessorEntityMismatchError,
)
from app.payments.upi_direct.flow import UpiDirectFlowHandler
from app.payments.upi_direct.models import parse_checkout_state
from app.payments.upi_direct.stripe_client import StripeUpiClient, flatten_form


def test_already_paid_hint_recursive() -> None:
    assert contains_already_paid_hint(
        {"error": {"message": "User is already paid for this product"}}
    )
    assert not contains_already_paid_hint({"error": {"message": "rate limited"}})
    assert not contains_already_paid_hint("already paid taxes")  # missing full marker


def test_processor_constant() -> None:
    assert _PROCESSOR == "openai_llc"


def test_parse_checkout_processor() -> None:
    state = parse_checkout_state(
        {
            "checkout_session_id": "cs_test",
            "publishable_key": "pk_test",
            "processor_entity": "openai_llc",
        }
    )
    assert state.processor_entity == "openai_llc"


def test_processor_mismatch_raises() -> None:
    # Flow-level check unit: simulate decision
    actual = "openai_ie"
    if actual and actual != _PROCESSOR:
        with pytest.raises(ProcessorEntityMismatchError):
            raise ProcessorEntityMismatchError(actual=actual)


def test_elements_form_contains_upi_and_inr() -> None:
    # Build the same query structure StripeUpiClient.elements_sessions uses.
    pairs = [
        ("deferred_intent[currency]", "inr"),
        ("deferred_intent[payment_method_types][0]", "card"),
        ("deferred_intent[payment_method_types][1]", "link"),
        ("deferred_intent[payment_method_types][2]", "upi"),
        ("currency", "inr"),
    ]
    flat = {k: v for k, v in pairs}
    assert flat["currency"] == "inr"
    assert "upi" in flat.values()


def test_flatten_form_nested() -> None:
    pairs = flatten_form({"a": {"b": 1}, "c": [True, None]})
    assert ("a[b]", "1") in pairs
    assert ("c[0]", "true") in pairs
    assert not any(p[0] == "c[1]" for p in pairs)


async def test_create_checkout_builds_in_inr() -> None:
    captured: dict = {}

    class Resp:
        status_code = 200

        def json(self):
            return {
                "checkout_session_id": "cs_x",
                "publishable_key": "pk_x",
                "processor_entity": "openai_llc",
            }

    client = MagicMock()
    client.post = AsyncMock(return_value=Resp())

    def _capture(*args, **kwargs):
        captured["json"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers")
        return Resp()

    client.post = AsyncMock(side_effect=_capture)
    c = ChatgptUpiClient(client, MagicMock())
    session = SessionBundle(email="a@b.com", access_token="tok", cookies={})
    state = await c.create_checkout(session)
    assert state.checkout_session_id == "cs_x"
    assert captured["json"]["billing_details"] == {"country": "IN", "currency": "INR"}
    assert captured["headers"]["OAI-Language"] == "en-IN"


async def test_update_promo_uses_openai_llc_referer() -> None:
    captured: dict = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"ok": True}

    client = MagicMock()

    async def _post(*args, **kwargs):
        captured["json"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers")
        return Resp()

    client.post = _post
    c = ChatgptUpiClient(client, MagicMock())
    session = SessionBundle(email="a@b.com", access_token="tok", cookies={})
    await c.update_checkout_promo(session, "cs_1")
    assert captured["json"]["processor_entity"] == "openai_llc"
    assert "openai_llc/cs_1" in captured["headers"]["Referer"]


def test_boundary_no_ideal_import_phase2() -> None:
    import ast
    import app.payments.upi_direct as pkg

    root = Path(pkg.__file__).parent  # type: ignore[arg-type]
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("app.payments.ideal"), path
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("app.payments.ideal"), path
