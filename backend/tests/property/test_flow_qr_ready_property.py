"""Property test cho `IdealFlowHandler.run()` — Property 21 (Requirement 7.3).

**Property 21: IdealJob chỉ đạt `qr_ready` khi CẢ 2 điều kiện ghi file và
lưu path đều thành công.**

Contract R7.3 nói: `IdealFlowHandler.run()` chuyển 1 IdealJob sang trạng
thái `QR_READY` KHI VÀ CHỈ KHI cả 2 sub-condition sau cùng thoả:

    (a) Ghi file PNG thành công — `QrRenderer.render_png(...)` KHÔNG raise
        `QrRenderError` (encode + I/O đều OK).
    (b) Lưu path vào bản ghi Job thành công — trong implementation hiện tại
        là `str(qr_path)` build ra `JobResult.artifact_path` không raise.

Ghi chú về sub-condition (b): trong implementation `IdealFlowHandler._run_
inner` (tham chiếu `flow.py` step 12), "lưu path" chỉ là `str(qr_path)` với
`qr_path` là `pathlib.Path` — `Path.__str__` KHÔNG raise trong bất kỳ tình
huống nào ở CPython. Do đó Property 21 REDUCE về:

    `qr_ready` iff `render_png` KHÔNG raise QrRenderError.

Test này sinh giá trị `qr_success` bằng `st.booleans()` — đại diện tổ hợp:

    - `qr_success=True`  : (a)=OK ∧ (b)=OK (b auto-OK) → kỳ vọng QR_READY.
    - `qr_success=False` : (a)=FAIL → raise QrRenderError → catch tại
      boundary DUY NHẤT của `IdealFlowHandler.run()` → JobResult(ERROR,
      error_code="qr_render_failed"), KHÔNG có trạng thái trung gian không
      xác định.

Test bypass 11 bước đầu (step 1-11) qua mock/stub sub-client fine-grained:
`ChatgptClient`/`StripeClient`/`TransactionClient` được patch trong module
`app.payments.ideal.flow` bằng fake class trả shape hợp lệ; `SessionCache`/
`SettingsRepository`/`IssuerSelector`/`DeviceProfileAllocator`/`Profile-
Generator` inject stub trực tiếp qua constructor `IdealFlowHandler`.

Sub-condition này KHÔNG cần cover happy path đầy đủ (đã có integration
test riêng ở task 33) — chỉ cần verify boundary QR_READY vs ERROR đúng
theo bit `qr_success`.

**Validates: Requirements 7.3**
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any, Final
from unittest.mock import patch

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import app.payments.ideal.flow as flow_module
from app.core.payment_flow import (
    Job,
    JobStatus,
    SimpleCancellationToken,
)
from tests.support.fake_http import FakeAsyncSession, FakeResponse
from app.payments.ideal.errors import QrRenderError
from app.payments.ideal.models import (
    BillingAddress,
    CheckoutSession,
    DeviceProfile,
    IdealTransactionInitiate,
    IssuerBank,
    SessionBundle,
    StripeElementsSession,
    StripePaymentPageInit,
)

# ---------------------------------------------------------------------------
# Fixture data — cố định để boundary render step là biến DUY NHẤT trong test.
# ---------------------------------------------------------------------------

#: Mã issuer khớp `ideal.default_issuer` — dùng cho stub Settings +
#: stub IssuerSelector.select đảm bảo step 12 chọn được IssuerBank hợp lệ.
_ISSUER_ID: Final[str] = "INGBNL2A"

#: Deeplink giả lập của IssuerBank được chọn — nội dung không quan trọng
#: vì `render_png` bị stub, chỉ giữ non-empty để log realtime hợp lý.
_DEEPLINK: Final[str] = "https://issuer.example/ideal/deeplink"

#: Định danh job stable để artifact path assertion so sánh chính xác.
_JOB_ID: Final[str] = "job-flow-qr-ready"

#: Định danh account cho account_line 2-part — đi nhánh access_token direct
#: trong `_resolve_session`, bypass toàn bộ HTTP login flow của ChatgptClient.
_EMAIL: Final[str] = "user@example.com"
_ACCESS_TOKEN: Final[str] = "test-access-token"

#: `email|access_token` — format 2-part hợp lệ theo `parse_account_line`.
_ACCOUNT_LINE: Final[str] = f"{_EMAIL}|{_ACCESS_TOKEN}"


# ---------------------------------------------------------------------------
# Fake sub-clients — bypass step 1-11 với shape hợp lệ tối thiểu.
# Được patch vào `app.payments.ideal.flow` scope (nơi `_run_inner` instantiate
# `ChatgptClient(...)`, `StripeClient(...)`, `TransactionClient(...)`).
# ---------------------------------------------------------------------------


class _FakeChatgptClient:
    """No-op ChatgptClient — create_checkout + approve trả shape hợp lệ."""

    def __init__(self, **_kwargs: Any) -> None:
        # Ignore mọi kwargs (http_client/session_cache/logger) — không dùng
        # trong test này.
        pass

    async def create_checkout(self, _session: SessionBundle) -> CheckoutSession:
        return CheckoutSession(
            checkout_session_id="cs_test_flow_qr_ready",
            publishable_key="pk_test_flow_qr_ready",
            processor_entity="openai_ie",
        )

    async def snapshot_billing(
        self,
        _session: SessionBundle,
        _billing: BillingAddress,
    ) -> None:
        # HTTP 204 No Content trong production — no-op ở fake.
        return None

    async def approve(
        self,
        _checkout_session_id: str,
        _processor_entity: str,
        _session: SessionBundle,
    ) -> None:
        return None


#: URL redirect giả `pm-redirects.stripe.com` mà `refresh_state` trả trong
#: payload — `extract_redirect_url_from_payload` sẽ trích ra để step 10
#: (`follow_redirect`) tiếp tục. Nội dung không quan trọng (follow_redirect
#: cũng bị fake), chỉ cần non-empty để không rơi vào nhánh exhausted.
_STRIPE_REDIRECT_URL: Final[str] = (
    "https://pm-redirects.stripe.com/authorize/acct_fake/sa_nonce_fake"
)


class _FakeStripeClient:
    """No-op StripeClient — mọi request trả shape hợp lệ, không raise."""

    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def init(
        self,
        _checkout_session_id: str,
        _publishable_key: str,
    ) -> StripePaymentPageInit:
        return StripePaymentPageInit(
            init_checksum="chk_test",
            config_id="cfg_test",
        )

    async def ensure_token_config(self) -> None:
        # Production fetch Stripe.js bundle → StripeTokenConfig, cache 1 lần.
        # Fake bỏ qua fetch; trả `None` — `confirm` prod nhận
        # `token_config: StripeTokenConfig | None = None` nên None là shape
        # hợp lệ caller chấp nhận (không raise).
        return None

    async def elements_sessions(
        self,
        _checkout_session_id: str,
        _publishable_key: str,
        amount: int = 0,  # noqa: ARG002 — flow truyền amount=init_result.amount
    ) -> StripeElementsSession:
        return StripeElementsSession(session_id="stripe_sess_test")

    async def update_billing(
        self,
        _checkout_session_id: str,
        _publishable_key: str,
        _elements_session_id: str,
        _billing: BillingAddress,
    ) -> None:
        # POST tax_region vào payment_page state — HTTP 200, no return value.
        return None

    async def confirm(
        self,
        _checkout_session_id: str,
        _publishable_key: str,
        _elements_session_id: str,
        _billing: BillingAddress,
        **_kwargs: Any,  # init_checksum/amount/*_config_id/page_id/token_config
    ) -> dict[str, Any]:
        # Production trả nguyên payload dict; flow hiện KHÔNG đọc kết quả này
        # (redirect lấy qua refresh_state) nên dict rỗng là đủ.
        return {}

    async def refresh_state(
        self,
        _checkout_session_id: str,
        _publishable_key: str,
        elements_session_id: str = "",  # noqa: ARG002
    ) -> dict[str, Any]:
        # Trả payload có sẵn `setup_intent.next_action.redirect_to_url.url`
        # để `extract_redirect_url_from_payload` trích ngay lần poll đầu.
        return {
            "status": "requires_action",
            "setup_intent": {
                "next_action": {
                    "redirect_to_url": {"url": _STRIPE_REDIRECT_URL},
                },
            },
        }

    @staticmethod
    def extract_redirect_url_from_payload(payload: dict[str, Any]) -> str | None:
        # Mirror logic prod: setup_intent → payment_intent priority.
        for intent_key in ("setup_intent", "payment_intent"):
            intent = payload.get(intent_key)
            if not isinstance(intent, dict):
                continue
            next_action = intent.get("next_action")
            if not isinstance(next_action, dict):
                continue
            redirect_to_url = next_action.get("redirect_to_url")
            if isinstance(redirect_to_url, dict):
                url = redirect_to_url.get("url")
                if url:
                    return url
        return None

    async def follow_redirect(self, _redirect_to_url: str) -> tuple[str, str]:
        return ("encoded_tx_url_test", "sig_test")


class _FakeTransactionClient:
    """No-op TransactionClient — trả IdealTransactionInitiate với đúng 1
    IssuerBank khả dụng khớp `_ISSUER_ID` để step 12 chọn được deeplink.
    """

    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def initiate(
        self,
        _encoded_tx_url: str,
        _sig: str,
        _device_profile: DeviceProfile,
    ) -> IdealTransactionInitiate:
        return IdealTransactionInitiate(
            view="INITIAL_VIEW",
            amount="10.99",
            creditorName="Fake Creditor",
            supportedIssuers=[
                IssuerBank(
                    id=_ISSUER_ID,
                    deeplinkType="IOS",
                    deeplink=_DEEPLINK,
                    availabilityStatus="AVAILABLE",
                ),
            ],
            # `qrCodeUrl` bắt buộc non-empty ở step 12 — thiếu → flow raise
            # IdealFlowError(qr_code_url_missing) trước cả bước render QR.
            qrCodeUrl="https://tx.ideal.nl/2/tx_fake?sig=sig_test",
        )


# ---------------------------------------------------------------------------
# Stubs cho dependency inject qua constructor `IdealFlowHandler`.
# ---------------------------------------------------------------------------


class _StubSessionCache:
    """AccountSessionCache stub — mọi method trả None (cache miss).

    Ép `_resolve_session` đi nhánh access_token direct (bypass login HTTP
    thật) vì `_ACCOUNT_LINE` là format 2-part.
    """

    async def get(self, _account_key: str) -> None:
        return None

    async def save(self, _account_key: str, _payload: dict) -> None:
        return None

    async def clear(self, _account_key: str) -> None:
        return None


class _StubSettings:
    """SettingsRepository stub — chỉ hỗ trợ `ideal.default_issuer`.

    Step 12 gọi `self._settings.get("ideal.default_issuer")` để lấy mã
    issuer target — stub trả `_ISSUER_ID` để `IssuerSelector.select` khớp.
    """

    async def get(self, key: str) -> Any | None:
        if key == "ideal.default_issuer":
            return _ISSUER_ID
        return None


class _StubProfileGenerator:
    """IdealProfileGenerator stub — trả BillingAddress locale NL cứng."""

    def generate(self, email: str) -> BillingAddress:
        return BillingAddress(
            name="Test User",
            email=email,
            address={
                "country": "NL",
                "line1": "Teststraat 1",
                "line2": "",
                "city": "Amsterdam",
                "postal_code": "1000AA",
                "state": "",
            },
        )


class _StubDeviceProfileAllocator:
    """DeviceProfileAllocator stub — trả DeviceProfile locale NL cứng.

    KHÔNG cache theo job_id (test không quan tâm) — trả instance mới mỗi
    lần cũng OK vì test chỉ cover step 12 boundary.
    """

    async def pick_for_job(self, _job_id: str) -> DeviceProfile:
        return DeviceProfile(
            language="nl-NL",
            timeZone="Europe/Amsterdam",
            screenWidth=1920,
            screenHeight=1080,
            screenAvailableWidth=1920,
            screenAvailableHeight=1040,
            colorDepth=24,
        )


class _StubIssuerSelector:
    """IssuerSelector stub — trả phần tử khớp `default_issuer_id` (naïve).

    `TransactionClient.initiate` stub trả đúng 1 IssuerBank khớp `_ISSUER_
    ID` nên `.select` luôn tìm thấy — không raise `NoIssuerAvailableError`.
    """

    def select(
        self, issuers: list[IssuerBank], default_issuer_id: str
    ) -> IssuerBank:
        for issuer in issuers:
            if issuer.id == default_issuer_id:
                return issuer
        # Fail-fast defensive — không nên xảy ra với fake TransactionClient
        # ở trên.
        raise AssertionError(
            f"_StubIssuerSelector: không tìm thấy issuer khớp "
            f"default_issuer_id={default_issuer_id!r} trong {issuers!r}"
        )


class _StubQrRenderer:
    """QrRenderer stub controllable — hành vi phụ thuộc `render_should_succeed`.

    - `True`  → return `_target_path` (Path đã cố định trước) — mô phỏng
      "ghi file PNG OK".
    - `False` → raise `QrRenderError(reason="simulated encode fail")` —
      mô phỏng "ghi file PNG FAIL" ở R7.6, IdealFlowHandler.run() phải
      catch tại boundary DUY NHẤT → JobResult(ERROR).
    """

    def __init__(self, render_should_succeed: bool, target_path: Path) -> None:
        self._render_should_succeed = render_should_succeed
        self._target_path = target_path

    def render_png(
        self, _deeplink: str, _job_id: str, _output_dir: Path
    ) -> Path:
        if self._render_should_succeed:
            return self._target_path
        raise QrRenderError(reason="simulated encode fail")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_handler(
    qr_success: bool,
    target_path: Path,
    output_dir: Path,
) -> flow_module.IdealFlowHandler:
    """Dựng `IdealFlowHandler` với stub dependency (bypass step 1-11)."""
    return flow_module.IdealFlowHandler(
        settings=_StubSettings(),  # type: ignore[arg-type]
        session_cache=_StubSessionCache(),  # type: ignore[arg-type]
        profile_generator=_StubProfileGenerator(),  # type: ignore[arg-type]
        device_profile_allocator=_StubDeviceProfileAllocator(),  # type: ignore[arg-type]
        issuer_selector=_StubIssuerSelector(),  # type: ignore[arg-type]
        qr_renderer=_StubQrRenderer(qr_success, target_path),  # type: ignore[arg-type]
        logger_factory=logging.getLogger,
        qr_output_dir=output_dir,
    )


def _build_job() -> Job:
    """Job 2-part format — bypass login qua nhánh access_token direct."""
    return Job(
        job_id=_JOB_ID,
        payment_method="ideal",
        account_line=_ACCOUNT_LINE,
        created_at=0.0,
        cancellation_token=SimpleCancellationToken(),
    )


# ---------------------------------------------------------------------------
# Property 21 — QR_READY iff (render_png OK ∧ str(qr_path) OK).
# ---------------------------------------------------------------------------


@settings(
    # 10 example đủ vì domain input là boolean (2 giá trị) — hypothesis
    # exhaustively cover cả True và False.
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(qr_success=st.booleans())
def test_flow_qr_ready_iff_render_and_store_both_succeed(
    qr_success: bool,
) -> None:
    """Property 21: `IdealFlowHandler.run()` trả `JobResult(status=QR_READY)`
    KHI VÀ CHỈ KHI cả `render_png` VÀ lưu path đều thành công. Mọi tổ hợp
    còn lại trả `JobResult(status=ERROR, error_code="qr_render_failed")`.

    Trong implementation hiện tại, "lưu path" là `str(qr_path)` — không thể
    fail (Path.__str__ không raise) nên property collapse về `qr_success`.

    **Validates: Requirements 7.3**
    """
    with tempfile.TemporaryDirectory() as tmp_dir_str:
        output_dir = Path(tmp_dir_str)
        target_path = output_dir / f"{_JOB_ID}.png"
        handler = _build_handler(qr_success, target_path, output_dir)
        job = _build_job()

        # Patch 3 sub-client class refs bên trong module `flow` — tại thời
        # điểm `_run_inner` gọi `ChatgptClient(...)`, `StripeClient(...)`,
        # `TransactionClient(...)`, các ref này đã được thay bằng fake.
        # KHÔNG patch class gốc trong module `chatgpt_client`/`stripe_client`/
        # `transaction_client` vì `flow.py` đã import bằng `from X import Y`
        # (bind local ref) — chỉ cần patch trong scope `flow_module`.
        # Step 11a của flow tạo 1 fresh HTTP client (`http.create_async_client`)
        # để GET `pay.ideal.nl/transactions/...` lấy cookie tx_api_token —
        # đây là network call THẬT không đi qua sub-client được patch. Stub
        # factory trả FakeAsyncSession (GET → 200) để bypass, giữ boundary
        # render QR (step 12) là biến DUY NHẤT của property.
        def _fake_client_factory(**_kwargs: Any) -> FakeAsyncSession:
            return FakeAsyncSession(
                default_handler=lambda _call: FakeResponse(200, text="ok")
            )

        with (
            patch.object(flow_module, "ChatgptClient", _FakeChatgptClient),
            patch.object(flow_module, "StripeClient", _FakeStripeClient),
            patch.object(flow_module, "TransactionClient", _FakeTransactionClient),
            patch.object(
                flow_module.http, "create_async_client", _fake_client_factory
            ),
        ):
            result = asyncio.run(handler.run(job, None))

        if qr_success:
            # (a)=OK ∧ (b)=OK → QR_READY. `artifact_path` phải là str của
            # path do renderer trả về (chứng minh path đã "lưu" vào JobResult).
            assert result.status == JobStatus.QR_READY, (
                f"qr_success=True (render_png trả path hợp lệ) phải cho "
                f"status=QR_READY, nhận: {result.status!r} "
                f"(error_code={result.error_code!r}, "
                f"error_message={result.error_message!r})"
            )
            assert result.artifact_path == str(target_path), (
                f"QR_READY phải mang artifact_path=str(qr_path) do renderer "
                f"trả về. Kỳ vọng: {str(target_path)!r}, "
                f"nhận: {result.artifact_path!r}"
            )
            assert result.error_code is None, (
                f"QR_READY phải có error_code=None, "
                f"nhận: {result.error_code!r}"
            )
            assert result.error_message is None, (
                f"QR_READY phải có error_message=None, "
                f"nhận: {result.error_message!r}"
            )
        else:
            # (a)=FAIL → QrRenderError → catch tại boundary DUY NHẤT →
            # JobResult(ERROR, error_code="qr_render_failed"). KHÔNG được
            # rơi vào trạng thái trung gian không xác định (QR_READY hoặc
            # bất kỳ status nào khác đều là bug R7.3).
            assert result.status == JobStatus.ERROR, (
                f"qr_success=False (render_png raise QrRenderError) phải "
                f"cho status=ERROR, nhận: {result.status!r}"
            )
            assert result.error_code == "qr_render_failed", (
                f"ERROR do render_png fail phải mang error_code="
                f"'qr_render_failed' (hợp đồng ổn định của QrRenderError → "
                f"JobResult), nhận: {result.error_code!r}"
            )
            assert result.artifact_path is None, (
                f"ERROR không được kèm artifact_path (chưa lưu file thành "
                f"công), nhận: {result.artifact_path!r}"
            )
