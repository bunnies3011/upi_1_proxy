"""Pure dataclasses + fail-closed amount parsing for UPI Direct."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ProxyPoolSelection:
    raw_line: str
    materialized_url: str


@dataclass(frozen=True)
class ResolvedProxyPools:
    checkout_lines: list[str] = field(default_factory=list)
    promotion_lines: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CheckoutState:
    checkout_session_id: str
    publishable_key: str
    processor_entity: str | None = None
    raw_payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class AmountState:
    """Explicit amount parse result — never defaults missing data to zero."""

    amount_minor: int | None
    currency: str | None
    source_path: str | None


@dataclass(frozen=True)
class StripeInitState:
    init_checksum: str
    config_id: str
    page_id: str = ""
    amount: AmountState = field(
        default_factory=lambda: AmountState(None, None, None)
    )
    raw_payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class ElementsState:
    session_id: str
    config_id: str | None = None
    raw_payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class ExpectedPaymentState:
    checkout_session_id: str
    page_id: str
    elements_session_id: str
    amount_minor: int
    currency: str = "inr"
    publishable_key: str = ""
    init_checksum: str = ""
    init_config_id: str = ""
    elements_config_id: str | None = None


@dataclass(frozen=True)
class ConfirmAttempt:
    variant: str
    http_status: int
    ok: bool
    data: dict[str, Any] | None = None
    error_detail: str | None = None


@dataclass(frozen=True)
class QrInstruction:
    image_url_png: str | None = None
    hosted_instructions_url: str | None = None
    upi_uri: str | None = None
    expires_at: float | None = None
    source: str = ""


@dataclass(frozen=True)
class ApproveOutcome:
    http_status: int
    result: str | None
    ok: bool
    data: dict[str, Any] | None = None
    ambiguous: bool = False


_AMOUNT_PATHS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("elements_options.amount", ("elements_options", "amount")),
    ("total_summary.due", ("total_summary", "due")),
    ("total_summary.total", ("total_summary", "total")),
    ("invoice.amount_due", ("invoice", "amount_due")),
    ("invoice.total", ("invoice", "total")),
    ("amount_total", ("amount_total",)),
)

_CURRENCY_PATHS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("elements_options.currency", ("elements_options", "currency")),
    ("currency", ("currency",)),
    ("invoice.currency", ("invoice", "currency")),
    ("deferred_intent.currency", ("deferred_intent", "currency")),
)


def _dig(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    cur: Any = payload
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _is_stripe_checkout_session_id(value: str) -> bool:
    return value.strip().startswith("cs_")


def _find_stripe_checkout_session_id(node: Any) -> str | None:
    if isinstance(node, dict):
        for key, value in node.items():
            key_l = str(key).lower()
            if isinstance(value, str) and "checkout" in key_l and "session" in key_l:
                candidate = value.strip()
                if _is_stripe_checkout_session_id(candidate):
                    return candidate
        for value in node.values():
            found = _find_stripe_checkout_session_id(value)
            if found:
                return found
    elif isinstance(node, (list, tuple)):
        for value in node:
            found = _find_stripe_checkout_session_id(value)
            if found:
                return found
    return None


def _is_valid_minor_int(value: Any) -> bool:
    # bool is a subclass of int — reject explicitly.
    return isinstance(value, int) and not isinstance(value, bool)


def parse_amount_state(payload: dict[str, Any]) -> AmountState:
    """Collect all amount candidates; reject conflicts/missing/bad types.

    Never defaults missing amount to 0.
    """
    if not isinstance(payload, dict):
        return AmountState(None, None, None)

    candidates: list[tuple[str, int]] = []
    for path_label, path in _AMOUNT_PATHS:
        value = _dig(payload, path) if len(path) > 1 else payload.get(path[0])
        if value is None:
            continue
        if not _is_valid_minor_int(value):
            return AmountState(None, None, path_label)
        if value < 0:
            return AmountState(None, None, path_label)
        candidates.append((path_label, value))

    if not candidates:
        amount_minor: int | None = None
        amount_path: str | None = None
    else:
        first_amount = candidates[0][1]
        for path_label, value in candidates[1:]:
            if value != first_amount:
                return AmountState(None, None, f"conflict:{candidates[0][0]}!={path_label}")
        amount_minor = first_amount
        amount_path = candidates[0][0]

    currency: str | None = None
    currency_path: str | None = None
    currency_candidates: list[tuple[str, str]] = []
    for path_label, path in _CURRENCY_PATHS:
        value = _dig(payload, path) if len(path) > 1 else payload.get(path[0])
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            return AmountState(amount_minor, None, path_label)
        currency_candidates.append((path_label, value.strip().lower()))

    if currency_candidates:
        first_cur = currency_candidates[0][1]
        for path_label, value in currency_candidates[1:]:
            if value != first_cur:
                return AmountState(
                    amount_minor,
                    None,
                    f"currency_conflict:{currency_candidates[0][0]}!={path_label}",
                )
        currency = first_cur
        currency_path = currency_candidates[0][0]

    source = amount_path or currency_path
    return AmountState(amount_minor=amount_minor, currency=currency, source_path=source)


def parse_checkout_state(payload: dict[str, Any]) -> CheckoutState:
    cs_id = payload.get("checkout_session_id")
    pk = payload.get("publishable_key")
    if not isinstance(cs_id, str) or not cs_id.strip():
        raise ValueError("missing checkout_session_id")
    cs_id_s = cs_id.strip()
    if not _is_stripe_checkout_session_id(cs_id_s):
        nested_cs_id = _find_stripe_checkout_session_id(payload)
        if nested_cs_id:
            cs_id_s = nested_cs_id
        else:
            prefix = cs_id_s.split("_", 1)[0] if "_" in cs_id_s else cs_id_s[:12]
            raise ValueError(
                f"checkout_session_id is not a Stripe session id (prefix={prefix})"
            )
    if not isinstance(pk, str) or not pk.strip():
        raise ValueError("missing publishable_key")
    processor = payload.get("processor_entity")
    processor_s = processor.strip() if isinstance(processor, str) and processor.strip() else None
    return CheckoutState(
        checkout_session_id=cs_id_s,
        publishable_key=pk.strip(),
        processor_entity=processor_s,
        raw_payload=payload,
    )


def parse_stripe_init_state(payload: dict[str, Any]) -> StripeInitState:
    init_checksum = payload.get("init_checksum")
    config_id = payload.get("config_id")
    if not isinstance(init_checksum, str) or not init_checksum:
        raise ValueError("missing init_checksum")
    if not isinstance(config_id, str) or not config_id:
        raise ValueError("missing config_id")
    page_id_raw = payload.get("id")
    page_id = page_id_raw if isinstance(page_id_raw, str) else ""
    return StripeInitState(
        init_checksum=init_checksum,
        config_id=config_id,
        page_id=page_id,
        amount=parse_amount_state(payload),
        raw_payload=payload,
    )


def parse_elements_state(payload: dict[str, Any]) -> ElementsState:
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("missing session_id")
    config_id = payload.get("config_id")
    config_s = config_id if isinstance(config_id, str) else None
    return ElementsState(session_id=session_id, config_id=config_s, raw_payload=payload)


__all__ = [
    "ProxyPoolSelection",
    "ResolvedProxyPools",
    "CheckoutState",
    "AmountState",
    "StripeInitState",
    "ElementsState",
    "ExpectedPaymentState",
    "ConfirmAttempt",
    "QrInstruction",
    "ApproveOutcome",
    "parse_amount_state",
    "parse_checkout_state",
    "parse_stripe_init_state",
    "parse_elements_state",
]
