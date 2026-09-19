"""IdealProfileGenerator — sinh `BillingAddress` locale Hà Lan.

Thuộc `payments/ideal/` (Requirement 13.1 — Payment_Module_Boundary): mọi
logic đặc thù locale NL đều nằm trong module này, KHÔNG chia sẻ code với
`random_india_profile` của UPI dù cấu trúc tương tự (Glossary
`IdealProfileGenerator`).

Vai trò: cấp đầu vào cho bước `confirm` của Stripe (Requirement 3.1) — với
`email` lấy nguyên từ account đã login (Requirement 1), còn `name` + toàn
bộ `address` sinh ngẫu nhiên theo pool NL phổ biến để tương thích với các
kiểm tra chống bot của Stripe/pay.ideal.nl.

Random source: `random.choice` module-level — KHÔNG seed cố định. Mỗi job
được sinh 1 profile độc lập; property test (Task 13.2) chỉ assert bất
biến "luôn hợp lệ theo locale NL", không phụ thuộc thứ tự sinh.

_Requirements: 3.1_
"""

from __future__ import annotations

import random
import string
from typing import Final

from app.payments.ideal.models import BillingAddress

# ---------------------------------------------------------------------------
# Pool dữ liệu locale NL — tra cứu nhanh (frozen tuple, module-level)
# ---------------------------------------------------------------------------

#: 10 tên gọi (first name) NL phổ biến — mix cả nam/nữ.
_FIRST_NAMES_NL: Final[tuple[str, ...]] = (
    "Jan",
    "Piet",
    "Maria",
    "Emma",
    "Lucas",
    "Sophie",
    "Daan",
    "Julia",
    "Sem",
    "Anna",
)

#: 10 họ (last name) NL phổ biến — giữ đúng cách viết hoa/thường của tiền tố
#: (`de`/`van den` viết thường theo chuẩn tiếng Hà Lan khi đi kèm tên riêng).
_LAST_NAMES_NL: Final[tuple[str, ...]] = (
    "de Jong",
    "Jansen",
    "de Vries",
    "van den Berg",
    "Bakker",
    "Visser",
    "Smit",
    "Meijer",
    "de Boer",
    "Mulder",
)

#: 8 tên đường NL phổ biến — hậu tố `-straat`/`-weg`/`-laan` đúng chuẩn.
_STREETS_NL: Final[tuple[str, ...]] = (
    "Kerkstraat",
    "Dorpstraat",
    "Molenweg",
    "Schoolstraat",
    "Julianalaan",
    "Wilhelminalaan",
    "Kastanjelaan",
    "Beatrixlaan",
)

#: 10 thành phố NL lớn — pool đủ đa dạng cho anti-detection.
_CITIES_NL: Final[tuple[str, ...]] = (
    "Amsterdam",
    "Rotterdam",
    "Utrecht",
    "Eindhoven",
    "Groningen",
    "Tilburg",
    "Almere",
    "Breda",
    "Nijmegen",
    "Haarlem",
)

#: Chuỗi 2 chữ cái hoa cho phần chữ của postal code NL — dùng `ascii_uppercase`
#: để đảm bảo là ký tự chữ Latin viết hoa, khớp regex `[A-Z]{2}`.
_POSTAL_LETTERS: Final[str] = string.ascii_uppercase

#: Giới hạn số nhà: Hà Lan thực tế đặt số nhà 1 tới vài nghìn — chọn 1..999
#: để giữ chuỗi ngắn gọn, tránh conflict với format `<Street> <number>` khi
#: parser đối phương (Stripe validator) áp giới hạn độ dài line1.
_HOUSE_NUMBER_MIN: Final[int] = 1
_HOUSE_NUMBER_MAX: Final[int] = 999

#: Range 4 chữ số cho postal code NL: 1000..9999. Đầu số 0xxx không tồn tại
#: trong hệ thống bưu chính NL (bắt đầu từ 1011 — Amsterdam).
_POSTAL_DIGITS_MIN: Final[int] = 1000
_POSTAL_DIGITS_MAX: Final[int] = 9999

#: Country code ISO-3166 alpha-2 cho Hà Lan — hằng số bắt buộc theo R3.1.
_COUNTRY_NL: Final[str] = "NL"


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class IdealProfileGenerator:
    """Sinh `BillingAddress` locale Hà Lan cho mỗi IdealJob (Requirement 3.1).

    Class rỗng state (không constructor arg, không attribute) — chỉ đóng
    vai trò namespace để `IdealFlowHandler` inject/mock dễ dàng khi test
    integration mà không cần patch hàm module-level.

    Thread-safety: `random.choice` và `random.randint` dùng singleton
    module `random` — an toàn khi gọi song song vì mỗi job pick độc lập
    và không chia sẻ state hiển thị. Property test (Task 13.2) sẽ verify
    tính "luôn hợp lệ" chứ không phụ thuộc reproducibility.
    """

    def generate(self, email: str) -> BillingAddress:
        """Sinh 1 `BillingAddress` hợp lệ theo locale Hà Lan.

        Toàn bộ giá trị sinh ngẫu nhiên từ các pool NL phía trên; `email`
        truyền nguyên từ account đã login (Requirement 3.1).

        Format cụ thể:
            - `country`: "NL" (bắt buộc R3.1).
            - `name`: `"<first_name> <last_name>"` — dấu cách đơn ở giữa.
            - `address.line1`: `"<Street> <house_number>"` — chuẩn NL đặt
              số nhà SAU tên đường (khác Anh/Mỹ đặt trước).
            - `address.line2`: `""` (R3.1 không bắt buộc line2; giữ rỗng
              thay vì `None` để khớp shape `dict[str, str]` của
              `BillingAddress.address`).
            - `address.city`: chọn từ pool 10 thành phố NL.
            - `address.postal_code`: `"DDDD LL"` — 4 chữ số + 1 space +
              2 chữ cái hoa (chuẩn PC6 của TNT Post NL).
            - `address.state`: `""` — Hà Lan dùng "provincie" chứ không
              dùng "state"; Stripe accept empty cho country NL.

        Args:
            email: Email của account đã login. Truyền nguyên vào
                `BillingAddress.email` — hàm này KHÔNG validate email
                (Requirement 1 đã validate trước khi login).
        """
        first_name = random.choice(_FIRST_NAMES_NL)
        last_name = random.choice(_LAST_NAMES_NL)
        name = f"{first_name} {last_name}"

        street = random.choice(_STREETS_NL)
        house_number = random.randint(_HOUSE_NUMBER_MIN, _HOUSE_NUMBER_MAX)
        line1 = f"{street} {house_number}"

        city = random.choice(_CITIES_NL)

        postal_digits = random.randint(_POSTAL_DIGITS_MIN, _POSTAL_DIGITS_MAX)
        postal_letters = (
            random.choice(_POSTAL_LETTERS) + random.choice(_POSTAL_LETTERS)
        )
        postal_code = f"{postal_digits} {postal_letters}"

        return BillingAddress(
            name=name,
            email=email,
            address={
                "country": _COUNTRY_NL,
                "line1": line1,
                "line2": "",
                "city": city,
                "postal_code": postal_code,
                "state": "",
            },
        )


__all__ = ["IdealProfileGenerator"]
