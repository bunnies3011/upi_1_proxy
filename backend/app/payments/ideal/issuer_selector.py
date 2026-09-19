"""Chọn IssuerBank mặc định từ danh sách `supportedIssuers[]` (Requirement 6).

Thành phần này đứng SAU `TransactionClient.initiate()` (Requirement 5) — nhận
list `IssuerBank` đã parse xong (view/supportedIssuers đã validate) và giá trị
`ideal.default_issuer` từ Settings_Store, chọn ra đúng 1 IssuerBank khả dụng
khớp cấu hình để `QrRenderer` sinh QR PNG từ `deeplink` (Requirement 7).

Fail_Fast_Policy (Requirement 6.3): khi `default_issuer_id` không khớp `id`
của bất kỳ IssuerBank khả dụng nào, raise `NoIssuerAvailableError` mang
DANH SÁCH ĐẦY ĐỦ `id` các issuer khả dụng — để `IdealFlowHandler` log rõ
nguyên nhân và user cập nhật `ideal.default_issuer` sang mã hợp lệ. Không
im lặng chọn bừa 1 issuer khác (che lỗi cấu hình).

Payment_Module_Boundary: chỉ import từ `app.payments.ideal.*` — không đụng
`app.core.*` (Settings_Store là caller inject `default_issuer_id`, không đọc
DB trong module này để giữ stateless + dễ test).

SOLID:
    - Single Responsibility: chỉ lo chọn issuer, không parse, không render QR.
    - Stateless: `select()` không đụng field instance — có thể instance-per-call
      hoặc singleton đều được, không quan trọng.

_Requirements: 6.2, 6.3_
"""

from __future__ import annotations

from typing import Final

from app.payments.ideal.errors import NoIssuerAvailableError
from app.payments.ideal.models import IssuerBank

#: Giá trị `availabilityStatus` coi là khả dụng theo convention `pay.ideal.nl`
#: (chuỗi in hoa, khớp wire format Requirement 5.7). Nếu HAR log/spec tương lai
#: xác nhận thêm giá trị khác cũng khả dụng (ví dụ "ONLINE"), mở rộng thành
#: `frozenset({"AVAILABLE", ...})` và cập nhật check bằng `in _AVAILABLE_STATUSES`.
_AVAILABLE_STATUS: Final[str] = "AVAILABLE"


class IssuerSelector:
    """Chọn IssuerBank mặc định khớp `ideal.default_issuer` từ list issuers
    khả dụng — Requirement 6.2, 6.3.

    Class stateless — instance-per-call hay singleton đều OK. Dùng class thay
    vì free function để cho phép DI (test dễ mock) và đồng nhất với các
    thành phần khác trong `payments/ideal/` (StripeClient, TransactionClient,
    QrRenderer, IdealProfileGenerator).
    """

    def select(
        self, issuers: list[IssuerBank], default_issuer_id: str
    ) -> IssuerBank:
        """Chọn IssuerBank có `id == default_issuer_id` VÀ `availabilityStatus`
        khả dụng trong `issuers`.

        Logic (Requirement 6.2, 6.3):
            1. Lọc `issuers` giữ lại các phần tử có
               `availabilityStatus == "AVAILABLE"` (convention pay.ideal.nl —
               các giá trị khác như "UNAVAILABLE" bị loại).
            2. Trong danh sách khả dụng, tìm issuer có `id == default_issuer_id`.
            3. Nếu tìm thấy → trả về IssuerBank đó (R6.2).
            4. Nếu KHÔNG tìm thấy → raise `NoIssuerAvailableError` mang
               danh sách `id` các issuer khả dụng (R6.3) để `IdealFlowHandler`
               log rõ nguyên nhân, hướng dẫn user cập nhật cấu hình.

        Ghi chú:
            - Hàm này KHÔNG validate `default_issuer_id` thuộc whitelist
              `ideal.known_issuers` (đó là R6.5, R6.6 do Settings API validate
              KHI GHI vào Settings_Store, không phải khi đọc ra dùng).
            - Nếu `issuers` rỗng ngay từ đầu → filter khả dụng cũng rỗng →
              raise `NoIssuerAvailableError(available_issuer_ids=[])`. Nhưng
              thực tế R5.6 đã fail-fast list rỗng ở `parse_ideal_transaction_initiate`,
              nên đến bước này `issuers` luôn non-empty — code vẫn xử lý
              defensive để KHÔNG rơi vào trạng thái không xác định.
            - Trường hợp có nhiều issuer trùng `id` khả dụng (không xảy ra theo
              wire format thực tế, nhưng defensive): trả về phần tử ĐẦU TIÊN
              trong `issuers` theo thứ tự gốc — giữ tính deterministic để
              log/trace tái hiện được.

        Args:
            issuers: Danh sách `IssuerBank` đã parse từ response `initiate`
                (Requirement 5.7). Có thể chứa cả issuer khả dụng và không
                khả dụng — hàm tự lọc.
            default_issuer_id: Giá trị `ideal.default_issuer` đọc từ
                Settings_Store — mã kỹ thuật (`id`) của IssuerBank muốn dùng.

        Returns:
            IssuerBank khớp `default_issuer_id` VÀ `availabilityStatus`
            khả dụng.

        Raises:
            NoIssuerAvailableError: Không có IssuerBank khả dụng nào có
                `id == default_issuer_id`. `available_issuer_ids` mang
                danh sách `id` của TẤT CẢ issuer khả dụng còn lại (có thể
                rỗng nếu toàn bộ `issuers` đều không khả dụng) —
                Requirement 6.3.
        """
        available_issuers: list[IssuerBank] = [
            issuer
            for issuer in issuers
            if issuer.availabilityStatus == _AVAILABLE_STATUS
        ]

        for issuer in available_issuers:
            if issuer.id == default_issuer_id:
                return issuer

        # Không tìm thấy issuer khớp — R6.3: liệt kê `id` các issuer khả dụng
        # để user cập nhật `ideal.default_issuer` sang mã hợp lệ.
        raise NoIssuerAvailableError(
            available_issuer_ids=[issuer.id for issuer in available_issuers],
        )


__all__ = ["IssuerSelector"]
