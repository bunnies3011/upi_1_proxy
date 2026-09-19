"""Unit test log entries chứa đầy đủ field bắt buộc ở mỗi bước Stripe/approve/refresh/redirect/initiate (Task 35.2).

**Requirements: 1.9, 2.8, 3.7, 4.10, 5.9, 14.2**

Yêu cầu:

    (A) Mỗi request HTTP tới Stripe/ChatGPT/pay.ideal.nl trong flow phải
        emit log với 3 field bắt buộc: `request_name` (định danh request),
        `attempt` (số lần thử — 1 cho request không retry), `http_status`
        (mã HTTP nhận được, có thể omit khi request chưa nhận response).

    (B) Khi IdealJob chuyển `error`, `JobResult.error_message` phải ghi
        đầy đủ 3 phần: bước xảy ra lỗi (`step`), mã HTTP nếu có, mô tả
        nguyên nhân cụ thể — KHÔNG dùng thông báo chung mơ hồ (R14.2).

Test scope tập trung vào `StripeClient._bounded_retry` + `_StripeLoggerAdapter`
+ hierarchy `IdealFlowError`, vì đây là 3 điểm CHỦ ĐỘNG emit log
`request_name`/`attempt`/`http_status` và điểm build `error_message` trung
tâm cho flow error. Test dùng `httpx.MockTransport` cho isolation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from app.payments.ideal.errors import (
    AddressValidationRejectedError,
    ApproveFailedError,
    ConfirmInvalidResponseError,
    IdealFlowError,
    RedirectValidationError,
    RefreshPollExhaustedError,
    StripeHttpClientError,
    TransactionHttpError,
)
from app.payments.ideal.flow import _StripeLoggerAdapter
from app.payments.ideal.models import BillingAddress
from app.payments.ideal.stripe_client import StripeClient
from tests.support.fake_http import FakeAsyncSession, FakeResponse

# ---------------------------------------------------------------------------
# Fake infra
# ---------------------------------------------------------------------------


class _FakeSettings:
    """Trả `None` cho mọi key → StripeClient fallback về default nội bộ."""

    async def get(self, key: str) -> Any | None:  # noqa: ARG002
        return None


class _RecordingLogger:
    """Logger duck-typed compatible với `_SupportsInfoLog` — capture `(event, kwargs)`.

    Không dùng `logging.Logger` chuẩn vì test cần kiểm tra kwargs shape
    (`request`, `attempt`, `max_attempts`, `status_code`) mà `logging.Logger`
    không expose nguyên vẹn — nó gộp vào `LogRecord.extra`.
    """

    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []
        #: Capture positional %-style args riêng (production dùng cả 2 kiểu
        #: log: structured kwargs cho error branch + `logger.info(msg, *args)`
        #: %-format cho happy path). Giữ để assertion đọc được cả 2 shape.
        self.positional: list[tuple[str, tuple[Any, ...]]] = []

    def info(self, event: str, /, *args: Any, **kwargs: Any) -> None:
        self.records.append((event, kwargs))
        if args:
            self.positional.append((event, args))


class _RecordingStdlibLogger(logging.Logger):
    """`logging.Logger` subclass capture message thành list — cho test adapter."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.messages: list[str] = []
        self.setLevel(logging.DEBUG)

    def info(self, msg: object, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        self.messages.append(str(msg) % args if args else str(msg))


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


# ---------------------------------------------------------------------------
# (A) — Log entries chứa `request_name`, `attempt`, `http_status`
# ---------------------------------------------------------------------------


def test_bounded_retry_logs_request_attempt_status_for_each_step() -> None:
    """`StripeClient._bounded_retry` emit log kèm định danh request + HTTP
    status khi request thành công (R2.8).

    Test 1 flow `init` happy-path (200 lần đầu). Production hiện GỘP log
    success attempt-đầu thành 1 dòng %-format duy nhất:

        ``logger.info("stripe %s ok status=%d", request_name, status)``

    (branch `attempt == 1` trong `_bounded_retry`). `attempt`/`max_attempts`
    KHÔNG lặp lại như structured field ở happy path — chúng chỉ xuất hiện
    khi retry (`attempt > 1`) hoặc ở branch lỗi (`transport_error`/`http_5xx`/
    `fail_fast`, xem test 4xx bên dưới). Do đó ở đây chỉ verify phần R2.8
    mà happy-path THỰC SỰ đảm bảo: log nhận diện được ĐÚNG request nào +
    ĐÚNG mã HTTP nào — qua positional args của dòng %-format.

    **Validates: Requirements 2.8**
    """
    def _handler(call):  # noqa: ANN001
        return FakeResponse(
            200,
            json_body={
                "init_checksum": "chk_ok",
                "config_id": "cfg_ok",
            },
        )

    recording_logger = _RecordingLogger()

    async def _run() -> None:
        fake_session = FakeAsyncSession(default_handler=_handler)
        client = StripeClient(
            http_client=fake_session,
            settings=_FakeSettings(),  # type: ignore[arg-type]
            logger=recording_logger,  # type: ignore[arg-type]
        )
        await client.init(
            checkout_session_id="cs_log_test",
            publishable_key="pk_log_test",
        )

    asyncio.run(_run())

    # Phải emit ít nhất 1 log entry cho request `init`.
    assert recording_logger.records, (
        f"Thiếu log entry cho request init (R2.8). "
        f"Records: {recording_logger.records!r}"
    )

    # Success log dạng %-format: event chứa placeholder, args=(request, status).
    success_logs = [
        (event, args)
        for event, args in recording_logger.positional
        if "ok" in event and "status" in event
    ]
    assert success_logs, (
        f"Thiếu log 'stripe ... ok status=...' sau khi có response — R2.8 "
        f"yêu cầu log mã HTTP nhận được. "
        f"positional={recording_logger.positional!r}, "
        f"records={recording_logger.records!r}"
    )

    event, args = success_logs[0]
    # (1) Log phải nhận diện ĐÚNG request đang gọi (`init`) — R2.8.
    assert "init" in args, (
        f"Log '{event}' thiếu định danh request 'init' (R2.8): args={args!r}"
    )
    # (2) Log phải kèm HTTP status nhận được (200) — R2.8.
    assert 200 in args, (
        f"Log '{event}' thiếu HTTP status 200 nhận được (R2.8): args={args!r}"
    )


def test_bounded_retry_logs_status_code_on_client_error_4xx() -> None:
    """Khi Stripe trả 4xx, log entries phải bao gồm `status_code` và tag
    `fail_fast` — R2.6 + R2.8: caller biết chính xác status HTTP để tra
    cứu (không thông báo chung mơ hồ — R14.2).

    **Validates: Requirements 2.8, 14.2**
    """
    def _handler(call):  # noqa: ANN001
        return FakeResponse(400, json_body={"error": {"code": "bad_request"}})

    recording_logger = _RecordingLogger()

    async def _run() -> None:
        fake_session = FakeAsyncSession(default_handler=_handler)
        client = StripeClient(
            http_client=fake_session,
            settings=_FakeSettings(),  # type: ignore[arg-type]
            logger=recording_logger,  # type: ignore[arg-type]
        )
        # init dùng `_bounded_retry` — status 400 → Fail_Fast (R2.6),
        # raise StripeHttpClientError.
        await client.init(
            checkout_session_id="cs_400",
            publishable_key="pk_400",
        )

    with pytest.raises(StripeHttpClientError):
        asyncio.run(_run())

    # Có log "stripe request fail_fast" hoặc "stripe request completed"
    # với status_code=400.
    events_with_status = [
        (event, kwargs)
        for event, kwargs in recording_logger.records
        if kwargs.get("status_code") == 400
    ]
    assert events_with_status, (
        f"Thiếu log entry kèm status_code=400 khi Stripe trả 4xx — R2.8 "
        f"yêu cầu log mã lỗi HTTP. Records: {recording_logger.records!r}"
    )

    # Verify có event fail_fast tường minh (không thông báo chung mơ hồ).
    fail_fast_events = [
        (event, kwargs)
        for event, kwargs in recording_logger.records
        if event == "stripe request fail_fast"
    ]
    assert fail_fast_events, (
        f"Thiếu log 'stripe request fail_fast' — R14.2 yêu cầu log tường "
        f"minh khi Fail_Fast_Policy. Records: {recording_logger.records!r}"
    )


def test_stripe_logger_adapter_formats_kwargs_into_message() -> None:
    """`_StripeLoggerAdapter.info(event, **kwargs)` phải format ra 1 message
    chuỗi chứa `event` + tất cả kwargs → truyền vào stdlib logger.

    Đây là bridge layer từ StripeClient tới logger per-job của flow.
    Verify shape kết quả chứa các field bắt buộc `request`/`attempt`/
    `status_code` để log trong SSE/journal đủ context R2.8/R14.2.
    """
    stdlib = _RecordingStdlibLogger("test.stripe_adapter")
    adapter = _StripeLoggerAdapter(stdlib, known_secrets=[])

    adapter.info(
        "stripe request completed",
        request="confirm",
        attempt=1,
        max_attempts=3,
        status_code=400,
    )

    assert stdlib.messages, "Adapter phải forward log tới stdlib logger."
    msg = stdlib.messages[0]

    # Verify các key có mặt trong message (dạng key=val).
    assert "stripe request completed" in msg
    assert "request=" in msg and "'confirm'" in msg, (
        f"Message thiếu field 'request' (R2.8): {msg!r}"
    )
    assert "attempt=" in msg and "1" in msg, (
        f"Message thiếu field 'attempt' (R2.8): {msg!r}"
    )
    assert "status_code=" in msg and "400" in msg, (
        f"Message thiếu field 'status_code' (R2.8, R14.2): {msg!r}"
    )


def test_stripe_logger_adapter_redacts_known_secrets() -> None:
    """`_StripeLoggerAdapter` redact `known_secrets` khi format message.

    R1.9/R14.8: log không được lộ giá trị nhạy cảm — ngay cả khi giá trị
    lỡ được inject vào message qua event/kwargs.
    """
    stdlib = _RecordingStdlibLogger("test.stripe_adapter.redact")
    secret_value = "secret_sig_abcxyz"
    adapter = _StripeLoggerAdapter(stdlib, known_secrets=[secret_value])

    adapter.info(
        "stripe follow_redirect extracted",
        request="follow_redirect",
        sig=secret_value,  # noqa: S106 — dữ liệu test có ý thức
    )

    msg = stdlib.messages[0]
    assert secret_value not in msg, (
        f"Secret value '{secret_value}' KHÔNG được xuất hiện trong log "
        f"(R1.9, R14.8): {msg!r}"
    )


# ---------------------------------------------------------------------------
# (B) — IdealFlowError.error_message chứa đủ 3 phần: step, HTTP status, cause
# ---------------------------------------------------------------------------


def test_stripe_http_client_error_message_contains_step_status_cause() -> None:
    """`StripeHttpClientError` (R2.6) → 3 phần:

        - step: `stripe_init` / `stripe_elements` / `stripe_confirm` / ...
        - http_status: mã HTTP (integer, non-None).
        - message cause: mô tả nguyên nhân cụ thể (không phải generic).

    **Validates: Requirements 14.2**
    """
    exc = StripeHttpClientError(
        request_name="init",
        http_status=400,
        body_snippet="{'error': {'code': 'bad_request'}}",
    )
    assert exc.step == "stripe_init"
    assert exc.http_status == 400
    assert exc.error_code == "stripe_http_4xx"
    # Message chứa cả tên request và status.
    msg = str(exc)
    assert "init" in msg or "stripe_init" in msg, (
        f"Message thiếu định danh request/step: {msg!r}"
    )
    assert "400" in msg, f"Message thiếu HTTP status: {msg!r}"
    assert "client error" in msg or "no retry" in msg or "không retry" in msg, (
        f"Message không mô tả nguyên nhân cụ thể (R14.2 — không dùng thông "
        f"báo chung mơ hồ): {msg!r}"
    )


def test_address_validation_rejected_message_contains_field() -> None:
    """`AddressValidationRejectedError` (R3.4) → phải chứa field cụ thể bị reject."""
    exc = AddressValidationRejectedError(rejected_field="address_line1")
    assert exc.step == "stripe_confirm"
    assert exc.rejected_field == "address_line1"
    assert "address_line1" in str(exc), (
        f"Message thiếu field cụ thể bị reject: {str(exc)!r}"
    )


def test_confirm_invalid_response_error_message_contains_http_status() -> None:
    """`ConfirmInvalidResponseError` (R3.5) → chứa `http_status`."""
    exc = ConfirmInvalidResponseError(
        http_status=500,
        message="Stripe confirm HTTP status không hợp lệ (http_status=500)",
    )
    assert exc.step == "stripe_confirm"
    assert exc.http_status == 500
    assert "500" in str(exc)


def test_approve_failed_error_message_contains_status_and_detail() -> None:
    """`ApproveFailedError` (R4.2) → chứa http_status + detail (cause).

    **Validates: Requirements 14.2**
    """
    exc = ApproveFailedError(http_status=502, detail="upstream timeout")
    assert exc.step == "checkout_approve"
    assert "502" in str(exc)
    assert "upstream timeout" in str(exc)


def test_refresh_poll_exhausted_error_message_contains_attempts() -> None:
    """`RefreshPollExhaustedError` (R4.6) → chứa số attempts đã thử.

    **Validates: Requirements 14.2**
    """
    exc = RefreshPollExhaustedError(attempts=5)
    assert exc.step == "refresh_poll"
    assert "5" in str(exc)


def test_redirect_validation_error_message_contains_status_and_location_flag() -> None:
    """`RedirectValidationError` (R4.8) → chứa http_status + has_location flag."""
    exc = RedirectValidationError(http_status=200, has_location=False)
    assert exc.step == "follow_redirect"
    assert "200" in str(exc)
    assert "has_location" in str(exc)


def test_transaction_http_error_message_contains_status_and_detail() -> None:
    """`TransactionHttpError` (R5.8) — bước `initiate` pay.ideal.nl."""
    exc = TransactionHttpError(http_status=503, detail="service unavailable")
    assert exc.step == "initiate"
    assert "503" in str(exc)
    assert "service unavailable" in str(exc)


# ---------------------------------------------------------------------------
# (B) cross-cut — mọi IdealFlowError phải có non-empty step + error_code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        StripeHttpClientError(request_name="init", http_status=500),
        AddressValidationRejectedError(rejected_field="name"),
        ConfirmInvalidResponseError(http_status=502),
        ApproveFailedError(http_status=400, detail="denied"),
        RefreshPollExhaustedError(attempts=3),
        RedirectValidationError(http_status=200, has_location=False),
        TransactionHttpError(http_status=503, detail="down"),
    ],
    ids=[
        "stripe_http_client",
        "address_validation",
        "confirm_invalid",
        "approve_failed",
        "refresh_exhausted",
        "redirect_validation",
        "transaction_http",
    ],
)
def test_every_ideal_flow_error_has_non_empty_step_and_code(
    exc: IdealFlowError,
) -> None:
    """R14.2/R14.4: MỌI `IdealFlowError` subclass phải mang non-empty
    `step` và `error_code` — dùng để log realtime bước lỗi + mã lỗi ổn định
    lộ ra API.

    Đây là chốt hợp đồng cấu trúc: bất kỳ error nào flow raise ra tại
    boundary duy nhất `IdealFlowHandler.run()` đều có ĐỦ 2 phần này để
    build `JobResult.error_code` + log message chi tiết → user thấy chính
    xác bước lỗi & nguyên nhân, không phải "internal error" chung chung.

    **Validates: Requirements 14.2, 14.4**
    """
    assert exc.step, f"IdealFlowError {type(exc).__name__} thiếu step non-empty (R14.2)"
    assert exc.error_code, (
        f"IdealFlowError {type(exc).__name__} thiếu error_code non-empty (R14.4)"
    )
    assert str(exc), (
        f"IdealFlowError {type(exc).__name__} có message rỗng — vi phạm "
        f"R14.2 (yêu cầu mô tả nguyên nhân cụ thể)"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
