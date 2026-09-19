"""IdealFlowHandler — orchestrator 12-step luồng iDEAL (`payments/ideal/`).

Class `IdealFlowHandler` implement `PaymentFlowHandler` (`app.core.payment_flow`),
được `payments/ideal/__init__.py` (task 20.3) đăng ký vào `JobManager` qua
`register_handler("ideal", handler)`.

Luồng đi qua 12 bước theo Requirements 1–7:

    1. Parse `job.account_line` → `IdealParsedAccount` (R1.1).
    2. Resolve session — thử cache (R10.4), fallback login (R1.3) hoặc dùng
       access_token trực tiếp (R1.1 định dạng 2-part).
    3. `ChatgptClient.create_checkout` (R1.5-1.9).
    4. `StripeClient.init` (R2.1-2.2).
    5. `StripeClient.elements_sessions` (R2.3-2.4).
    6. `IdealProfileGenerator.generate` (R3.1).
    7. `StripeClient.confirm` (R3.2-3.7).
    8. `ChatgptClient.approve` (R4.1-4.2).
    9. `StripeClient.refresh_poll` (R4.3-4.6).
   10. `StripeClient.follow_redirect` (R4.7-4.9).
   11. `DeviceProfileAllocator.pick_for_job` + `TransactionClient.initiate`
       (R5.1-5.9).
   12. Đọc `ideal.default_issuer` → `IssuerSelector.select` (R6.2, R6.3) →
       `QrRenderer.render_png` (R7.1-7.3).

Fail_Fast_Policy tại boundary DUY NHẤT (R14.2, R14.3): mọi `IdealFlowError`
subclass phát sinh trong 12 bước đều được catch tại 1 try/except cuối method
`run()`, map sang `JobResult(status=ERROR, error_code=e.error_code,
error_message=redact_message(str(e), known_secrets))`. Exception ngoài
hierarchy `IdealFlowError` (bug nội tại chưa lường trước) được để propagate
lên `JobManager._run_handler` — KHÔNG catch-all ở boundary iDEAL.

Cancellation (R8.6): kiểm tra `job.cancellation_token.is_cancelled()` TRƯỚC
mỗi bước. Nếu cancelled → return `JobResult(status=STOPPED)` ngay lập tức.
`asyncio.CancelledError` (task bị cancel từ ngoài) cũng map thành `STOPPED`.

QR ready (R7.3): CHỈ chuyển `qr_ready` khi CẢ ghi file PNG (`qr_renderer.
render_png` thành công) VÀ lưu path (return trong `JobResult.artifact_path`)
đều thành công. Nếu `render_png` raise `QrRenderError` → catch tại boundary
duy nhất → ERROR.

R7.7: KHÔNG gọi bất kỳ request nào sau khi đạt `qr_ready` — cấu trúc tuần
tự 12 bước tự đảm bảo (không có bước nào sau `render_png` cuối method).

Sensitive_Data_Redaction (R1.9, R3.6, R4.9, R14.8): mọi log áp dụng
`redact_dict()` cho dict payload và `redact_message()` cho chuỗi tự do với
`known_secrets = [password, totp_secret, access_token, sig]`. Danh sách
`known_secrets` được tích luỹ động khi giá trị nhạy cảm được phát hiện
(parse account line, resolve session, follow_redirect).

Per-job `curl_cffi.AsyncSession` (R9.5, R9.7): mỗi lần `run()` khởi tạo 1
session riêng qua `app.core.http_client.create_async_client()`. Session
này set `impersonate` theo `DEFAULT_IMPERSONATE` ở `http_client.py`
(hiện Chrome 136 desktop) để giả TLS/JA3/H2 fingerprint Chrome thật —
bypass Cloudflare 403 (chrome131 bị block từ 2026-07). Nếu `proxy_lease` non-None → cấu hình
`proxy=proxy_lease.materialized_url`; nếu `None` chạy Direct_Mode. Session
được đóng qua `async with` để giải phóng connection pool ngay khi job kết thúc.

_Requirements: 1.9, 2.8, 3.7, 4.10, 5.9, 6.1, 7.3, 7.7, 7.8, 13.4_
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Final

from app.core import http_client as http
from app.core.payment_flow import (
    AccountLineError,
    Job,
    JobResult,
    JobStatus,
    ParsedAccount,
    PaymentFlowHandler,
)
from app.core.proxy_format import materialize_proxy
from app.core.proxy_pool import ProxyLease
from app.core.redaction import redact_dict, redact_message
from app.core.session_cache import AccountSessionCache
from app.core.settings_store import SettingsRepository
from app.payments._chatgpt.errors import ChatgptLoginError
from app.payments._chatgpt.session import (
    resolve_session as _resolve_session_fn,
    _safe_build_session_from_cache as _safe_build_session_from_cache_fn,
)
from app.payments.ideal.chatgpt_client import ChatgptClient
from app.payments.ideal.device_profile import DeviceProfileAllocator
from app.payments.ideal.errors import (
    IdealFlowError,
    NoIssuerAvailableError,
)
from app.payments.ideal.issuer_selector import IssuerSelector
from app.payments.ideal.models import (
    IdealParsedAccount,
    SessionBundle,
    parse_account_line as _parse_account_line,
)
from app.payments.ideal.profile_generator import IdealProfileGenerator
from app.payments.ideal.qr_renderer import QrRenderer
from app.payments.ideal.stripe_client import StripeClient
from app.payments.ideal.transaction_client import TransactionClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Settings key chứa `id` issuer mặc định người dùng chọn (R6.2). Đọc bằng
#: full-qualified key (`namespace.field`) — namespace `ideal.*` do
#: `payments/ideal/__init__.py` (task 20.3) đăng ký vào Settings_Store.
_SETTING_DEFAULT_ISSUER: Final[str] = "ideal.default_issuer"

#: Settings key cho concurrency (Requirement 8.4) — trả về từ
#: `get_max_concurrent_key()` để `JobManager` đọc mà không hardcode.
_SETTING_MAX_CONCURRENT: Final[str] = "ideal.max_concurrent"

#: Độ dài phần đầu của SHA256 dùng làm `account_key` (Requirement 10 —
#: cache key). 32 hex đủ để tránh collision thực tế, KHÔNG lưu email thô.
_ACCOUNT_KEY_HEX_LENGTH: Final[int] = 32

#: Kết quả STOPPED chung — dùng lại instance thay vì tạo mới mỗi lần
#: cancellation check để giảm garbage.
_STOPPED_RESULT: Final[JobResult] = JobResult(status=JobStatus.STOPPED)

#: Kết quả PAUSED — trả về khi Push_Success_Gate paused và handler đã đi
#: qua checkpoint an toàn (login xong, cache session đã persist). Wrapper
#: `JobManager._run_handler` phát hiện `pause_requested=True` → chuyển
#: `record.status = PENDING` thay vì terminal STOPPED. Khi user bấm
#: "Tiếp tục", scheduler dispatch lại → step 2 (resolve session) hit
#: cache → SKIP login, chạy thẳng vào step 3.
_PAUSED_RESULT: Final[JobResult] = JobResult(
    status=JobStatus.STOPPED, pause_requested=True
)


def _check_stop(job: Job) -> "JobResult | None":
    """Chỉ check user cancel — dùng ở step 1 (parse) và step 2 (login).

    KHÔNG check pause tại đây để đảm bảo login (nếu cần) chạy xong tự
    nhiên và persist cache session TRƯỚC khi cho phép pause. Khi resume,
    step 2 sẽ dùng cache thay vì login lại.

    Returns:
        `_STOPPED_RESULT` nếu user đã cancel, None nếu tiếp tục.
    """
    if job.cancellation_token.is_cancelled():
        return _STOPPED_RESULT
    return None


def _check_stop_or_pause(job: Job) -> "JobResult | None":
    """Check cancel + pause — dùng từ step 3 trở đi (SAU khi login xong).

    Ưu tiên `is_cancelled` vì cancel mạnh hơn (user chủ động dừng hẳn).
    Nếu chỉ pause → return `_PAUSED_RESULT` với `pause_requested=True`
    để wrapper chuyển record về PENDING chờ user resume.

    Returns:
        `_STOPPED_RESULT` (user cancel), `_PAUSED_RESULT` (gate paused),
        hoặc None (tiếp tục chạy).
    """
    if job.cancellation_token.is_cancelled():
        return _STOPPED_RESULT
    if job.cancellation_token.is_paused():
        return _PAUSED_RESULT
    return None

#: Timeout wall-clock mặc định (giây) cho các HTTP request KHÔNG có timeout
#: riêng đọc từ Settings (chatgpt_client, transaction_client). Stripe client
#: tự đọc `ideal.stripe_request_timeout_seconds` live từ Settings — không
#: dùng hằng này.
_DEFAULT_HTTP_TIMEOUT_SECONDS: Final[float] = 30.0

#: Poll `refresh_state` tối đa n lần trước khi Fail_Fast vì Stripe chưa
#: attach `setup_intent.next_action.redirect_to_url.url` (R4.3, R4.6). Với
#: initial delay 0.5s + backoff 1.5x cap 3s → tổng ~30s worst-case.
_REDIRECT_POLL_MAX_ATTEMPTS: Final[int] = 15
_REDIRECT_POLL_INITIAL_DELAY_SECONDS: Final[float] = 0.5
_REDIRECT_POLL_MAX_DELAY_SECONDS: Final[float] = 3.0

#: Default HTTP headers cho per-job session — HẦU HẾT được `impersonate`
#: (Chrome desktop hiện tại, xem `DEFAULT_IMPERSONATE`) set tự động
#: (User-Agent, Sec-CH-UA*, Sec-Fetch-*, Accept, Accept-Encoding gồm
#: Brotli). Chỉ giữ lại các header override tối thiểu mà `impersonate`
#: không set hoặc set khác với luồng iDEAL cần:
#:
#: - ``Accept-Language``: default của Chrome là `en-US,en;q=0.9`, thêm
#:   `nl` để `pay.ideal.nl` trả UI Dutch (giữ nguyên hành vi cũ).
#:
#: Client code (`ChatgptClient`, `StripeClient`, `TransactionClient`) vẫn có
#: quyền override từng request qua kwarg `headers=` — merge với default.
_DEFAULT_HTTP_HEADERS: Final[dict[str, str]] = {
    "Accept-Language": "en-US,en;q=0.9,nl;q=0.8",
}


# ---------------------------------------------------------------------------
# Stripe logger adapter — bridge từ kwargs-style API (_SupportsInfoLog) sang
# vanilla logging.Logger (format string + redact known_secrets).
# ---------------------------------------------------------------------------


class _StripeLoggerAdapter:
    """Adapter cho `StripeClient._SupportsInfoLog` sang `logging.Logger`.

    `StripeClient` gọi `logger.info(event, request=..., attempt=..., ...)` —
    kwargs không tương thích với `logging.Logger.info(msg, *args, **kwargs)`
    của stdlib (stdlib reserve `extra=`/`exc_info=`/... nên kwargs ngẫu
    nhiên sẽ TypeError). Adapter này:

    1. Redact `kwargs` qua `redact_dict()` để mask field nhạy cảm theo tên
       (R3.6 — `js_checksum`/`rv_timestamp`/`passive_captcha_token`).
    2. Redact string cuối qua `redact_message()` với `known_secrets` để mask
       theo GIÁ TRỊ (R4.9 — `sig` cụ thể của job hiện tại).
    3. Chuyển thành format string đơn giản `"{event} key1=val1 key2=val2"`
       rồi gọi `logging.Logger.info(...)`.

    Reference tới `known_secrets` là list mutable share với `IdealFlowHandler.
    run()` — khi flow phát hiện thêm secret (ví dụ `sig` sau
    `follow_redirect`), append vào list và adapter tự động dùng list mới
    nhất cho log tiếp theo (không cần construct lại adapter).
    """

    def __init__(
        self,
        underlying: logging.Logger,
        known_secrets: list[str],
    ) -> None:
        self._logger = underlying
        self._known_secrets = known_secrets

    def info(self, event: str, /, *args: Any, **kwargs: Any) -> None:
        """Chấp nhận CẢ 2 kiểu gọi mà `stripe_client` đang dùng lẫn lộn:

        - **printf-style** (giống `logging.Logger.info`):
            ``logger.info("stripe %s ok status=%d", request_name, status)``
          → format `event % args` trước khi log.

        - **kwargs-style** (event + key=val):
            ``logger.info("stripe request fail_fast", request=..., attempt=...)``
          → format thành `"event key1=val1 key2=val2"`.

        Cả 2 kiểu có thể xuất hiện trong cùng 1 file (`stripe_client.py`)
        theo lịch sử code. Adapter merge cả 2 để không phải sửa 20+ call
        site — logger.info phía dưới nhận string đã format sẵn nên stdlib
        `logging` xử lý thuần as-is.
        """
        # (1) Positional format nếu có args (printf-style).
        if args:
            try:
                formatted_event = event % args
            except (TypeError, ValueError):
                # Format string sai / thừa/thiếu placeholder — không crash
                # flow chỉ vì log; fallback ghép args nguyên vẹn.
                formatted_event = f"{event} args={args!r}"
        else:
            formatted_event = event

        # (2) Redact kwargs theo tên field.
        redacted_kwargs = redact_dict(kwargs) if kwargs else {}
        if redacted_kwargs:
            kv_parts = " ".join(f"{k}={v!r}" for k, v in redacted_kwargs.items())
            raw_message = f"[stripe] {formatted_event} {kv_parts}"
        else:
            raw_message = f"[stripe] {formatted_event}"

        # (3) Redact theo giá trị (known_secrets).
        message = redact_message(raw_message, self._known_secrets)
        self._logger.info(message)


# ---------------------------------------------------------------------------
# IdealFlowHandler
# ---------------------------------------------------------------------------


class IdealFlowHandler:
    """Orchestrator 12-step luồng iDEAL — implement `PaymentFlowHandler`.

    Attributes:
        _settings: Nguồn cấu hình runtime (Settings_Store, R11) — dùng để
            đọc `ideal.default_issuer` (R6.2) và inject xuống `StripeClient`
            đọc `ideal.stripe_*`/`ideal.refresh_poll_*` live.
        _session_cache: Cache session per-account (R10) — thử `get` trước
            khi login, `save` sau khi login thành công, `clear` khi
            revalidate fail.
        _profile_generator: Sinh `BillingAddress` locale NL (R3.1).
        _device_profile_allocator: Cấp `DeviceProfile` cố định trong vòng
            đời 1 IdealJob (R5.1-5.3).
        _issuer_selector: Chọn `IssuerBank` từ `supportedIssuers` khớp
            `ideal.default_issuer` (R6.2-6.3).
        _qr_renderer: Vẽ QR PNG thuần từ deeplink (R7.1-7.3).
        _logger_factory: Factory tạo logger per-job — inject để test dễ
            thay bằng adapter hoặc JobLogger (SSE broadcast) mà không cần
            patch logging module.
        _qr_output_dir: Thư mục runtime ghi file QR PNG (R7.3) — tên file
            = `<job_id>.png`, tránh collision cross-job.
    """

    def __init__(
        self,
        settings: SettingsRepository,
        session_cache: AccountSessionCache,
        profile_generator: IdealProfileGenerator,
        device_profile_allocator: DeviceProfileAllocator,
        issuer_selector: IssuerSelector,
        qr_renderer: QrRenderer,
        logger_factory: Callable[[str], logging.Logger],
        qr_output_dir: Path,
    ) -> None:
        self._settings = settings
        self._session_cache = session_cache
        self._profile_generator = profile_generator
        self._device_profile_allocator = device_profile_allocator
        self._issuer_selector = issuer_selector
        self._qr_renderer = qr_renderer
        self._logger_factory = logger_factory
        self._qr_output_dir = qr_output_dir

    @property
    def device_profile_allocator(self) -> DeviceProfileAllocator:
        """Expose allocator cho `bootstrap.py` đăng ký `JobManager.register_job_removed_hook`.

        `DeviceProfileAllocator._cache` (job_id → DeviceProfile, R5.1)
        sống suốt đời process và KHÔNG tự bounded — nếu không được
        `forget(job_id)` mỗi khi job bị xoá khỏi `JobManager`, cache này
        phình vô hạn theo tổng số job đã TỪNG chạy (memory leak). Property
        này là điểm truy cập DUY NHẤT để wiring code (bootstrap) lấy
        allocator mà không cần biết cấu trúc nội bộ `IdealFlowHandler`.
        """
        return self._device_profile_allocator

    # ------------------------------------------------------------------
    # PaymentFlowHandler protocol methods
    # ------------------------------------------------------------------

    def parse_account_line(self, line: str) -> ParsedAccount | AccountLineError:
        """Delegate sang module-level `parse_account_line` của `models.py`.

        `JobManager.submit_batch` gọi method này trước khi tạo Job để lọc
        dòng account bất hợp lệ (R8.2). Trong `run()`, method này được
        gọi LẠI (task 20.1 note) để lấy `IdealParsedAccount` đầy đủ —
        `JobManager` chỉ giữ `account_line` raw trong `Job`.
        """
        return _parse_account_line(line)

    def get_max_concurrent_key(self) -> str:
        """Trả về full-qualified key `"ideal.max_concurrent"` cho JobManager.

        `JobManager` (thuộc `core/`) đọc `max_concurrent` qua callback này
        thay vì hardcode tên key theo namespace payment method — đảm bảo
        Payment_Module_Boundary (R13.2, R13.5).
        """
        return _SETTING_MAX_CONCURRENT

    def get_account_dedup_key(
        self, parsed: "ParsedAccount"
    ) -> str | None:
        """Trả dedup key duy nhất cho account trong luồng iDEAL — email lowercase.

        Được `JobManager.submit_batch` gọi (duck-typed optional) để thực thi
        "1 email = 1 job": nếu account trong batch trùng dedup_key với job
        hiện có, JobManager reuse job cũ (reset về pending) thay vì tạo
        job mới. Trả `None` nếu `parsed` không phải `IdealParsedAccount`
        (fail-safe khi có kiểu lạ lọt vào — không dedup, tạo job mới).
        """
        if not isinstance(parsed, IdealParsedAccount):
            return None
        return parsed.email.strip().lower() or None

    # ------------------------------------------------------------------
    # Main orchestrator — 12 steps
    # ------------------------------------------------------------------

    async def run(self, job: Job, proxy_lease: ProxyLease | None) -> JobResult:
        """Thực thi toàn bộ 12 bước cho 1 IdealJob.

        Kiểm tra `cancellation_token` TRƯỚC mỗi bước — bị cancel → return
        `JobResult(status=STOPPED)` (R8.6).

        Bắt `IdealFlowError` tại boundary DUY NHẤT ở cuối method (R14.2,
        R14.3) → `JobResult(status=ERROR, error_code=..., error_message=
        redact_message(..., known_secrets))`. Exception ngoài hierarchy
        `IdealFlowError` để propagate lên `JobManager._run_handler` xử lý.

        Args:
            job: Job đã có `cancellation_token` (`SimpleCancellationToken`)
                và `account_line` raw từ user input.
            proxy_lease: ProxyLease do `JobManager` acquire trước khi gọi
                `run()`. `None` = Direct_Mode (R9.2).

        Returns:
            `JobResult`:
                - `QR_READY` với `artifact_path` = path file PNG khi cả
                  render_png VÀ return đều thành công (R7.3).
                - `ERROR` với `error_code`/`error_message` khi 1
                  `IdealFlowError` subclass được bắt tại boundary.
                - `STOPPED` khi `cancellation_token` báo cancel hoặc
                  `asyncio.CancelledError`.
        """
        logger = self._logger_factory(f"ideal.flow.{job.job_id}")
        # `known_secrets` tích luỹ động — mỗi lần tìm ra 1 secret mới
        # (password/totp/access_token/sig) append vào list. Adapter Stripe
        # share reference tới list này, KHÔNG cần rebuild adapter khi list
        # thay đổi.
        known_secrets: list[str] = []

        # Xây dựng `curl_cffi.AsyncSession` per-job qua facade.
        # `allow_redirects=True` mặc định cho phần lớn request; step 10
        # (`follow_redirect`) tự override `allow_redirects=False` khi gọi
        # cụ thể trong stripe_client (đã xử lý bên trong
        # `StripeClient.follow_redirect`).
        client_kwargs: dict[str, Any] = {
            "allow_redirects": True,
            "timeout": _DEFAULT_HTTP_TIMEOUT_SECONDS,
            # Header override tối thiểu — impersonate (Chrome desktop) lo
            # phần lớn (UA/Sec-CH-UA/Sec-Fetch/Accept/Accept-Encoding).
            "headers": dict(_DEFAULT_HTTP_HEADERS),
        }
        proxy_url: str | None = None
        if proxy_lease is not None:
            # `materialized_url` giữ RAW LINE người dùng nhập (có thể là
            # colon-form `host:port:user:pass`, credential-at form, hoặc
            # template `{SID}`). `materialize_proxy` chuẩn hoá thành URL
            # đầy đủ scheme://user:pass@host:port + URL-encode credential
            # + gen SID ngẫu nhiên nếu template. Format rác → ValueError
            # propagate lên `_run_handler` map thành ERROR job (Fail_Fast).
            proxy_url = materialize_proxy(proxy_lease.materialized_url)
            client_kwargs["proxy"] = proxy_url
            # Log URL đã materialize (đã mask credential) — sau `flow start`
            # log có `proxy_id` (raw line masked); dòng này bổ sung URL cuối
            # cùng được feed cho HTTP client sau khi thay `{SID}` + URL-encode để
            # dev đối chiếu 1-1 với network request tiếp theo.
            from app.core.proxy_format import mask_proxy as _mask_proxy
            logger.info(
                "proxy materialized: job_id=%s proxy=%s",
                job.job_id,
                _mask_proxy(proxy_url),
            )

        try:
            async with http.create_async_client(**client_kwargs) as http_client:
                return await self._run_inner(
                    job=job,
                    proxy_lease=proxy_lease,
                    proxy_url=proxy_url,
                    http_client=http_client,
                    logger=logger,
                    known_secrets=known_secrets,
                )
        except (IdealFlowError, ChatgptLoginError) as exc:
            # Boundary DUY NHẤT cho lỗi domain iDEAL (R14.2, R14.3, R14.4).
            # `ChatgptLoginError` (raised by the shared login surface) is not an
            # `IdealFlowError` (anti-circular-import invariant) but exposes the
            # same `error_code="login_failed"` / `step="login"` — mapped here
            # identically so login failures still end as
            # `JobResult(ERROR, error_code="login_failed")`.
            error_message = redact_message(str(exc), known_secrets)
            logger.warning(
                "flow error: job_id=%s step=%s error_code=%s message=%s",
                job.job_id,
                exc.step,
                exc.error_code,
                error_message,
            )
            return JobResult(
                status=JobStatus.ERROR,
                error_code=exc.error_code,
                error_message=error_message,
            )
        except asyncio.CancelledError:
            # Task bị cancel từ ngoài (ví dụ JobManager shutdown). Trả STOPPED
            # rồi để propagate — cách trả về này KHÔNG che CancelledError khỏi
            # scheduler, vì trong Python việc `return` trong except
            # CancelledError không re-raise. JobManager._run_handler xử lý
            # trạng thái cancel qua path riêng nếu cần.
            logger.info("flow cancelled: job_id=%s", job.job_id)
            return _STOPPED_RESULT

    # ------------------------------------------------------------------
    # Internal: 12-step body (tách khỏi `run()` để boundary try/except gọn)
    # ------------------------------------------------------------------

    async def _run_inner(
        self,
        *,
        job: Job,
        proxy_lease: ProxyLease | None,
        proxy_url: str | None,
        http_client: http.AsyncSession,
        logger: logging.Logger,
        known_secrets: list[str],
    ) -> JobResult:
        """Thân 12 bước của `run()` — bọc bởi `try/except IdealFlowError` ở caller.

        Tách hàm inner để:
            1. Body flow không lồng thêm 1 tầng indent nữa của `async with`
               `curl_cffi.AsyncSession` — dễ đọc 12 bước tuần tự.
            2. `return` trong bất kỳ bước nào đều tự đóng client qua
               `async with` ở caller (fan-out cleanup).
        """
        # -----------------------------------------------------------------
        # Step 1: Parse account line → IdealParsedAccount (R1.1).
        # -----------------------------------------------------------------
        if job.cancellation_token.is_cancelled():
            return _STOPPED_RESULT

        parsed = _parse_account_line(job.account_line)
        if isinstance(parsed, AccountLineError):
            # JobManager đã filter dòng lỗi ở submit_batch — vào tới đây
            # nghĩa là account_line từng valid, nhưng phòng vệ vẫn trả
            # ERROR có `error_code` ổn định.
            return JobResult(
                status=JobStatus.ERROR,
                error_code="invalid_account_line",
                error_message=f"[parse_account_line] {parsed.reason}",
            )
        # `parsed` type-narrowed thành IdealParsedAccount ở nhánh này.
        assert isinstance(parsed, IdealParsedAccount)

        # Bổ sung secret vào `known_secrets` NGAY sau khi parse — mọi log
        # từ điểm này trở đi tự động redact `password`/`totp_secret`/
        # `access_token` nếu vô tình bị nhúng vào string log.
        if parsed.password:
            known_secrets.append(parsed.password)
        if parsed.totp_secret:
            known_secrets.append(parsed.totp_secret)
        if parsed.access_token:
            known_secrets.append(parsed.access_token)

        # 1 dòng flow start — email đủ để user match với batch input,
        # mode direct/proxied cho biết có proxy hay không (proxy_id chi
        # tiết đã có ở log `proxy_acquired` từ JobManager).
        logger.info(
            "flow start email=%s mode=%s",
            parsed.email,
            "direct" if proxy_lease is None else "proxied",
        )

        # Sub-clients per-job (share `http_client` + `logger` + `known_secrets`).
        chatgpt_client = ChatgptClient(
            http_client=http_client,
            session_cache=self._session_cache,
            logger=logger,
        )
        stripe_client = StripeClient(
            http_client=http_client,
            settings=self._settings,
            logger=_StripeLoggerAdapter(logger, known_secrets),
            # Pass `proxy_url` để `follow_redirect` fresh-client cũng đi
            # qua proxy — tránh leak IP thật ra `pm-redirects.stripe.com`
            # trong Proxied_Mode.
            proxy_url=proxy_url,
        )
        transaction_client = TransactionClient(
            http_client=http_client,
            logger=logger,
        )

        # -----------------------------------------------------------------
        # Step 2: Resolve session — cache first (R10.4), fallback login (R1.3)
        # hoặc dùng access_token trực tiếp (R1.1 định dạng 2-part).
        # -----------------------------------------------------------------
        if job.cancellation_token.is_cancelled():
            return _STOPPED_RESULT

        account_key = self._compute_account_key(parsed.email)
        session = await self._resolve_session(
            chatgpt_client=chatgpt_client,
            parsed=parsed,
            account_key=account_key,
            logger=logger,
        )
        # Access_token của session có thể mới xuất hiện từ login (nếu parse
        # là 3-part format) — thêm vào `known_secrets` để redact log Stripe/
        # transaction/... về sau. `if not in` tránh duplicate khi parse
        # đã có sẵn access_token trùng session.
        if session.access_token and session.access_token not in known_secrets:
            known_secrets.append(session.access_token)

        # -----------------------------------------------------------------
        # Step 3: Create checkout (R1.5-1.9).
        # -----------------------------------------------------------------
        # Từ Step 3 trở đi (SAU khi step 2 đã login xong và cache session
        # đã persist qua `AccountSessionCache.save`), check thêm
        # `is_paused()` để hỗ trợ Push_Success_Gate — user bấm "Tiếp tục"
        # sẽ chạy lại từ đầu, step 2 hit cache → skip login.
        _stop = _check_stop_or_pause(job)
        if _stop is not None:
            return _stop

        checkout_session = await chatgpt_client.create_checkout(session)

        # -----------------------------------------------------------------
        # Step 4: Stripe init (R2.1-2.2, retry theo R2.6). Trả về
        # `StripePaymentPageInit` với `amount` extract từ response — dùng
        # cho `deferred_intent[amount]` trong `elements_sessions` kế tiếp.
        # -----------------------------------------------------------------
        _stop = _check_stop_or_pause(job)
        if _stop is not None:
            return _stop

        init_result = await stripe_client.init(
            checkout_session.checkout_session_id,
            checkout_session.publishable_key,
        )

        # Fetch Stripe.js bundle → extract TokenConfig (Requirement 3.3).
        # Chạy song song với elements_sessions để tiết kiệm ~1-2s (bundle
        # fetch không phụ thuộc kết quả elements). Cache disk → chỉ tốn 1
        # lần trên toàn bộ project (bundle rarely changes).
        _stop = _check_stop_or_pause(job)
        if _stop is not None:
            return _stop

        token_config_task = asyncio.create_task(
            stripe_client.ensure_token_config()
        )

        # -----------------------------------------------------------------
        # Step 5: Stripe elements/sessions (R2.3-2.4). Truyền `amount` từ
        # init response — Stripe bắt buộc `deferred_intent[mode]` +
        # `deferred_intent[amount]` cho subscription flow.
        # -----------------------------------------------------------------
        _stop = _check_stop_or_pause(job)
        if _stop is not None:
            token_config_task.cancel()
            return _stop

        elements = await stripe_client.elements_sessions(
            checkout_session.checkout_session_id,
            checkout_session.publishable_key,
            amount=init_result.amount,
        )
        token_config = await token_config_task

        # -----------------------------------------------------------------
        # Step 6: Generate BillingAddress NL (R3.1).
        # -----------------------------------------------------------------
        _stop = _check_stop_or_pause(job)
        if _stop is not None:
            return _stop

        billing = self._profile_generator.generate(parsed.email)

        # -----------------------------------------------------------------
        # Step 6b: Update tax_region/billing vào payment_page state trước
        # confirm. HAR thực tế (browser) gọi 7 POST update progressive theo
        # từng field user gõ. Backend gọi 1 lần với tax_region đầy đủ để
        # commit state — nếu skip, confirm 200 nhưng KHÔNG attach payment
        # method → refresh_poll không có setup_intent.
        # -----------------------------------------------------------------
        _stop = _check_stop_or_pause(job)
        if _stop is not None:
            return _stop

        await stripe_client.update_billing(
            checkout_session.checkout_session_id,
            checkout_session.publishable_key,
            elements.session_id,
            billing,
        )

        # -----------------------------------------------------------------
        # Step 7: Stripe confirm (R3.2-3.7, KHÔNG retry — R3.5).
        # -----------------------------------------------------------------
        _stop = _check_stop_or_pause(job)
        if _stop is not None:
            return _stop

        # Confirm với 2 token JS-runtime compute từ Stripe.js bundle
        # (Requirement 3.3) — bỏ bank selection ở đây (Requirement 6.2 —
        # bank chọn ở pay.ideal.nl sau redirect, không phải ở Stripe).
        confirm_result = await stripe_client.confirm(
            checkout_session.checkout_session_id,
            checkout_session.publishable_key,
            elements.session_id,
            billing,
            init_checksum=init_result.init_checksum,
            amount=init_result.amount,
            elements_config_id=elements.config_id,
            init_config_id=init_result.config_id,
            page_id=init_result.page_id,
            token_config=token_config,
        )

        # -----------------------------------------------------------------
        # Step 8a: POST `/checkout/snapshot` gửi billing_address đầy đủ cho
        # ChatGPT (HAR events 002-004). Nếu skip: approve trả 200 nhưng
        # Stripe KHÔNG transition submission_attempt.state → setup_intent
        # không attach → refresh_state timeout.
        # -----------------------------------------------------------------
        _stop = _check_stop_or_pause(job)
        if _stop is not None:
            return _stop

        await chatgpt_client.snapshot_billing(session, billing)

        # -----------------------------------------------------------------
        # Step 8b: ChatGPT approve (HAR event 005 — sau snapshot, sau confirm).
        # Approve chatgpt.com trigger Stripe attach setup_intent qua webhook
        # backend. Response `{result: "approved"}`.
        # -----------------------------------------------------------------
        _stop = _check_stop_or_pause(job)
        if _stop is not None:
            return _stop

        await chatgpt_client.approve(
            checkout_session.checkout_session_id,
            checkout_session.processor_entity,
            session,
        )

        # -----------------------------------------------------------------
        # Step 9: Refresh state — lấy `setup_intent.next_action.redirect_to_url.url`
        # (Requirement 4.3-4.6). Stripe attach `next_action` ASYNC sau confirm,
        # nên phải poll GET `/v1/payment_pages/{id}?elements_session_client[...]`
        # cho tới khi thấy `redirect_to_url.url` xuất hiện.
        #
        # HAR event 013 xác nhận shape query params + response
        # (`setup_intent.next_action.redirect_to_url.url` =
        # `https://pm-redirects.stripe.com/authorize/{acct}/sa_nonce_{...}`).
        # -----------------------------------------------------------------
        _stop = _check_stop_or_pause(job)
        if _stop is not None:
            return _stop

        redirect_to_url = await self._poll_stripe_redirect_url(
            stripe_client=stripe_client,
            checkout_session_id=checkout_session.checkout_session_id,
            publishable_key=checkout_session.publishable_key,
            elements_session_id=elements.session_id,
            cancellation_token=job.cancellation_token,
            logger=logger,
            job_id=job.job_id,
        )

        # -----------------------------------------------------------------
        # Step 10: Follow redirect `pm-redirects.stripe.com/authorize/...` →
        # 302 Location `https://pay.ideal.nl/transactions/{encoded}?sig=...`
        # (Requirement 4.7-4.9). `follow_redirect()` đã có sẵn: đọc raw 302,
        # parse Location, trả `(encoded_tx_url, sig)`.
        # -----------------------------------------------------------------
        _stop = _check_stop_or_pause(job)
        if _stop is not None:
            return _stop

        encoded_tx_url, sig = await stripe_client.follow_redirect(redirect_to_url)
        # Sig cần append vào known_secrets để mask log downstream (R4.9).
        if sig and sig not in known_secrets:
            known_secrets.append(sig)

        # -----------------------------------------------------------------
        # Step 11a: GET `pay.ideal.nl/transactions/{encoded_tx_url}?sig=<sig>`
        # để lấy cookie `tx_api_token` (JWT). Cookie này BẮT BUỘC cho POST
        # `/api/v1/transactions/{encoded_tx_url}/initiate` bên dưới.
        #
        # DÙNG CLIENT TÁCH BIỆT: shared http_client có headers `oai-*` leak
        # từ chatgpt_client → `pay.ideal.nl` reject HTTP 431 (Request Header
        # Fields Too Large). Fresh client + minimal headers → OK.
        # Cookie `tx_api_token` sẽ được lưu trong fresh_client jar và reuse
        # cho POST /initiate ngay sau đó.
        # -----------------------------------------------------------------
        payment_link = (
            f"https://pay.ideal.nl/transactions/{encoded_tx_url}?sig={sig}"
        )
        # Fresh client cho `pay.ideal.nl` — headers tối thiểu (tránh
        # 431 do header `oai-*` leak). PHẢI truyền `proxy=proxy_url` khi
        # có proxy để `pay.ideal.nl` thấy cùng IP với các bước trước
        # (Stripe/ChatGPT) — nếu khác IP giữa các bước, iDEAL risk-engine
        # có thể decline giao dịch.
        pay_ideal_client_kwargs: dict[str, Any] = {
            "allow_redirects": True,
            "timeout": 30.0,
            "headers": {
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
                "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
            },
        }
        if proxy_url is not None:
            pay_ideal_client_kwargs["proxy"] = proxy_url
        pay_ideal_client = http.create_async_client(**pay_ideal_client_kwargs)
        try:
            try:
                pay_page_response = await pay_ideal_client.get(
                    payment_link,
                    headers={"Referer": "https://checkout.stripe.com/"},
                )
            except (
                http.TimeoutException,
                http.NetworkError,
                http.TransportError,
            ) as exc:
                # Transport lỗi (timeout / DNS / proxy die) khi GET
                # pay.ideal.nl → map cùng `pay_ideal_page_http_error` để
                # trigger auto-retry (code đã trong whitelist).
                logger.warning(
                    "pay_ideal GET transport error: job_id=%s type=%s msg=%s",
                    job.job_id,
                    type(exc).__name__,
                    str(exc)[:200],
                )
                raise IdealFlowError(
                    error_code="pay_ideal_page_http_error",
                    step="pay_ideal_get_page",
                    message=(
                        f"GET pay.ideal.nl/transactions/ transport error "
                        f"({type(exc).__name__}): {str(exc)[:200]}"
                    ),
                ) from exc
            if not (200 <= pay_page_response.status_code < 300):
                raise IdealFlowError(
                    error_code="pay_ideal_page_http_error",
                    step="pay_ideal_get_page",
                    message=(
                        f"GET pay.ideal.nl/transactions/ returned HTTP "
                        f"{pay_page_response.status_code} — could not obtain "
                        f"the tx_api_token cookie."
                    ),
                )

            # -------------------------------------------------------------
            # Step 11b: Pick DeviceProfile + POST
            # `pay.ideal.nl/api/v1/transactions/{encoded_tx_url}/initiate`
            # (Requirement 5.1-5.9). Response chứa:
            #   - `qrCodeUrl` = URL iDEAL 2.0 universal (nội dung QR PNG)
            #   - `payloadUri` = URL-encoded path (cho JS bank picker)
            #   - `supportedIssuers[]` = list bank + deeplink
            #
            # DÙNG `pay_ideal_client` (fresh client) — có cookie
            # `tx_api_token` từ step 11a. Nếu dùng `http_client` shared →
            # thiếu cookie + có oai-* headers → 431/500.
            # -------------------------------------------------------------
            _stop = _check_stop_or_pause(job)
            if _stop is not None:
                return _stop

            device_profile = await self._device_profile_allocator.pick_for_job(
                job.job_id
            )
            # Build TransactionClient MỚI với pay_ideal_client (cookie jar
            # đã lưu tx_api_token từ step 11a).
            pay_ideal_transaction_client = TransactionClient(
                http_client=pay_ideal_client,
                logger=logger,
            )
            transaction_initiate = await pay_ideal_transaction_client.initiate(
                encoded_tx_url,
                sig,
                device_profile,
            )
        finally:
            await pay_ideal_client.close()

        # QR code content — encode chuỗi này thành QR PNG. Đây là URL
        # `https://tx.ideal.nl/2/{tx_id}?sig={sig}` (iDEAL 2.0 universal),
        # banking app trên điện thoại quét trực tiếp URL này để hoàn tất
        # thanh toán, KHÔNG cần user vào bank picker manual.
        qr_content = transaction_initiate.qrCodeUrl
        if not qr_content:
            raise IdealFlowError(
                error_code="qr_code_url_missing",
                step="transaction_initiate",
                message=(
                    "Response POST /api/v1/transactions/{encoded}/initiate "
                    "has no `qrCodeUrl` — cannot render QR PNG."
                ),
            )

        logger.info(
            "flow ideal ok issuers=%d amount=%s creditor=%s",
            len(transaction_initiate.supportedIssuers),
            transaction_initiate.amount,
            transaction_initiate.creditorName,
        )

        # -----------------------------------------------------------------
        # Step 12: Render QR PNG từ `qrCodeUrl` (R7.3). User quét PNG →
        # banking app open payment.
        # -----------------------------------------------------------------
        _stop = _check_stop_or_pause(job)
        if _stop is not None:
            return _stop

        qr_path = self._qr_renderer.render_png(
            qr_content,
            job.job_id,
            self._qr_output_dir,
        )
        artifact_path_str = str(qr_path)

        # `qr_ready` là milestone quan trọng nhất — 1 dòng gọn, không dump
        # full payment_link (dài + đã có ở SSE `payment_link` extra). Path
        # PNG hữu ích cho debug local.
        logger.info("flow qr_ready artifact=%s", artifact_path_str)
        return JobResult(
            status=JobStatus.QR_READY,
            artifact_path=artifact_path_str,
            payment_link=payment_link,
        )

    # ------------------------------------------------------------------
    # Step 9 helper — poll refresh_state cho tới khi Stripe attach
    # setup_intent.next_action.redirect_to_url.url
    # ------------------------------------------------------------------

    async def _poll_stripe_redirect_url(
        self,
        *,
        stripe_client: StripeClient,
        checkout_session_id: str,
        publishable_key: str,
        elements_session_id: str,
        cancellation_token,
        logger: logging.Logger,
        job_id: str,
    ) -> str:
        """Poll GET `/v1/payment_pages/{id}?elements_session_client[...]`
        cho tới khi response có `setup_intent.next_action.redirect_to_url.url`.

        Stripe attach `next_action` ASYNC sau khi ChatGPT approve — request
        đầu tiên có thể trả state cũ (không có next_action). Poll với
        exponential backoff cho tới `_REDIRECT_POLL_MAX_ATTEMPTS`.

        Fail_Fast_Policy: hết attempts → raise IdealFlowError.

        Returns:
            URL string, dạng `https://pm-redirects.stripe.com/authorize/...`.
        """
        max_attempts = _REDIRECT_POLL_MAX_ATTEMPTS
        delay = _REDIRECT_POLL_INITIAL_DELAY_SECONDS
        last_payload_status: Any = None

        for attempt in range(1, max_attempts + 1):
            if cancellation_token.is_cancelled():
                raise IdealFlowError(
                    error_code="cancelled",
                    step="refresh_state",
                    message="Job cancelled during refresh_state polling.",
                )

            payload = await stripe_client.refresh_state(
                checkout_session_id,
                publishable_key,
                elements_session_id=elements_session_id,
            )
            redirect_url = stripe_client.extract_redirect_url_from_payload(payload)
            if redirect_url:
                logger.info(
                    "flow refresh_state redirect ok attempt=%d/%d",
                    attempt,
                    max_attempts,
                )
                return redirect_url

            last_payload_status = payload.get("status") if isinstance(payload, dict) else None
            # Log "waiting" mỗi attempt tạo noise (poll 15 lần = 15 dòng
            # giống nhau). Chỉ log 1 dòng đầu để user biết flow đang chờ,
            # sau đó im lặng cho tới khi thấy redirect hoặc exhausted.
            if attempt == 1:
                logger.info(
                    "flow refresh_state waiting for next_action (status=%s, "
                    "poll interval=%.1fs)",
                    last_payload_status,
                    delay,
                )
            await asyncio.sleep(delay)
            # Exponential backoff, cap ở _REDIRECT_POLL_MAX_DELAY_SECONDS.
            delay = min(delay * 1.5, _REDIRECT_POLL_MAX_DELAY_SECONDS)

        raise IdealFlowError(
            error_code="stripe_next_action_missing",
            step="refresh_state",
            message=(
                f"After {max_attempts} poll attempt(s), response `/v1/payment_pages/{{id}}` "
                f"still has no `setup_intent.next_action.redirect_to_url.url`. "
                f"Last checkout status={last_payload_status!r}."
            ),
        )

    # ------------------------------------------------------------------
    # Session resolution (step 2 helper)
    # ------------------------------------------------------------------

    async def _resolve_session(
        self,
        *,
        chatgpt_client: ChatgptClient,
        parsed: IdealParsedAccount,
        account_key: str,
        logger: logging.Logger,
    ) -> SessionBundle:
        """Thin wrapper over the shared `_chatgpt.resolve_session()` free function.

        The full cache→login→direct-token strategy (Requirement 1.2, 1.3, 10.4)
        now lives in `payments/_chatgpt/session.py` so the UPI flow can reuse it.
        Behaviour is unchanged — `self._session_cache` is threaded in explicitly
        (it also supplies `skip_revalidate_if_fresh_hours`).
        """
        return await _resolve_session_fn(
            login_client=chatgpt_client,
            session_cache=self._session_cache,
            parsed=parsed,
            account_key=account_key,
            logger=logger,
        )

    @staticmethod
    def _safe_build_session_from_cache(
        payload: dict[str, Any],
        logger: logging.Logger,
    ) -> SessionBundle | None:
        """Delegate to the shared `_chatgpt.session._safe_build_session_from_cache`.

        Kept as a method so `check_plan_status` (below) keeps calling
        `self._safe_build_session_from_cache(...)` unchanged.
        """
        return _safe_build_session_from_cache_fn(payload, logger)

    # ------------------------------------------------------------------
    # Check plan status (Task 5 — dùng cache session để verify Plus)
    # ------------------------------------------------------------------

    async def check_plan_status(self, job: Job) -> dict[str, Any]:
        """Kiểm tra tài khoản đã lên Plus chưa cho 1 job.

        Chiến lược:
            1. Parse `account_line` → email (fail-fast nếu invalid).
            2. Đọc `session_cache.get(account_key)`. Nếu miss → trả
               `{plan: 'unknown', error: 'no_session_cached'}` (cần chạy
               login trước — chưa có cookie/token thì không check được).
            3. Build `SessionBundle` từ payload cache (nếu shape corrupt →
               trả `{plan: 'unknown', error: 'session_corrupt'}`).
            4. Khởi tạo AsyncSession cô lập (KHÔNG dùng proxy — check plan
               là read-only, không cần rotate IP như flow chính).
            5. Gọi `ChatgptClient.check_plan_status()` → trả nguyên vẹn
               dict result.

        KHÔNG raise (giống chatgpt_client.check_plan_status) — route layer
        chỉ đơn giản trả 200 với dict.
        """
        logger = self._logger_factory(f"ideal.check_plan.{job.job_id}")

        parsed = self.parse_account_line(job.account_line)
        if isinstance(parsed, AccountLineError):
            return {"plan": "unknown", "error": f"invalid_account_line: {parsed.reason}"}

        account_key = self._compute_account_key(parsed.email)
        cached = await self._session_cache.get(account_key)
        if cached is None:
            return {
                "plan": "unknown",
                "error": "no_session_cached",
                "email": parsed.email,
            }

        session = self._safe_build_session_from_cache(cached.payload, logger)
        if session is None:
            return {
                "plan": "unknown",
                "error": "session_corrupt",
                "email": parsed.email,
            }

        client_kwargs: dict[str, Any] = {
            "allow_redirects": True,
            "timeout": _DEFAULT_HTTP_TIMEOUT_SECONDS,
            "headers": dict(_DEFAULT_HTTP_HEADERS),
        }
        async with http.create_async_client(**client_kwargs) as http_client:
            chatgpt_client = ChatgptClient(
                http_client=http_client,
                session_cache=self._session_cache,
                logger=logger,
            )
            return await chatgpt_client.check_plan_status(session)

    # ------------------------------------------------------------------
    # Account key helper
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_account_key(email: str) -> str:
        """Sinh `account_key` = sha256(email)[:32] — KHÔNG lưu email plain.

        Dùng cho `session_cache.get/save/clear` như định danh account —
        cache filename tự hash thêm 1 lần nữa (defensive) nên tổng cộng
        chuỗi trên đĩa hoàn toàn không có email thô.
        """
        return hashlib.sha256(email.encode("utf-8")).hexdigest()[
            :_ACCOUNT_KEY_HEX_LENGTH
        ]


def _static_conformance_check(handler: "IdealFlowHandler") -> PaymentFlowHandler:
    """Static conformance check tới interface `PaymentFlowHandler`.

    Nếu chữ ký method của `IdealFlowHandler` (`run` / `parse_account_line` /
    `get_max_concurrent_key`) không khớp Protocol → mypy/pyright cảnh báo
    tại điểm return này. Runtime check đã được `JobManager.register_handler`
    thực hiện qua duck-type — 2 lớp phòng thủ bổ trợ nhau, không thay thế.

    Hàm này KHÔNG được gọi ở runtime — chỉ tồn tại để type checker xác nhận
    IdealFlowHandler thoả mãn structural type PaymentFlowHandler.
    """
    return handler


__all__ = ["IdealFlowHandler"]
