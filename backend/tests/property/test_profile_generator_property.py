"""Property test cho `payments/ideal/profile_generator.py` — invariant
`IdealProfileGenerator.generate(email)` LUÔN sinh `BillingAddress` hợp lệ
theo locale Hà Lan (Property 7).

**Property 7: IdealProfileGenerator luôn sinh BillingAddress hợp lệ theo
locale NL**

Sinh email hợp lệ ngẫu nhiên (RFC 5322 subset — hypothesis `st.emails()`),
gọi `IdealProfileGenerator().generate(email)`, assert toàn bộ shape +
constraint locale NL của `BillingAddress` trả về:

    - `result.email` == email đã pass vào (email pass-through nguyên vẹn —
      R3.1: email lấy nguyên từ account đã login, generator KHÔNG được
      chỉnh sửa).
    - `result.name` là chuỗi non-empty chứa ÍT NHẤT một dấu space
      (first_name + last_name join bằng " " theo hiện thực generator).
    - `result.address["country"]` == "NL" (R3.1 — locale bắt buộc).
    - `result.address["line1"]` khớp regex `^[A-Za-z]+ \d+$` (chuẩn NL:
      tên đường + số nhà, không dấu phẩy — pool `_STREETS_NL` toàn ký tự
      Latin, không diacritic).
    - `result.address["city"]` non-empty.
    - `result.address["postal_code"]` khớp regex `^\d{4} [A-Z]{2}$` (chuẩn
      PC6 của TNT Post NL: 4 chữ số + 1 space + 2 chữ cái hoa).
    - `result.address["line2"]` == "" (R3.1: line2 không bắt buộc → giữ
      rỗng, KHÔNG `None`, để khớp shape `dict[str, str]`).
    - `result.address["state"]` == "" (Hà Lan dùng "provincie" chứ không
      dùng "state"; Stripe accept empty cho country NL).

**Validates: Requirements 3.1**

Ghi chú:
    - `max_examples` mặc định 20 lấy từ profile "fast" đã register ở
      `tests/conftest.py`; property này bao trùm mọi email hợp lệ và mọi
      lát cắt ngẫu nhiên của pool NL trong generator nên 20 example là
      đủ dày để phát hiện regression bất biến.
    - KHÔNG assert cụ thể first_name/last_name/street/city/postal thuộc
      pool nào — property là "hợp lệ theo locale NL", không phải "khớp
      pool đóng". Nếu pool NL mở rộng thêm giá trị mới trong tương lai,
      test này vẫn PASS miễn giá trị mới tuân thủ shape ràng buộc.
    - Generator dùng `random` module-level (không seed) → mỗi hypothesis
      example gọi `generate()` sẽ nhận 1 lát cắt ngẫu nhiên khác nhau,
      giúp bao trùm nhiều tổ hợp pool NL trong cùng 1 run.
"""

from __future__ import annotations

import re
from typing import Final

from hypothesis import given
from hypothesis import strategies as st

from app.payments.ideal.profile_generator import IdealProfileGenerator

# ---------------------------------------------------------------------------
# Regex bất biến — compile 1 lần cấp module, tránh recompile mỗi example.
# ---------------------------------------------------------------------------

#: `<Street> <number>` — pool `_STREETS_NL` toàn ký tự Latin không diacritic,
#: số nhà 1..999. Regex chấp nhận cả chữ hoa/thường ở phần tên đường để
#: không phụ thuộc case của pool cụ thể.
_LINE1_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z]+ \d+$")

#: `DDDD LL` — chuẩn PC6 của TNT Post NL: 4 chữ số + 1 space + 2 chữ cái hoa.
_POSTAL_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d{4} [A-Z]{2}$")


# ---------------------------------------------------------------------------
# Property 7 — IdealProfileGenerator.generate(email) luôn hợp lệ locale NL.
# ---------------------------------------------------------------------------


@given(email=st.emails())
def test_generate_always_produces_valid_nl_billing_address(email: str) -> None:
    """R3.1: mọi email hợp lệ đưa vào `IdealProfileGenerator.generate()` →
    trả về `BillingAddress` với `country == "NL"`, `line1`/`city`/
    `postal_code` đúng chuẩn NL, `line2`/`state` rỗng, và `email` được
    pass-through nguyên vẹn."""
    generator = IdealProfileGenerator()

    result = generator.generate(email)

    # Email pass-through nguyên vẹn — generator KHÔNG được chỉnh sửa.
    assert result.email == email

    # Name = "<first> <last>" — non-empty, chứa ít nhất 1 space giữa 2 phần.
    assert isinstance(result.name, str)
    assert result.name != ""
    assert " " in result.name
    # Đảm bảo phần trước/sau space đầu tiên đều non-empty (loại trừ trường
    # hợp bệnh lý " Foo" hay "Foo " lọt qua chỉ nhờ có space).
    first_part, _, rest = result.name.partition(" ")
    assert first_part != ""
    assert rest != ""

    # Locale NL bắt buộc theo R3.1.
    assert result.address["country"] == "NL"

    # line1 = "<Street> <number>" — regex đóng khoá cả 2 đầu chuỗi.
    line1 = result.address["line1"]
    assert line1 != ""
    assert _LINE1_PATTERN.fullmatch(line1) is not None, (
        f"line1 không khớp '<Street> <number>': {line1!r}"
    )

    # City non-empty (không assert thuộc pool cụ thể — xem docstring module).
    city = result.address["city"]
    assert isinstance(city, str)
    assert city != ""

    # postal_code = "DDDD LL" — chuẩn PC6 NL.
    postal_code = result.address["postal_code"]
    assert _POSTAL_CODE_PATTERN.fullmatch(postal_code) is not None, (
        f"postal_code không khớp 'DDDD LL': {postal_code!r}"
    )

    # line2 và state rỗng theo R3.1 (không bắt buộc → giữ "" chứ không None).
    assert result.address["line2"] == ""
    assert result.address["state"] == ""
