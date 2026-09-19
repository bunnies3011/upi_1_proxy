"""TransactionClient — bước Requirement 5: khởi tạo giao dịch iDEAL trên
`pay.ideal.nl` và trả về `IdealTransactionInitiate` đã validate.

Đứng SAU `StripeClient.follow_redirect()` (Requirement 4.7-4.9) — nhận
`encoded_tx_url` + `sig` trích xuất từ redirect 302 và `DeviceProfile` do
`DeviceProfileAllocator.pick_for_job()` cấp cho vòng đời 1 IdealJob, gọi
`POST pay.ideal.nl/api/v1/transactions/{encoded_tx_url}/initiate?sig={sig}`
với body `{"deviceInfo": ..., "httpReferrer": "https://chatgpt.com/"}`
(Requirement 5.1), rồi trả về `IdealTransactionInitiate` để `IssuerSelector`
xử lý bước Requirement 6.

Fail_Fast_Policy (Requirement 14.1, 14.3, 14.4):
    - Lỗi transport (timeout, connection refused, DNS, TLS) hoặc HTTP status
      không phải 2xx (Requirement 5.8) → raise `TransactionHttpError` mang
      `http_status` (None nếu chưa có response).
    - Response body không parse được JSON → raise `TransactionHttpError` với
      `http_status=<code 2xx>` và `detail` nêu rõ decode fail.
    - Response thiếu field bắt buộc (`view`, `supportedIssuers[]`, hoặc
      thiếu field của IssuerBank) → `parse_ideal_transaction_initiate()` tự
      raise `RequiredFieldMissingError` (Requirement 5.6, 5.7) — client
      không catch, để propagate lên boundary `IdealFlowHandler`.
    - `view != "INITIAL_VIEW"` (Requirement 5.5) → raise
      `InvalidTransactionViewError(actual_view=<value>)`.

Payment_Module_Boundary: chỉ import `app.core.redaction` (helper thuần —
không đụng Settings_Store/DB/ProxyPool). Toàn bộ domain model + exception
lấy từ `app.payments.ideal.*`.

Sensitive_Data_Redaction (Requirement 4.9, 14.8): giá trị `sig` KHÔNG log
thô trong bất kỳ điểm nào — luôn qua `redact_message(msg, [sig])` trước khi
`logger.info()`. Bản thân `sig` được truyền NGUYÊN VẸN qua HTTP client `params=`
(Property 14) — client tự URL-encode ở tầng wire, giá trị domain giữ identity.

_Requirements: 5.1, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9_
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Any, Final

from app.core import http_client as http
from app.core.redaction import redact_message
from app.payments.ideal.errors import (
    InvalidTransactionViewError,
    TransactionHttpError,
)
from app.payments.ideal.models import (
    DeviceProfile,
    IdealTransactionInitiate,
    parse_ideal_transaction_initiate,
)

#: Base URL của endpoint iDEAL — hằng, không cấu hình qua Settings_Store
#: vì tool chỉ hoạt động với domain sản phẩm chính thức (không có staging
#: của `pay.ideal.nl` mà tool support).
_IDEAL_BASE_URL: Final[str] = "https://pay.ideal.nl"

#: Path template cho endpoint initiate — Requirement 5.1. `encoded_tx_url`
#: là chuỗi đã URL-encode do redirect từ Stripe trả về (Requirement 4.7),
#: KHÔNG re-encode ở đây (double encoding sẽ phá URL wire format).
_INITIATE_PATH_TEMPLATE: Final[str] = "/api/v1/transactions/{encoded_tx_url}/initiate"

#: `httpReferrer` cố định theo HAR observed — Requirement 5.1.
_HTTP_REFERRER: Final[str] = "https://chatgpt.com/"

#: Giá trị `view` mong đợi (Requirement 5.5). Bất kỳ giá trị khác đều là
#: trạng thái không an toàn để tiếp tục xử lý `supportedIssuers[]`.
_EXPECTED_INITIAL_VIEW: Final[str] = "INITIAL_VIEW"

#: Default timeout khi caller không truyền `timeout_seconds` — cùng giá trị
#: default của `ideal.stripe_request_timeout_seconds` (Requirement 2.1, 30s)
#: để hành vi mạng nhất quán giữa các bước HTTP của flow.
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0


class TransactionClient:
    """Client cho endpoint `POST pay.ideal.nl/api/v1/transactions/{encoded_tx_url}/initiate`.

    Dependency injection thuần: `http_client` (`curl_cffi.AsyncSession` do
    `IdealFlowHandler` cấp — đã config proxy theo `ProxyLease` và headers
    baseline), `logger` (`logging.Logger` chuẩn — task 20.1 sẽ inject
    `JobLogger` wrapper phát SSE realtime, hiện tại nhận Logger vanilla).

    Stateless — instance-per-job hay singleton đều OK. Không giữ state
    giữa các lần gọi `initiate()`.
    """

    def __init__(
        self,
        http_client: http.AsyncSession,
        logger: logging.Logger,
    ) -> None:
        self._http_client = http_client
        self._logger = logger

    async def initiate(
        self,
        encoded_tx_url: str,
        sig: str,
        device_profile: DeviceProfile,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> IdealTransactionInitiate:
        """Gọi endpoint initiate và trả `IdealTransactionInitiate` đã validate.

        Args:
            encoded_tx_url: Phần path token trích xuất từ URL redirect
                `https://pay.ideal.nl/transactions/{encoded_tx_url}?sig={sig}`
                (Requirement 4.7). Đã URL-encode ở tầng redirect — nhét
                thẳng vào path template.
            sig: Chữ ký giao dịch, truyền query param `?sig=<sig>`
                (Property 14: identity — không parse/biến đổi). Client tự
                percent-encode ở wire, giá trị domain giữ nguyên.
            device_profile: `DeviceProfile` do `DeviceProfileAllocator`
                cấp cho toàn bộ vòng đời IdealJob này (Requirement 5.1,
                5.3). Serialize qua `dataclasses.asdict()` → dict camelCase
                khớp wire format iDEAL.
            timeout_seconds: Timeout wall-clock cho request này (đơn vị
                giây, positional keyword). Caller (`IdealFlowHandler`) đọc
                `ideal.stripe_request_timeout_seconds` từ Settings_Store và
                truyền vào; default `30.0` giây khi caller không chỉ định
                (nhất quán với Requirement 2.1 default).

        Returns:
            `IdealTransactionInitiate` đã parse xong với `view ==
            "INITIAL_VIEW"` và `supportedIssuers` non-empty đã parse thành
            list `IssuerBank`.

        Raises:
            TransactionHttpError: Requirement 5.8 — HTTP status không phải
                2xx, lỗi transport (timeout, connection refused, DNS/TLS),
                hoặc response body không parse được JSON.
            RequiredFieldMissingError: Requirement 5.6, 5.7 — thiếu `view`,
                `supportedIssuers[]` (hoặc rỗng, hoặc kiểu sai), hoặc thiếu
                field của IssuerBank. Propagate từ `parse_ideal_transaction_initiate`.
            InvalidTransactionViewError: Requirement 5.5 — `view` khác
                `"INITIAL_VIEW"` (mang `actual_view` = giá trị thực tế).
        """
        url = f"{_IDEAL_BASE_URL}{_INITIATE_PATH_TEMPLATE.format(encoded_tx_url=encoded_tx_url)}"
        params = {"sig": sig}
        body = {
            "deviceInfo": asdict(device_profile),
            "httpReferrer": _HTTP_REFERRER,
        }

        # 1) Log realtime bắt đầu gọi — mask `sig`. KHÔNG log toàn bộ URL
        #    (chứa `sig` là query param sau khi HTTP client build), chỉ log path
        #    template + `sig` đã redact để giữ khả năng trace bước flow.
        self._logger.info(
            redact_message(
                f"[initiate] POST {_IDEAL_BASE_URL}{_INITIATE_PATH_TEMPLATE.format(encoded_tx_url=encoded_tx_url)}?sig={sig}",
                [sig],
            )
        )

        # 2) Gọi HTTP. Chuyển mọi RequestException (transport/timeout/protocol)
        #    thành TransactionHttpError với http_status=None — Fail_Fast R5.8.
        try:
            response = await self._http_client.post(
                url,
                params=params,
                json=body,
                timeout=timeout_seconds,
            )
        except http.HTTPError as exc:
            self._logger.error(
                redact_message(
                    f"[initiate] Lỗi transport khi POST initiate: {exc!r}",
                    [sig],
                )
            )
            raise TransactionHttpError(
                http_status=None,
                detail=f"transport_error: {type(exc).__name__}",
            ) from exc

        # 3) Check HTTP status — R5.8: bất kỳ non-2xx đều Fail_Fast.
        if not (200 <= response.status_code < 300):
            self._logger.error(
                redact_message(
                    f"[initiate] HTTP {response.status_code} từ pay.ideal.nl",
                    [sig],
                )
            )
            raise TransactionHttpError(
                http_status=response.status_code,
                detail=f"non_2xx_status: {response.status_code}",
            )

        # 4) Parse JSON body. Response không parse được JSON là bất thường
        #    (endpoint luôn trả JSON theo HAR) — Fail_Fast bằng TransactionHttpError.
        try:
            payload: Any = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            self._logger.error(
                redact_message(
                    f"[initiate] Response body không parse được JSON: {exc!r}",
                    [sig],
                )
            )
            raise TransactionHttpError(
                http_status=response.status_code,
                detail=f"invalid_json_body: {type(exc).__name__}",
            ) from exc

        if not isinstance(payload, dict):
            # Response JSON không phải object cấp cao nhất → không parse được
            # thành model, cũng thuộc lỗi shape wire-level.
            self._logger.error(
                redact_message(
                    f"[initiate] Response body không phải JSON object: type={type(payload).__name__}",
                    [sig],
                )
            )
            raise TransactionHttpError(
                http_status=response.status_code,
                detail=f"non_object_json_body: {type(payload).__name__}",
            )

        # 5) Parse → IdealTransactionInitiate. Thiếu field bắt buộc →
        #    RequiredFieldMissingError propagate lên boundary IdealFlowHandler
        #    (Requirement 5.6, 5.7).
        initiate = parse_ideal_transaction_initiate(payload)

        # 6) Validate view == INITIAL_VIEW (Requirement 5.5). Đặt SAU parse
        #    để có thể mang `actual_view` cụ thể trong exception, không gộp
        #    với lỗi thiếu field.
        if initiate.view != _EXPECTED_INITIAL_VIEW:
            self._logger.error(
                redact_message(
                    f"[initiate] View khác '{_EXPECTED_INITIAL_VIEW}': got '{initiate.view}'",
                    [sig],
                )
            )
            raise InvalidTransactionViewError(actual_view=initiate.view)

        # 7) Log thành công realtime — Requirement 5.9: số issuer, amount,
        #    creditorName. `sig` đã redact ở mọi log line phía trên.
        self._logger.info(
            redact_message(
                (
                    f"[initiate] OK — issuers={len(initiate.supportedIssuers)}, "
                    f"amount={initiate.amount!r}, creditorName={initiate.creditorName!r}"
                ),
                [sig],
            )
        )

        return initiate


__all__ = ["TransactionClient"]
