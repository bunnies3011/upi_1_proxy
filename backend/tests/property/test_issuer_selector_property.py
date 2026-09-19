"""Property test cho `payments/ideal/issuer_selector.py` — `IssuerSelector`
chỉ chọn issuer khớp cấu hình VÀ khả dụng (Property 18).

**Property 18: IssuerSelector chỉ chọn issuer khớp whitelist và khả dụng**

Với mọi danh sách `IssuerBank[]` và giá trị `default_issuer_id` ngẫu nhiên,
`IssuerSelector.select` phải:

1. Trả về ĐÚNG IssuerBank khi tồn tại phần tử có `id == default_issuer_id`
   VÀ `availabilityStatus == "AVAILABLE"` — Requirement 6.2. Khi có nhiều
   issuer khả dụng cùng `id` (defensive case, không xảy ra theo wire format
   thực tế), trả về phần tử ĐẦU TIÊN trong danh sách gốc (deterministic,
   khớp docstring `IssuerSelector.select`).
2. Trong mọi trường hợp khác — không có id khớp, hoặc có id khớp nhưng
   `availabilityStatus != "AVAILABLE"` — raise `NoIssuerAvailableError` với
   `available_issuer_ids` khớp CHÍNH XÁC danh sách `id` các issuer đang khả
   dụng còn lại, theo đúng thứ tự xuất hiện trong `issuers` (Requirement 6.3).

**Validates: Requirements 6.2, 6.3**

Fail_Fast_Policy (Glossary): không có issuer khả dụng khớp cấu hình → dừng
ngay bằng exception domain-specific mang danh sách issuer khả dụng thực tế,
để `IdealFlowHandler` log rõ nguyên nhân và user cập nhật `ideal.default_issuer`
sang mã hợp lệ — KHÔNG im lặng chọn bừa issuer khác (che lỗi cấu hình).
"""

from __future__ import annotations

from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.payments.ideal.errors import NoIssuerAvailableError
from app.payments.ideal.issuer_selector import IssuerSelector
from app.payments.ideal.models import IssuerBank

# ---------------------------------------------------------------------------
# Sinh dữ liệu.
# ---------------------------------------------------------------------------

#: Pool 8 mã issuer cố định — dùng chung cho `id` của `IssuerBank` VÀ
#: `default_issuer_id`, để hypothesis dễ tạo ra CẢ trường hợp khớp (id ∈ pool
#: trùng với id nào đó trong list) LẪN trường hợp không khớp (`NONEXISTENT_ID`
#: hoặc pool id không xuất hiện trong list vì list rỗng/không chứa mã đó).
#: 8 mã nằm trong dải 5-10 theo yêu cầu Property 18 (đủ đa dạng để shrink
#: hypothesis không mất thời gian, không quá nhiều gây slow test).
_ISSUER_POOL: Final[tuple[str, ...]] = (
    "INGBNL2A",
    "RABONL2U",
    "ABNANL2A",
    "BUNQNL2A",
    "TRIONL2U",
    "KNABNL2H",
    "SNSBNL2A",
    "REVOLT21",
)

#: Sentinel id KHÔNG thuộc pool — dùng để chủ động test nhánh không khớp
#: (mọi issuer trong list đều có id thuộc pool, nên default_issuer_id này
#: đảm bảo không match issuer nào).
_NONEXISTENT_ISSUER_ID: Final[str] = "NONEXISTENT_ID"

_DEEPLINK_TYPE: Final[str] = "URL"
_DEEPLINK: Final[str] = "https://x.example/y"

#: Strategy cho 1 `IssuerBank` — chỉ biến thiên `id` (thuộc pool) và
#: `availabilityStatus` (AVAILABLE/UNAVAILABLE) để trace kiểm định lỗi Property
#: 18 gọn nhất; `deeplinkType`/`deeplink` cố định vì chúng không ảnh hưởng
#: logic `IssuerSelector.select`.
_issuer_strategy = st.builds(
    IssuerBank,
    id=st.sampled_from(_ISSUER_POOL),
    deeplinkType=st.just(_DEEPLINK_TYPE),
    deeplink=st.just(_DEEPLINK),
    availabilityStatus=st.sampled_from(["AVAILABLE", "UNAVAILABLE"]),
)


# ---------------------------------------------------------------------------
# Property 18.
# ---------------------------------------------------------------------------


@given(
    issuers=st.lists(_issuer_strategy, min_size=0, max_size=8),
    default_issuer_id=st.sampled_from(
        list(_ISSUER_POOL) + [_NONEXISTENT_ISSUER_ID]
    ),
)
@settings(max_examples=50)
def test_issuer_selector_matches_available_and_configured_id(
    issuers: list[IssuerBank],
    default_issuer_id: str,
) -> None:
    """R6.2, R6.3: `IssuerSelector.select` khớp CHÍNH XÁC 2 nhánh:

    - Có issuer khớp cấu hình VÀ khả dụng → trả về đúng object đó (R6.2).
    - Không khớp → raise `NoIssuerAvailableError` mang danh sách `id` của các
      issuer khả dụng còn lại theo thứ tự gốc (R6.3).
    """
    # Expected: lọc ra các issuer khả dụng, giữ nguyên thứ tự gốc — logic
    # PHẢI khớp implementation (list comprehension theo thứ tự `issuers`).
    available = [
        issuer
        for issuer in issuers
        if issuer.availabilityStatus == "AVAILABLE"
    ]

    # Expected match: issuer khả dụng ĐẦU TIÊN có `id == default_issuer_id`
    # trong danh sách gốc. `next(...)` trên generator giữ nguyên thứ tự
    # duyệt của implementation `IssuerSelector.select` (docstring bảo đảm
    # deterministic — pick phần tử đầu tiên khi có nhiều issuer trùng id).
    match: IssuerBank | None = next(
        (issuer for issuer in available if issuer.id == default_issuer_id),
        None,
    )

    selector = IssuerSelector()

    if match is not None:
        # R6.2: có issuer khả dụng khớp cấu hình → trả về CHÍNH object đó
        # (identity check `is`, không phải chỉ equal), khớp thứ tự gốc.
        result = selector.select(issuers, default_issuer_id)
        assert result is match
    else:
        # R6.3: không có issuer khả dụng khớp cấu hình → raise
        # `NoIssuerAvailableError`, và `available_issuer_ids` KHỚP CHÍNH XÁC
        # danh sách `id` các issuer khả dụng còn lại theo đúng thứ tự gốc
        # (không sort, không dedupe — reflect wire order để user debug).
        with pytest.raises(NoIssuerAvailableError) as exc_info:
            selector.select(issuers, default_issuer_id)

        exc = exc_info.value
        assert exc.available_issuer_ids == [issuer.id for issuer in available]
