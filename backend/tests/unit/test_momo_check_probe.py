import logging

import pytest

from app.core.payment_flow import Job, SimpleCancellationToken
from app.payments._chatgpt.models import SessionBundle
from app.payments.momo_check.probe import (
    MomoCheckoutConcurrencyLimitError,
    _create_checkout_with_backoff,
    _is_checkout_concurrency_limited,
    collect_payment_method_tokens,
)
from app.payments.upi_direct.errors import CheckoutError
from app.payments.upi_direct.models import CheckoutState


def test_collect_payment_methods_ignores_catalog_and_request_echo() -> None:
    payload = {
        "payment_method_specs": [
            {"type": "momo"},
            {"type": "paypal"},
        ],
        "deferred_intent": {
            "payment_method_types": ["card", "link", "momo"],
        },
        "external_payment_method_types": ["momo"],
        "payment_method_types": ["card"],
    }

    assert collect_payment_method_tokens(payload) == ["card"]


def test_collect_payment_methods_reads_available_checkout_methods() -> None:
    payload = {
        "payment_method_preference": {
            "ordered_payment_method_types": ["card", "momo"],
        },
    }

    assert collect_payment_method_tokens(payload) == ["card", "momo"]


def test_collect_payment_methods_ignores_ineligible_methods() -> None:
    payload = {
        "payment_method_preference": {
            "ordered_payment_method_types": ["card"],
            "ineligible": {
                "payment_method_types": ["momo"],
            },
        },
    }

    assert collect_payment_method_tokens(payload) == ["card"]


def _checkout_error(status_code: int, detail: str) -> CheckoutError:
    error = CheckoutError(detail=detail)
    setattr(error, "status_code", status_code)
    return error


def test_checkout_concurrency_classifier_is_narrow() -> None:
    assert _is_checkout_concurrency_limited(
        _checkout_error(503, "http_503:reached concurrency limit")
    )
    assert not _is_checkout_concurrency_limited(
        _checkout_error(503, "http_503:service unavailable")
    )
    assert not _is_checkout_concurrency_limited(
        _checkout_error(403, "http_403:reached concurrency limit")
    )


class _FakeCheckoutClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    async def create_checkout(
        self,
        session: SessionBundle,
        *,
        billing_country: str,
        billing_currency: str,
        language: str,
    ) -> CheckoutState:
        del session, billing_country, billing_currency, language
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, CheckoutState)
        return outcome


def _job() -> Job:
    return Job(
        job_id="job-1",
        payment_method="momo_check",
        account_line="a@example.com|password|totp",
        created_at=0.0,
        cancellation_token=SimpleCancellationToken(),
    )


def _session() -> SessionBundle:
    return SessionBundle(
        email="a@example.com",
        access_token="token",
        cookies={},
    )


def _checkout_state() -> CheckoutState:
    return CheckoutState(
        checkout_session_id="cs_test_1",
        publishable_key="pk_test_1",
        processor_entity="openai_llc",
        raw_payload={},
    )


@pytest.mark.asyncio
async def test_checkout_retries_only_concurrency_limit() -> None:
    client = _FakeCheckoutClient(
        [
            _checkout_error(503, "http_503:reached concurrency limit"),
            _checkout_error(503, "http_503:reached concurrency limit"),
            _checkout_state(),
        ]
    )

    result = await _create_checkout_with_backoff(
        job=_job(),
        chatgpt=client,  # type: ignore[arg-type]
        session=_session(),
        logger=logging.getLogger("test"),  # type: ignore[arg-type]
        retry_delays=(0.0, 0.0),
    )

    assert result.checkout_session_id == "cs_test_1"
    assert client.calls == 3


@pytest.mark.asyncio
async def test_checkout_does_not_retry_403() -> None:
    error = _checkout_error(403, "http_403:blocked")
    client = _FakeCheckoutClient([error])

    with pytest.raises(CheckoutError) as exc_info:
        await _create_checkout_with_backoff(
            job=_job(),
            chatgpt=client,  # type: ignore[arg-type]
            session=_session(),
            logger=logging.getLogger("test"),  # type: ignore[arg-type]
            retry_delays=(0.0, 0.0),
        )

    assert exc_info.value is error
    assert client.calls == 1


@pytest.mark.asyncio
async def test_checkout_concurrency_limit_exhaustion_has_specific_code() -> None:
    client = _FakeCheckoutClient(
        [
            _checkout_error(503, "http_503:reached concurrency limit"),
            _checkout_error(503, "http_503:reached concurrency limit"),
        ]
    )

    with pytest.raises(MomoCheckoutConcurrencyLimitError) as exc_info:
        await _create_checkout_with_backoff(
            job=_job(),
            chatgpt=client,  # type: ignore[arg-type]
            session=_session(),
            logger=logging.getLogger("test"),  # type: ignore[arg-type]
            retry_delays=(0.0,),
        )

    assert exc_info.value.error_code == "checkout_concurrency_limited"
    assert client.calls == 2
