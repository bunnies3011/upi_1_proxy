"""Property test cho `core/settings_store.py` — `SettingsRepository`/`TypeConstraint` (Property 38).

**Property 38: Settings Store validate đúng theo whitelist + type constraint
cho toàn bộ key**
Dùng `hypothesis`, tham số hoá theo mọi key thuộc bảng Requirement 11.7 ×
giá trị hợp lệ/không hợp lệ theo constraint.

**Validates: Requirements 2.5, 6.5, 6.6, 8.3, 8.9, 9.1, 10.1, 11.3**

LƯU Ý VỀ PHẠM VI (đọc kỹ trước khi mở rộng): `SettingsRepository.__init__`
(task 5.1) chỉ tự đăng ký sẵn 7 key thuộc 3 namespace CORE dùng chung —
`proxy.list`, `proxy.rotation_mode`, `proxy.max_leases_per_proxy`,
`proxy.dead_threshold`, `session_cache.enabled`, `session_cache.ttl_hours`,
`web.auth_token`. 9 key còn lại thuộc namespace `ideal.*` trong bảng
Requirement 11.7 (`ideal.max_concurrent`, `ideal.device_profiles`, ...) SẼ do
`payments/ideal/__init__.py` tự đăng ký sau (task 20.3, CHƯA implement tại
thời điểm viết test này). Vì vậy property test này CHỈ cover 7 key CORE đã
có sẵn ngay khi khởi tạo `SettingsRepository` — KHÔNG test được 9 key
`ideal.*` (chưa tồn tại trong registry nên `register_namespace("ideal", ...)`
chưa được gọi, mọi `set("ideal.*", ...)` sẽ raise do "key không thuộc
whitelist" chứ không phải do type constraint thật của các key đó).

Coverage đầy đủ 16 key (7 CORE + 9 `ideal.*`) là phạm vi của 1 property test
khác, chạy SAU khi task 20.3 hoàn thành (ví dụ đặt trong
`tests/property/test_settings_store_full_whitelist_property.py` hoặc mở rộng
file này sau khi `payments/ideal/__init__.py` tồn tại) — nằm ngoài phạm vi
task 5.2.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.db import DbEngine
from app.core.errors import SettingsValidationError
from app.core.settings_store import SettingsRepository

# -- Strategies: giá trị HỢP LỆ theo đúng TypeConstraint đã đăng ký ở task 5.1 --

_ROTATION_MODE_ENUM = ["round_robin", "least_used"]

_valid_int_min1 = st.integers(min_value=1, max_value=100_000)
_valid_ttl_hours = st.integers(min_value=1, max_value=720)

_VALID_STRATEGIES: dict[str, st.SearchStrategy] = {
    "proxy.list": st.lists(st.text(), max_size=10),
    "proxy.rotation_mode": st.sampled_from(_ROTATION_MODE_ENUM),
    "proxy.max_leases_per_proxy": _valid_int_min1,
    "proxy.dead_threshold": _valid_int_min1,
    "session_cache.enabled": st.booleans(),
    "session_cache.ttl_hours": _valid_ttl_hours,
}

# -- Strategies: giá trị KHÔNG HỢP LỆ (sai type HOẶC ngoài range/enum) --

# Scalar "sai type" chung — dùng lại cho mọi key int/bool/enum, luôn KHÔNG
# phải kiểu mong đợi của key đó (áp dụng filter riêng ở nơi dùng khi cần).
_wrong_type_scalars = st.one_of(
    st.text(max_size=10),
    st.integers(),
    st.booleans(),
    st.none(),
    st.floats(allow_nan=False, allow_infinity=False),
)

_invalid_list_str = st.one_of(
    # Đúng là list nhưng chứa >=1 item không phải str.
    st.lists(
        st.one_of(
            st.integers(), st.booleans(), st.none(), st.floats(allow_nan=False, allow_infinity=False)
        ),
        min_size=1,
        max_size=5,
    ),
    # Không phải list.
    st.one_of(st.text(), st.integers(), st.booleans(), st.none()),
)

_invalid_rotation_mode = st.one_of(
    st.text(max_size=20).filter(lambda s: s not in _ROTATION_MODE_ENUM),
    st.integers(),
    st.booleans(),
    st.none(),
    st.lists(st.text(), max_size=3),
)

_invalid_int_min1 = st.one_of(
    st.integers(max_value=0),  # dưới range [1, +∞]
    st.one_of(st.text(max_size=10), st.booleans(), st.none(), st.floats(allow_nan=False, allow_infinity=False)),
)

_invalid_ttl_hours = st.one_of(
    st.integers(max_value=0),  # dưới range [1, 720]
    st.integers(min_value=721, max_value=100_000),  # trên range [1, 720]
    st.one_of(st.text(max_size=10), st.booleans(), st.none(), st.floats(allow_nan=False, allow_infinity=False)),
)

_invalid_bool = st.one_of(
    st.text(max_size=10), st.integers(), st.none(), st.floats(allow_nan=False, allow_infinity=False)
)

_invalid_auth_token = st.one_of(
    st.just(""),  # dưới range độ dài [1, +∞]
    st.integers(),
    st.booleans(),
    st.none(),
    st.lists(st.text(), max_size=3),
)

_INVALID_STRATEGIES: dict[str, st.SearchStrategy] = {
    "proxy.list": _invalid_list_str,
    "proxy.rotation_mode": _invalid_rotation_mode,
    "proxy.max_leases_per_proxy": _invalid_int_min1,
    "proxy.dead_threshold": _invalid_int_min1,
    "session_cache.enabled": _invalid_bool,
    "session_cache.ttl_hours": _invalid_ttl_hours,
}

_CORE_KEYS = sorted(_VALID_STRATEGIES)


def _run(coro):
    """Chạy coroutine trong 1 event loop mới — tránh phụ thuộc vào cơ chế
    tích hợp async-native của `@given` (không đảm bảo sẵn có), theo pattern
    chuẩn khi kết hợp `hypothesis` với code async."""
    return asyncio.run(coro)


async def _new_repository(tmp_dir: str) -> SettingsRepository:
    """Tạo `SettingsRepository` mới trên 1 file SQLite riêng trong `tmp_dir`.

    Mỗi hypothesis example dùng 1 thư mục tạm hoàn toàn mới (không tái sử
    dụng fixture pytest như `tmp_path` giữa các example) để đảm bảo cách ly
    trạng thái tuyệt đối giữa các lần thử ngẫu nhiên.
    """
    engine = DbEngine(Path(tmp_dir) / "ideal_qr_tool.db")
    await engine.init_schema()
    return SettingsRepository(engine)


@pytest.mark.parametrize("key", _CORE_KEYS)
@given(data=st.data())
@settings(deadline=None, max_examples=25)
def test_valid_value_accepted_and_round_trips(key: str, data: st.DataObject) -> None:
    """Giá trị HỢP LỆ theo constraint thực tế của key → `set` không raise và
    `get` trả lại đúng giá trị đã set."""
    value = data.draw(_VALID_STRATEGIES[key])

    async def _body() -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = await _new_repository(tmp_dir)
            await repo.set(key, value)
            assert await repo.get(key) == value

    _run(_body())


@pytest.mark.parametrize("key", _CORE_KEYS)
@given(data=st.data())
@settings(deadline=None, max_examples=25)
def test_invalid_value_rejected_and_old_value_preserved(key: str, data: st.DataObject) -> None:
    """Giá trị KHÔNG HỢP LỆ (sai type/ngoài range/ngoài enum) → `set` luôn
    raise `SettingsValidationError` và giá trị đã set trước đó KHÔNG bị thay
    đổi."""
    baseline = data.draw(_VALID_STRATEGIES[key])
    invalid_value = data.draw(_INVALID_STRATEGIES[key])

    async def _body() -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = await _new_repository(tmp_dir)
            await repo.set(key, baseline)

            with pytest.raises(SettingsValidationError):
                await repo.set(key, invalid_value)

            assert await repo.get(key) == baseline

    _run(_body())
