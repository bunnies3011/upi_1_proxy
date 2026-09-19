"""Property test cho `core/settings_store.py` — `SettingsRepository`/`TypeConstraint`,
tham số hoá theo TOÀN BỘ 16 key thuộc bảng whitelist Requirement 11.7 (Property 38).

**Property 38: Settings Store validate đúng theo whitelist + type constraint
cho toàn bộ key**
Dùng `hypothesis`, tham số hoá theo mọi key thuộc bảng Requirement 11.7 ×
giá trị hợp lệ/không hợp lệ theo constraint.

**Validates: Requirements 2.5, 6.5, 6.6, 8.3, 8.9, 9.1, 10.1, 11.3**

LƯU Ý VỀ PHẠM VI: `SettingsRepository.__init__` (task 5.1) chỉ tự đăng ký sẵn
7 key thuộc 3 namespace CORE dùng chung (`proxy.*`, `session_cache.*`,
`web.*`) — namespace `ideal.*` (9 key còn lại trong bảng Requirement 11.7)
CHƯA được đăng ký ở đó; việc đăng ký thật sự thuộc trách nhiệm của
`payments/ideal/__init__.py` (task 20.3), theo đúng Payment_Module_Boundary
(Requirement 13.6) — `core/settings_store.py` KHÔNG hardcode danh sách key
`ideal.*`.

Để tham số hoá đủ 16 key mà KHÔNG sửa `settings_store.py` (không vi phạm
boundary) và KHÔNG chờ task 20.3, file test này tự gọi
`repo.register_namespace("ideal", {...})` ngay trong fixture/setup của
CHÍNH property test (`_IDEAL_NAMESPACE_CONSTRAINTS` bên dưới, khớp đúng bảng
"Whitelist key đầy đủ (Requirement 11.7)" trong design.md) — phạm vi đăng ký
này chỉ tồn tại trong test process, không ảnh hưởng tới registry thật của
`SettingsRepository` khi chạy production. Khi task 20.3 implement thật,
`payments/ideal/__init__.py` PHẢI đăng ký namespace `ideal` với ĐÚNG các
constraint này để hành vi khớp với property đã verify ở đây.
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
from app.core.settings_store import SettingsRepository, TypeConstraint

# ---------------------------------------------------------------------------
# Namespace `ideal.*` mô phỏng — khớp đúng bảng Requirement 11.7 (design.md).
# ---------------------------------------------------------------------------

_POSITIVE_LOWER_BOUND = 0.001  # xấp xỉ thực dụng cho ràng buộc "> 0" (bound loại trừ).

_DEVICE_PROFILE_REQUIRED_KEYS = [
    "language",
    "time_zone",
    "screen_width",
    "screen_height",
    "screen_available_width",
    "screen_available_height",
    "color_depth",
]

_VALID_DEVICE_PROFILE = {
    "language": "nl-NL",
    "time_zone": "Europe/Amsterdam",
    "screen_width": 1920,
    "screen_height": 1080,
    "screen_available_width": 1920,
    "screen_available_height": 1040,
    "color_depth": 24,
}

_IDEAL_NAMESPACE_CONSTRAINTS: dict[str, TypeConstraint] = {
    "default_issuer": TypeConstraint(type="string", min=1),
    "known_issuers": TypeConstraint(type="list_str"),
    "max_concurrent": TypeConstraint(type="int", min=1, max=50),
    "refresh_poll_max_attempts": TypeConstraint(type="int", min=1),
    "refresh_poll_delay_seconds": TypeConstraint(type="number", min=_POSITIVE_LOWER_BOUND),
    "stripe_request_timeout_seconds": TypeConstraint(type="number", min=_POSITIVE_LOWER_BOUND),
    "stripe_max_retry_attempts": TypeConstraint(type="int", min=1),
    "stripe_retry_backoff_seconds": TypeConstraint(type="number", min=_POSITIVE_LOWER_BOUND),
    "device_profiles": TypeConstraint(
        type="list_object",
        item_schema={"required_keys": _DEVICE_PROFILE_REQUIRED_KEYS},
    ),
}

# ---------------------------------------------------------------------------
# Strategies: giá trị HỢP LỆ theo đúng TypeConstraint của từng key (16 key).
# ---------------------------------------------------------------------------

_ROTATION_MODE_ENUM = ["round_robin", "least_used"]

_valid_int_min1 = st.integers(min_value=1, max_value=100_000)
_valid_ttl_hours = st.integers(min_value=1, max_value=720)
_valid_positive_number = st.floats(
    min_value=_POSITIVE_LOWER_BOUND, max_value=100_000, allow_nan=False, allow_infinity=False
)
_valid_device_profile_list = st.lists(st.just(_VALID_DEVICE_PROFILE), max_size=5)

_VALID_STRATEGIES: dict[str, st.SearchStrategy] = {
    # 7 key CORE (đăng ký thật, task 5.1).
    "proxy.list": st.lists(st.text(), max_size=10),
    "proxy.rotation_mode": st.sampled_from(_ROTATION_MODE_ENUM),
    "proxy.max_leases_per_proxy": _valid_int_min1,
    "proxy.dead_threshold": _valid_int_min1,
    "session_cache.enabled": st.booleans(),
    "session_cache.ttl_hours": _valid_ttl_hours,
    # 9 key `ideal.*` (đăng ký mô phỏng trong test, thay cho task 20.3).
    "ideal.default_issuer": st.text(min_size=1, max_size=30),
    "ideal.known_issuers": st.lists(st.text(min_size=1, max_size=20), max_size=15),
    "ideal.max_concurrent": st.integers(min_value=1, max_value=50),
    "ideal.refresh_poll_max_attempts": _valid_int_min1,
    "ideal.refresh_poll_delay_seconds": _valid_positive_number,
    "ideal.stripe_request_timeout_seconds": _valid_positive_number,
    "ideal.stripe_max_retry_attempts": _valid_int_min1,
    "ideal.stripe_retry_backoff_seconds": _valid_positive_number,
    "ideal.device_profiles": _valid_device_profile_list,
}

# ---------------------------------------------------------------------------
# Strategies: giá trị KHÔNG HỢP LỆ (sai type HOẶC ngoài range/enum).
# ---------------------------------------------------------------------------

_wrong_type_scalars = st.one_of(
    st.text(max_size=10),
    st.integers(),
    st.booleans(),
    st.none(),
    st.floats(allow_nan=False, allow_infinity=False),
)

_invalid_list_str = st.one_of(
    st.lists(
        st.one_of(
            st.integers(), st.booleans(), st.none(), st.floats(allow_nan=False, allow_infinity=False)
        ),
        min_size=1,
        max_size=5,
    ),
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
    st.integers(max_value=0),
    st.one_of(st.text(max_size=10), st.booleans(), st.none(), st.floats(allow_nan=False, allow_infinity=False)),
)

_invalid_int_min1_max50 = st.one_of(
    st.integers(max_value=0),
    st.integers(min_value=51, max_value=100_000),
    st.one_of(st.text(max_size=10), st.booleans(), st.none(), st.floats(allow_nan=False, allow_infinity=False)),
)

_invalid_ttl_hours = st.one_of(
    st.integers(max_value=0),
    st.integers(min_value=721, max_value=100_000),
    st.one_of(st.text(max_size=10), st.booleans(), st.none(), st.floats(allow_nan=False, allow_infinity=False)),
)

_invalid_bool = st.one_of(
    st.text(max_size=10), st.integers(), st.none(), st.floats(allow_nan=False, allow_infinity=False)
)

_invalid_non_empty_string = st.one_of(
    st.just(""),
    st.integers(),
    st.booleans(),
    st.none(),
    st.lists(st.text(), max_size=3),
)

_invalid_positive_number = st.one_of(
    st.floats(max_value=0.0, allow_nan=False, allow_infinity=False),
    st.one_of(st.text(max_size=10), st.booleans(), st.none(), st.lists(st.text(), max_size=3)),
)

_invalid_device_profiles = st.one_of(
    # Đúng là list nhưng item thiếu >=1 field bắt buộc của DeviceProfile.
    st.lists(
        st.dictionaries(
            st.sampled_from(_DEVICE_PROFILE_REQUIRED_KEYS),
            st.one_of(st.text(max_size=10), st.integers()),
            max_size=len(_DEVICE_PROFILE_REQUIRED_KEYS) - 1,
        ),
        min_size=1,
        max_size=3,
    ),
    # Không phải list.
    st.one_of(st.text(), st.integers(), st.booleans(), st.none()),
)

_INVALID_STRATEGIES: dict[str, st.SearchStrategy] = {
    "proxy.list": _invalid_list_str,
    "proxy.rotation_mode": _invalid_rotation_mode,
    "proxy.max_leases_per_proxy": _invalid_int_min1,
    "proxy.dead_threshold": _invalid_int_min1,
    "session_cache.enabled": _invalid_bool,
    "session_cache.ttl_hours": _invalid_ttl_hours,
    "ideal.default_issuer": _invalid_non_empty_string,
    "ideal.known_issuers": _invalid_list_str,
    "ideal.max_concurrent": _invalid_int_min1_max50,
    "ideal.refresh_poll_max_attempts": _invalid_int_min1,
    "ideal.refresh_poll_delay_seconds": _invalid_positive_number,
    "ideal.stripe_request_timeout_seconds": _invalid_positive_number,
    "ideal.stripe_max_retry_attempts": _invalid_int_min1,
    "ideal.stripe_retry_backoff_seconds": _invalid_positive_number,
    "ideal.device_profiles": _invalid_device_profiles,
}

_ALL_KEYS = sorted(_VALID_STRATEGIES)


def _run(coro):
    """Chạy coroutine trong 1 event loop mới — pattern chuẩn khi kết hợp
    `hypothesis` (sync `@given`) với code async."""
    return asyncio.run(coro)


async def _new_repository_with_ideal_namespace(tmp_dir: str) -> SettingsRepository:
    """Tạo `SettingsRepository` mới trên 1 file SQLite riêng trong `tmp_dir`,
    đăng ký thêm namespace `ideal` mô phỏng (thay cho task 20.3, CHƯA
    implement) để tham số hoá được đủ 16 key theo Requirement 11.7.

    Mỗi hypothesis example dùng 1 thư mục tạm hoàn toàn mới — cách ly trạng
    thái tuyệt đối giữa các lần thử ngẫu nhiên.
    """
    engine = DbEngine(Path(tmp_dir) / "ideal_qr_tool.db")
    await engine.init_schema()
    repo = SettingsRepository(engine)
    repo.register_namespace("ideal", _IDEAL_NAMESPACE_CONSTRAINTS)
    return repo


def test_all_whitelist_keys_are_covered() -> None:
    """Đảm bảo bảng strategy ở trên khớp đúng 15 key (6 core + 9 ideal.*)
    theo bảng "Whitelist key đầy đủ" trong design.md (auth đã bỏ nên
    `web.auth_token` không còn trong whitelist)."""
    assert len(_ALL_KEYS) == 15
    assert set(_VALID_STRATEGIES) == set(_INVALID_STRATEGIES)


@pytest.mark.parametrize("key", _ALL_KEYS)
@given(data=st.data())
@settings(deadline=None, max_examples=20)
def test_valid_value_accepted_and_round_trips(key: str, data: st.DataObject) -> None:
    """Giá trị HỢP LỆ theo constraint thực tế của key → `set`/`bulk_set`
    KHÔNG raise và `get` sau đó trả đúng giá trị đã set."""
    value = data.draw(_VALID_STRATEGIES[key])
    use_bulk = data.draw(st.booleans())

    async def _body() -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = await _new_repository_with_ideal_namespace(tmp_dir)

            if use_bulk:
                await repo.bulk_set({key: value})
            else:
                await repo.set(key, value)

            assert await repo.get(key) == value

    _run(_body())


@pytest.mark.parametrize("key", _ALL_KEYS)
@given(data=st.data())
@settings(deadline=None, max_examples=20)
def test_invalid_value_rejected_and_old_value_preserved(key: str, data: st.DataObject) -> None:
    """Giá trị KHÔNG HỢP LỆ (sai type/ngoài range/ngoài enum) → `set`/`bulk_set`
    luôn raise `SettingsValidationError` và giá trị đã set trước đó (nếu có)
    KHÔNG bị thay đổi."""
    baseline = data.draw(_VALID_STRATEGIES[key])
    invalid_value = data.draw(_INVALID_STRATEGIES[key])
    use_bulk = data.draw(st.booleans())

    async def _body() -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = await _new_repository_with_ideal_namespace(tmp_dir)
            await repo.set(key, baseline)

            with pytest.raises(SettingsValidationError):
                if use_bulk:
                    await repo.bulk_set({key: invalid_value})
                else:
                    await repo.set(key, invalid_value)

            assert await repo.get(key) == baseline

    _run(_body())
