"""Unit tests for UPI Direct approve retry behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from app.core.payment_flow import Job, SimpleCancellationToken
from app.payments._chatgpt.models import SessionBundle
from app.payments.upi_direct import confirm_qr_flow as flow
import pytest

from app.payments.upi_direct.errors import ApproveFailedError, ApproveOutcomeUnknownError
from app.payments.upi_direct.errors import StripeSetupFailedError
from app.payments.upi_direct.models import (
    ApproveOutcome,
    ConfirmAttempt,
    ExpectedPaymentState,
)


def _job() -> Job:
    return Job(
        job_id="job-approve-retry",
        payment_method="upi_direct",
        account_line="user@example.com|token",
        created_at=0.0,
        cancellation_token=SimpleCancellationToken(),
    )


def _expected() -> ExpectedPaymentState:
    return ExpectedPaymentState(
        checkout_session_id="cs_test",
        page_id="ppage_test",
        elements_session_id="elements_test",
        amount_minor=0,
        publishable_key="pk_test",
        init_checksum="chk",
    )


async def test_reposts_approve_after_unknown_reconciliation() -> None:
    chatgpt = MagicMock()
    chatgpt.approve = AsyncMock(
        side_effect=[
            ApproveOutcome(
                http_status=200,
                result=None,
                ok=False,
                data={},
                ambiguous=True,
            ),
            ApproveOutcome(
                http_status=200,
                result="approved",
                ok=True,
                data={"result": "approved"},
                ambiguous=False,
            ),
        ]
    )

    with patch.object(
        flow,
        "_reconcile_ambiguous_approve",
        new=AsyncMock(side_effect=[ApproveOutcomeUnknownError()]),
    ) as reconcile:
        result = await flow._approve_checkout_with_error_retries(  # noqa: SLF001
            job=_job(),
            session=SessionBundle(
                email="user@example.com",
                access_token="tok",
                cookies={},
            ),
            chatgpt=chatgpt,
            stripe=MagicMock(),
            expected=_expected(),
            checkout_session_id="cs_test",
            candidates=[],
            logger=MagicMock(),
            health_box=[None],
            approve_error_retries=2,
        )

    assert result is None
    assert chatgpt.approve.await_count == 2
    assert reconcile.await_count == 1


async def test_invalid_promotion_is_not_reconciled_as_ambiguous() -> None:
    chatgpt = MagicMock()
    chatgpt.approve = AsyncMock(
        return_value=ApproveOutcome(
            http_status=200,
            result="invalid_promotion",
            ok=False,
            data={"result": "invalid_promotion"},
            ambiguous=False,
        )
    )

    with patch.object(
        flow,
        "_reconcile_ambiguous_approve",
        new=AsyncMock(),
    ) as reconcile:
        with pytest.raises(ApproveFailedError) as exc_info:
            await flow._approve_checkout_with_error_retries(  # noqa: SLF001
                job=_job(),
                session=SessionBundle(
                    email="user@example.com",
                    access_token="tok",
                    cookies={},
                ),
                chatgpt=chatgpt,
                stripe=MagicMock(),
                expected=_expected(),
                checkout_session_id="cs_test",
                candidates=[],
                logger=MagicMock(),
                health_box=[None],
                approve_error_retries=2,
            )

    assert exc_info.value.error_code == "approve_invalid_promotion"
    assert chatgpt.approve.await_count == 1
    reconcile.assert_not_awaited()


async def test_approve_ok_payload_adds_qr_candidate() -> None:
    hosted = "https://payments.stripe.com/upi/instructions/from-approve"
    chatgpt = MagicMock()
    chatgpt.approve = AsyncMock(
        return_value=ApproveOutcome(
            http_status=200,
            result="approved",
            ok=True,
            data={
                "payment_intent": {
                    "next_action": {
                        "upi_handle_redirect_or_display_qr_code": {
                            "hosted_instructions_url": hosted,
                        }
                    }
                }
            },
            ambiguous=False,
        )
    )
    candidates = []

    result = await flow._approve_checkout_with_error_retries(  # noqa: SLF001
        job=_job(),
        session=SessionBundle(
            email="user@example.com",
            access_token="tok",
            cookies={},
        ),
        chatgpt=chatgpt,
        stripe=MagicMock(),
        expected=_expected(),
        checkout_session_id="cs_test",
        candidates=candidates,
        logger=MagicMock(),
        health_box=[None],
        approve_error_retries=1,
    )

    assert result is None
    assert len(candidates) == 1
    assert candidates[0].hosted_instructions_url == hosted


async def test_post_approve_refresh_polls_until_new_qr_payload() -> None:
    hosted = "https://payments.stripe.com/upi/instructions/polled"
    stripe = MagicMock()
    stripe.payment_page_refresh = AsyncMock(
        side_effect=[
            {"id": "ppage_test", "payment_intent": None},
            {"id": "ppage_test", "payment_intent": None},
            {
                "id": "ppage_test",
                "payment_intent": {
                    "next_action": {
                        "upi_handle_redirect_or_display_qr_code": {
                            "hosted_instructions_url": hosted,
                        }
                    }
                },
            },
        ]
    )
    candidates = []

    payload = await flow._poll_post_approve_refresh_for_qr(  # noqa: SLF001
        job=_job(),
        stripe=stripe,
        expected=_expected(),
        candidates=candidates,
        logger=MagicMock(),
    )

    assert payload is not None
    assert stripe.payment_page_refresh.await_count == 3
    assert len(candidates) == 1
    assert candidates[0].hosted_instructions_url == hosted


async def test_post_approve_confirm_adds_qr_candidate() -> None:
    hosted = "https://payments.stripe.com/upi/instructions/post-confirm"
    stripe = MagicMock()
    stripe.confirm_upi = AsyncMock(
        return_value=ConfirmAttempt(
            variant="qr_code",
            http_status=200,
            ok=True,
            data={
                "payment_intent": {
                    "next_action": {
                        "upi_handle_redirect_or_display_qr_code": {
                            "hosted_instructions_url": hosted,
                        }
                    }
                }
            },
        )
    )
    candidates = []

    confirm = await flow._post_approve_confirm_for_qr(  # noqa: SLF001
        job=_job(),
        stripe=stripe,
        expected=_expected(),
        profile=MagicMock(),
        email="user@example.com",
        token_config=None,
        candidates=candidates,
        logger=MagicMock(),
    )

    assert confirm is not None
    assert len(candidates) == 1
    assert candidates[0].hosted_instructions_url == hosted


def test_stripe_setup_failure_detail_from_post_approve_payload() -> None:
    detail = flow._stripe_no_qr_failure_detail(  # noqa: SLF001
        [
            {
                "setup_intent": {
                    "status": "requires_payment_method",
                    "next_action": None,
                    "last_setup_error": {
                        "code": "setup_attempt_failed",
                        "decline_code": "generic_decline",
                        "type": "card_error",
                    },
                }
            }
        ]
    )

    assert detail == (
        "stripe_setup_failed:"
        "code=setup_attempt_failed:"
        "decline=generic_decline:"
        "type=card_error:"
        "status=requires_payment_method"
    )


def test_stripe_setup_failed_error_code() -> None:
    exc = StripeSetupFailedError(
        detail="stripe_setup_failed:code=setup_attempt_failed"
    )

    assert exc.error_code == "stripe_setup_failed"
    assert exc.step == "qr"
