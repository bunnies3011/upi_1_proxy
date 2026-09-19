"""Property test cho `payments/ideal/models.validate_device_profile` — validate
DeviceProfile locale NL tại thời điểm LƯU (Property 16).

**Property 16: Validate DeviceProfile locale NL tại thời điểm lưu**

Với mọi DeviceProfile ngẫu nhiên (`language`/`timeZone` đúng hoặc sai locale
NL), `validate_device_profile(profile)`:

- Nếu `language == "nl-NL"` VÀ `timeZone == "Europe/Amsterdam"` → KHÔNG raise.
- Nếu `language != "nl-NL"` HOẶC `timeZone != "Europe/Amsterdam"` → raise
  `ValueError` (KHÔNG phải `IdealFlowError` vì đây là lỗi validate settings
  ở boundary Settings_Store, không phải lỗi phát sinh khi job đang chạy).

**Validates: Requirements 5.2**

Ghi chú tổ chức file:
- Test dùng `@given` sinh 2 field locale (`language`, `timeZone`) từ union
  của `st.just(<đúng>)` và `st.text(...).filter(<khác đúng>)`. `filter` đảm
  bảo nhánh "sai" thực sự không trùng giá trị locale NL — không có case biên
  nào lọt qua khiến property bị "true trivially".
- 5 field số khác (`screenWidth`, `screenHeight`, `screenAvailableWidth`,
  `screenAvailableHeight`, `colorDepth`) fix giá trị hợp lệ (1920/1080/24)
  vì Requirement 5.2 CHỈ ràng buộc 2 field locale — property này không phụ
  thuộc giá trị các field còn lại, giữ chúng cố định để hypothesis tập trung
  shrink counter-example vào đúng 2 field locale khi có regression.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.payments.ideal.models import validate_device_profile

# ---------------------------------------------------------------------------
# Giá trị locale NL bắt buộc theo Requirement 5.2 — khớp CHÍNH XÁC hằng số
# `_REQUIRED_LANGUAGE` / `_REQUIRED_TIME_ZONE` bên trong `models.py`.
# Không import 2 hằng số private đó (khỏi phá encapsulation của module),
# viết lại tại đây như "hợp đồng bên ngoài" mà property test kiểm thử.
# ---------------------------------------------------------------------------

_VALID_LANGUAGE = "nl-NL"
_VALID_TIME_ZONE = "Europe/Amsterdam"

# ---------------------------------------------------------------------------
# 5 field số cố định — Requirement 5.2 chỉ ràng buộc `language`/`timeZone`,
# 5 field còn lại chỉ cần hiện diện với giá trị int hợp lệ để dict được coi
# là "1 DeviceProfile đầy đủ shape". Fix cứng theo cấu hình màn hình phổ
# biến FullHD 24-bit — nhất quán với sample data thường gặp ở Settings_Store.
# ---------------------------------------------------------------------------

_FIXED_SCREEN_WIDTH = 1920
_FIXED_SCREEN_HEIGHT = 1080
_FIXED_SCREEN_AVAILABLE_WIDTH = 1920
_FIXED_SCREEN_AVAILABLE_HEIGHT = 1040
_FIXED_COLOR_DEPTH = 24


@given(
    language=st.one_of(
        st.just(_VALID_LANGUAGE),
        st.text(min_size=1, max_size=10).filter(lambda s: s != _VALID_LANGUAGE),
    ),
    timeZone=st.one_of(
        st.just(_VALID_TIME_ZONE),
        st.text(min_size=1, max_size=30).filter(lambda s: s != _VALID_TIME_ZONE),
    ),
)
@settings(max_examples=100)
def test_validate_device_profile_enforces_locale_nl(
    language: str, timeZone: str
) -> None:
    """R5.2: `validate_device_profile` chấp nhận đúng khi cả 2 field locale
    khớp `nl-NL` / `Europe/Amsterdam`; từ chối (raise `ValueError`) khi 1
    trong 2 field khác giá trị locale NL — không có counter-example nào."""
    profile = {
        "language": language,
        "timeZone": timeZone,
        "screenWidth": _FIXED_SCREEN_WIDTH,
        "screenHeight": _FIXED_SCREEN_HEIGHT,
        "screenAvailableWidth": _FIXED_SCREEN_AVAILABLE_WIDTH,
        "screenAvailableHeight": _FIXED_SCREEN_AVAILABLE_HEIGHT,
        "colorDepth": _FIXED_COLOR_DEPTH,
    }

    is_locale_nl = language == _VALID_LANGUAGE and timeZone == _VALID_TIME_ZONE

    if is_locale_nl:
        # Locale NL đầy đủ → không được raise. Nếu raise → property vi phạm
        # (locale đúng bị từ chối). Dùng try/except để bắt cả ValueError lẫn
        # bất kỳ exception phụ nào có thể lọt qua, biến thành failure message
        # tường minh thay vì pytest báo error chung.
        try:
            validate_device_profile(profile)
        except ValueError as exc:  # noqa: BLE001 — chỉ ValueError theo contract R5.2.
            pytest.fail(
                "validate_device_profile phải KHÔNG raise khi locale NL đủ, "
                f"nhận language={language!r}, timeZone={timeZone!r}, "
                f"exception={exc!r}."
            )
    else:
        # Ít nhất 1 field khác locale NL → phải raise `ValueError`.
        with pytest.raises(ValueError):
            validate_device_profile(profile)
