"""Property test cho `payments/ideal/device_profile.DeviceProfileAllocator.pick_for_job`
— DeviceProfile cố định trong vòng đời 1 job (Property 15).

**Property 15: DeviceProfile cố định trong vòng đời 1 job**

Với mọi `job_id` bất kỳ + mọi danh sách `ideal.device_profiles` ngẫu nhiên
(≥ 1 phần tử, locale NL hợp lệ), gọi `DeviceProfileAllocator.pick_for_job(job_id)`
N lần liên tiếp với CÙNG `job_id` → luôn trả về CÙNG 1 `DeviceProfile` instance
(cache theo `job_id` trong vòng đời `IdealFlowHandler.run()`, R5.1).

Ràng buộc bổ sung để hoàn thiện contract R5.3 (round-robin giữa các job khác
nhau) — property phụ đi kèm cùng file:

- Danh sách có 1 profile → 2 `job_id` khác nhau đều nhận cùng 1 profile
  (round-robin `cursor % 1 == 0`).
- Danh sách có ≥ 2 profile → 2 `job_id` khác nhau nhận profile theo thứ tự
  round-robin (index 0 rồi index 1 → 2 profile khác nhau).

**Validates: Requirements 5.1, 5.3**

Ghi chú tổ chức file:

- FakeSettings triển khai đúng surface mà `DeviceProfileAllocator` tiêu thụ
  (`async def get(key)` — cùng chữ ký với `SettingsRepository.get`). KHÔNG
  mock qua `unittest.mock.AsyncMock` để giữ contract rõ ràng và test không
  phụ thuộc chi tiết implementation của mock library.
- `pick_for_job` là `async` → wrap qua `asyncio.run(...)` để tương thích
  `hypothesis` sync `@given` (cùng pattern với các test session_cache trong
  repo).
- Hypothesis sinh 7 field DeviceProfile hợp lệ theo `models.DeviceProfile`
  dataclass shape + locale NL bắt buộc (`language="nl-NL"`,
  `timeZone="Europe/Amsterdam"` — R5.2). 5 field số sinh trong range hợp
  lý (không âm, không quá lớn) để loại trừ giá trị vô nghĩa nhưng không
  làm hẹp counter-example space cho R5.1/R5.3.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.payments.ideal.device_profile import DeviceProfileAllocator
from app.payments.ideal.models import DeviceProfile

# ---------------------------------------------------------------------------
# Locale NL bắt buộc theo Requirement 5.2 — allocator không validate lại
# runtime nhưng danh sách được `SettingsRepository` validate trước khi lưu,
# nên test sinh mọi profile với locale NL hợp lệ (property này không cover
# nhánh locale sai — thuộc phạm vi Property 16 ở file
# `test_device_profile_validate_property.py`).
# ---------------------------------------------------------------------------

_VALID_LANGUAGE = "nl-NL"
_VALID_TIME_ZONE = "Europe/Amsterdam"


class _FakeSettings:
    """Stub tối thiểu cho `SettingsRepository` chỉ phục vụ `DeviceProfileAllocator`.

    Cung cấp `async def get(key)` — chữ ký khớp `SettingsRepository.get`
    (allocator chỉ đọc key `ideal.device_profiles` qua method này, KHÔNG
    dùng `list`/`bulk_get`). Snapshot cố định trong toàn bộ vòng đời stub
    — đủ để property test kiểm tra hành vi cache/round-robin mà không cần
    setup DB thật.
    """

    def __init__(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot

    async def get(self, key: str) -> Any | None:
        return self._snapshot.get(key)


# ---------------------------------------------------------------------------
# Strategy sinh 1 DeviceProfile dict hợp lệ (locale NL + 5 field số trong
# range dương). Field int giữ range vừa phải để tránh sinh giá trị âm vô
# nghĩa nhưng không quá hẹp — allocator không quan tâm giá trị cụ thể của
# 5 field này, chỉ cần shape khớp `DeviceProfile.__init__`.
# ---------------------------------------------------------------------------

_screen_dim_strategy = st.integers(min_value=1, max_value=8192)
_color_depth_strategy = st.integers(min_value=1, max_value=64)

_device_profile_dict_strategy = st.fixed_dictionaries(
    {
        "language": st.just(_VALID_LANGUAGE),
        "timeZone": st.just(_VALID_TIME_ZONE),
        "screenWidth": _screen_dim_strategy,
        "screenHeight": _screen_dim_strategy,
        "screenAvailableWidth": _screen_dim_strategy,
        "screenAvailableHeight": _screen_dim_strategy,
        "colorDepth": _color_depth_strategy,
    }
)

# Danh sách profile ≥ 1 phần tử. Max 5 để giữ test nhanh — R5.1/R5.3 không
# phụ thuộc vào độ dài lớn, chỉ cần ≥ 1 để pick được và ≥ 2 để round-robin
# tạo ra 2 profile khác nhau (property phụ có case riêng cho từng nhánh).
_device_profiles_list_strategy = st.lists(
    _device_profile_dict_strategy, min_size=1, max_size=5
)

_job_id_strategy = st.uuids().map(str)


def _run(coro):
    """Chạy coroutine trong 1 event loop mới — pattern chuẩn của repo khi
    kết hợp `hypothesis` (sync `@given`) với code async (xem cách dùng ở
    `test_session_cache_*_property.py`)."""
    return asyncio.run(coro)


def _make_allocator(profiles: list[dict[str, Any]]) -> DeviceProfileAllocator:
    """Khởi tạo allocator với FakeSettings snapshot chỉ chứa 1 key duy nhất
    `ideal.device_profiles` = `profiles`."""
    fake_settings = _FakeSettings({"ideal.device_profiles": profiles})
    # type: ignore[arg-type] — FakeSettings duck-type khớp `SettingsRepository.get`.
    return DeviceProfileAllocator(settings=fake_settings)  # type: ignore[arg-type]


@given(
    profiles=_device_profiles_list_strategy,
    job_id=_job_id_strategy,
    n_calls=st.integers(min_value=2, max_value=5),
)
@settings(
    max_examples=50,
    # Allocator dùng `asyncio.Lock()` khởi tạo trong __init__ → gắn với event
    # loop hiện tại. Mỗi hypothesis example tạo instance mới trong `asyncio.run`
    # riêng nên KHÔNG bị leak state giữa các example — tắt cảnh báo
    # `function_scoped_fixture` (không dùng fixture) và `filter_too_much`
    # (strategies đủ rộng, không filter).
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_pick_for_job_returns_same_profile_across_repeated_calls(
    profiles: list[dict[str, Any]], job_id: str, n_calls: int
) -> None:
    """R5.1: Gọi `pick_for_job(job_id)` N lần với cùng `job_id` → luôn trả
    về CÙNG 1 `DeviceProfile` instance (identity check `is`), bất kể danh
    sách profile có bao nhiêu phần tử."""

    async def _scenario() -> DeviceProfile:
        allocator = _make_allocator(profiles)
        first = await allocator.pick_for_job(job_id)
        # Gọi thêm N-1 lần → toàn bộ phải cùng instance với lần đầu (identity
        # `is` — cache theo `job_id` giữ nguyên object, không tạo copy).
        for _ in range(n_calls - 1):
            subsequent = await allocator.pick_for_job(job_id)
            assert subsequent is first, (
                f"pick_for_job('{job_id}') phải trả cùng instance với lần đầu "
                f"(cache theo job_id — R5.1). Nhận instance khác: "
                f"first={first!r}, subsequent={subsequent!r}."
            )
        return first

    _run(_scenario())


@given(profiles=_device_profiles_list_strategy)
@settings(
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_pick_for_job_round_robin_across_distinct_job_ids(
    profiles: list[dict[str, Any]],
) -> None:
    """R5.3: 2 `job_id` khác nhau, danh sách profile ≥ 2 phần tử → nhận
    profile theo thứ tự round-robin (index 0 rồi index 1, hai profile
    khác nhau theo `_pick_cursor % len(profiles)`).

    Nếu danh sách chỉ có 1 profile → cả 2 `job_id` đều nhận cùng profile
    duy nhất (round-robin `cursor % 1 == 0` luôn, không mâu thuẫn R5.3)."""
    job_id_a = str(uuid.uuid4())
    job_id_b = str(uuid.uuid4())
    # `uuid4()` gần như chắc chắn không trùng nhau — assert để bảo hiểm nếu
    # có regression cực hiếm (test sẽ fail rõ ràng thay vì bị "true trivially").
    assert job_id_a != job_id_b

    async def _scenario() -> None:
        allocator = _make_allocator(profiles)
        profile_a = await allocator.pick_for_job(job_id_a)
        profile_b = await allocator.pick_for_job(job_id_b)

        if len(profiles) == 1:
            # Round-robin trên list 1 phần tử → cùng 1 profile cho mọi job_id.
            assert profile_a == profile_b, (
                "Danh sách 1 profile → mọi job_id đều nhận cùng profile duy "
                f"nhất (R5.3). Nhận: {profile_a!r} vs {profile_b!r}."
            )
        else:
            # Round-robin trên list ≥ 2 phần tử → job_a nhận index 0, job_b
            # nhận index 1 → phải KHÁC NHAU (dữ liệu 7 field khớp với 2
            # profile khác nhau trong `profiles`).
            expected_a = DeviceProfile(**profiles[0])
            expected_b = DeviceProfile(**profiles[1])
            assert profile_a == expected_a, (
                f"job_a phải nhận profile index 0 (round-robin R5.3). "
                f"Kỳ vọng: {expected_a!r}, nhận: {profile_a!r}."
            )
            assert profile_b == expected_b, (
                f"job_b phải nhận profile index 1 (round-robin R5.3). "
                f"Kỳ vọng: {expected_b!r}, nhận: {profile_b!r}."
            )

    _run(_scenario())


@given(
    profiles=_device_profiles_list_strategy,
    job_id=_job_id_strategy,
)
@settings(
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_pick_for_job_cache_survives_interleaved_calls_from_other_jobs(
    profiles: list[dict[str, Any]], job_id: str
) -> None:
    """R5.1 + R5.3 kết hợp: `job_id` cố định giữ nguyên profile ngay cả
    khi có `job_id` KHÁC được pick xen giữa (cursor round-robin bị tăng
    bởi job khác không được phép làm dịch chuyển profile đã cache của
    `job_id` này)."""
    other_job_id = str(uuid.uuid4())
    # Cực hiếm khi trùng, nhưng vẫn phòng ngừa.
    assert other_job_id != job_id

    async def _scenario() -> None:
        allocator = _make_allocator(profiles)
        first = await allocator.pick_for_job(job_id)
        # Xen 3 lần pick của `other_job_id` — mỗi lần đều đẩy cursor tiến,
        # nhưng KHÔNG được ảnh hưởng profile của `job_id` gốc (cache đã fill).
        for _ in range(3):
            await allocator.pick_for_job(other_job_id)
        again = await allocator.pick_for_job(job_id)

        assert again is first, (
            f"Profile của job_id='{job_id}' phải giữ nguyên identity dù có "
            f"job khác pick xen giữa (R5.1). first={first!r}, again={again!r}."
        )

    _run(_scenario())
