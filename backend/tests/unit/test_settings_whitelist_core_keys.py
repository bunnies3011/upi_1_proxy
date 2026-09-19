"""Unit test whitelist đầy đủ 15 key đúng type/range theo Requirement 11.7.

Bảng đầy đủ 15 key nằm ở design.md — "Whitelist key đầy đủ (Requirement 11.7)".
File này verify 2 nhóm, mỗi nhóm test chi tiết giá trị hợp lệ VÀ không hợp lệ
theo đúng type/range/enum của từng key:

- PHẦN A (6 KEY CORE): `SettingsRepository` (task 5.1) tự đăng ký sẵn 2
  namespace `proxy.*` / `session_cache.*` khi khởi tạo — test dùng trực tiếp
  registry THẬT, không mock. (Namespace `web` với `web.auth_token` đã bị bỏ
  cùng việc gỡ Auth hoàn toàn.)

- PHẦN B (9 KEY `ideal.*`): namespace `ideal` CHƯA được đăng ký ở thời điểm
  hiện tại — `payments/ideal/__init__.py` (task 20.3) mới là nơi đăng ký thật.
  Để verify được cả 15/15 key mà không phải chờ task 20.3, test tự gọi
  `register_namespace("ideal", {...})` với type constraint mô phỏng ĐÚNG theo
  bảng Requirement 11.7 (xem `_IDEAL_NAMESPACE_CONSTRAINTS` bên dưới). Khi
  task 20.3 implement thật, `payments/ideal/__init__.py` PHẢI đăng ký namespace
  `ideal` với ĐÚNG các constraint này để hành vi khớp với test đã pass ở đây.

Ghi chú diễn giải (do `TypeConstraint` hiện tại KHÔNG hỗ trợ range/enum động
phụ thuộc giá trị field khác, cũng KHÔNG hỗ trợ bound loại trừ/"exclusive"):

- `ideal.default_issuer` ("thuộc `ideal.known_issuers`"): đây là ràng buộc
  CROSS-FIELD (giá trị hợp lệ phụ thuộc runtime value của 1 key khác) —
  `TypeConstraint` chỉ hỗ trợ `enum` tĩnh cố định tại thời điểm
  `register_namespace`, không thể diễn tả "thuộc key khác". Test ở đây chỉ
  verify type constraint chung (string non-empty); việc khớp với
  `ideal.known_issuers` là business rule thuộc `IssuerSelector`
  (Property 18), KHÔNG thuộc trách nhiệm generic của Settings Store.
- `ideal.known_issuers` ("≥ 11 mã khởi tạo"): đây là invariant về SỐ LƯỢNG
  SEED DATA lúc khởi tạo (task 20.3 tự đăng ký sẵn ≥ 11 mã), KHÔNG phải range
  bắt buộc mỗi lần `set` sau đó (user vẫn có thể chủ động rút gọn danh sách).
  Test ở đây chỉ verify type constraint list-of-string; số lượng ≥ 11 mã khởi
  tạo được test riêng ở task 35.6.
- `ideal.refresh_poll_delay_seconds` / `stripe_request_timeout_seconds` /
  `stripe_retry_backoff_seconds` (đều "> 0", tức bound loại trừ 0): do
  `TypeConstraint.min` là bound BAO GỒM ("inclusive"), test dùng
  `_POSITIVE_LOWER_BOUND = 0.001` làm xấp xỉ thực dụng cho "> 0" (0 luôn là
  giá trị không hợp lệ trong toàn bộ test case dưới đây).
- `ideal.device_profiles` (mỗi object khớp DeviceProfile, locale NL): test
  chỉ verify shape (đủ 7 field bắt buộc của `DeviceProfile` qua `item_schema`)
  — validate GIÁ TRỊ locale NL cụ thể (ví dụ `language == "nl-NL"`) là business
  rule thuộc `DeviceProfileAllocator.validate_profile` (Property 16), không
  phải type constraint generic của Settings Store.

Requirements: 11.7.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.db import DbEngine
from app.core.errors import SettingsValidationError
from app.core.settings_store import SettingsRepository, TypeConstraint

_POSITIVE_LOWER_BOUND = 0.001

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

# Constraint mô phỏng ĐÚNG theo bảng Requirement 11.7 cho 9 key `ideal.*` —
# task 20.3 (`payments/ideal/__init__.py`) PHẢI đăng ký namespace `ideal` với
# constraint khớp với bảng dưới đây để giữ tính nhất quán với test này.
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


async def _make_core_repository(tmp_path: Path) -> SettingsRepository:
    """Repo THẬT — chỉ 7 key core tự đăng ký sẵn, KHÔNG mock gì thêm."""
    engine = DbEngine(tmp_path / "ideal_qr_tool.db")
    await engine.init_schema()
    return SettingsRepository(engine)


async def _make_repository_with_simulated_ideal_namespace(tmp_path: Path) -> SettingsRepository:
    """Repo THẬT + namespace `ideal` được mô phỏng đăng ký (thay cho task 20.3)."""
    repo = await _make_core_repository(tmp_path)
    repo.register_namespace("ideal", _IDEAL_NAMESPACE_CONSTRAINTS)
    return repo


# ---------------------------------------------------------------------------
# PHẦN A — 7 KEY CORE (registry thật của SettingsRepository)
# ---------------------------------------------------------------------------

_CORE_KEY_VALID_CASES: list[tuple[str, object]] = [
    ("proxy.list", []),
    ("proxy.list", ["1.2.3.4:8080"]),
    ("proxy.list", ["1.2.3.4:8080", "socks5://5.6.7.8:1080"]),
    ("proxy.rotation_mode", "round_robin"),
    ("proxy.rotation_mode", "least_used"),
    ("proxy.max_leases_per_proxy", 1),
    ("proxy.max_leases_per_proxy", 100),
    ("proxy.dead_threshold", 1),
    ("proxy.dead_threshold", 3),
    ("session_cache.enabled", True),
    ("session_cache.enabled", False),
    ("session_cache.ttl_hours", 1),
    ("session_cache.ttl_hours", 24),
    ("session_cache.ttl_hours", 720),
]

_CORE_KEY_INVALID_CASES: list[tuple[str, object]] = [
    ("proxy.list", "not-a-list"),
    ("proxy.list", [1, 2, 3]),
    ("proxy.list", ["ok", 5]),
    ("proxy.rotation_mode", "random_mode"),
    ("proxy.rotation_mode", ""),
    ("proxy.rotation_mode", 123),
    ("proxy.max_leases_per_proxy", 0),
    ("proxy.max_leases_per_proxy", -1),
    ("proxy.max_leases_per_proxy", 1.5),
    ("proxy.max_leases_per_proxy", "1"),
    ("proxy.max_leases_per_proxy", True),
    ("proxy.dead_threshold", 0),
    ("proxy.dead_threshold", -5),
    ("proxy.dead_threshold", 2.5),
    ("session_cache.enabled", 1),
    ("session_cache.enabled", 0),
    ("session_cache.enabled", "true"),
    ("session_cache.ttl_hours", 0),
    ("session_cache.ttl_hours", 721),
    ("session_cache.ttl_hours", -1),
    ("session_cache.ttl_hours", 24.5),
    ("session_cache.ttl_hours", "24"),
]


@pytest.mark.parametrize("key,value", _CORE_KEY_VALID_CASES)
async def test_core_key_accepts_valid_value(tmp_path: Path, key: str, value: object) -> None:
    repo = await _make_core_repository(tmp_path)

    await repo.set(key, value)

    assert await repo.get(key) == value


@pytest.mark.parametrize("key,value", _CORE_KEY_INVALID_CASES)
async def test_core_key_rejects_invalid_value(tmp_path: Path, key: str, value: object) -> None:
    repo = await _make_core_repository(tmp_path)

    with pytest.raises(SettingsValidationError):
        await repo.set(key, value)

    # Fail_Fast_Policy: giá trị không hợp lệ không được ghi vào DB.
    assert await repo.get(key) is None


def test_core_key_table_covers_all_6_core_keys() -> None:
    """Đảm bảo bảng test case ở trên không bỏ sót key core nào trong whitelist."""
    expected_keys = {
        "proxy.list",
        "proxy.rotation_mode",
        "proxy.max_leases_per_proxy",
        "proxy.dead_threshold",
        "session_cache.enabled",
        "session_cache.ttl_hours",
    }
    covered_keys = {key for key, _ in _CORE_KEY_VALID_CASES}
    assert covered_keys == expected_keys


# ---------------------------------------------------------------------------
# PHẦN B — 9 KEY `ideal.*` (namespace mô phỏng, thay cho task 20.3)
# ---------------------------------------------------------------------------

_IDEAL_KEY_VALID_CASES: list[tuple[str, object]] = [
    ("ideal.default_issuer", "ideal_ABNANL2A"),
    ("ideal.default_issuer", "ideal_INGBNL2A"),
    ("ideal.known_issuers", []),
    ("ideal.known_issuers", ["ideal_ABNANL2A", "ideal_INGBNL2A"]),
    ("ideal.max_concurrent", 1),
    ("ideal.max_concurrent", 5),
    ("ideal.max_concurrent", 50),
    ("ideal.refresh_poll_max_attempts", 1),
    ("ideal.refresh_poll_max_attempts", 20),
    ("ideal.refresh_poll_delay_seconds", _POSITIVE_LOWER_BOUND),
    ("ideal.refresh_poll_delay_seconds", 2.5),
    ("ideal.stripe_request_timeout_seconds", _POSITIVE_LOWER_BOUND),
    ("ideal.stripe_request_timeout_seconds", 30),
    ("ideal.stripe_max_retry_attempts", 1),
    ("ideal.stripe_max_retry_attempts", 3),
    ("ideal.stripe_retry_backoff_seconds", _POSITIVE_LOWER_BOUND),
    ("ideal.stripe_retry_backoff_seconds", 0.5),
    ("ideal.device_profiles", []),
    ("ideal.device_profiles", [_VALID_DEVICE_PROFILE]),
]

_IDEAL_KEY_INVALID_CASES: list[tuple[str, object]] = [
    ("ideal.default_issuer", ""),
    ("ideal.default_issuer", 123),
    ("ideal.known_issuers", "not-a-list"),
    ("ideal.known_issuers", [1, 2]),
    ("ideal.known_issuers", ["ok", 5]),
    ("ideal.max_concurrent", 0),
    ("ideal.max_concurrent", 51),
    ("ideal.max_concurrent", -1),
    ("ideal.max_concurrent", 2.5),
    ("ideal.max_concurrent", "5"),
    ("ideal.refresh_poll_max_attempts", 0),
    ("ideal.refresh_poll_max_attempts", -1),
    ("ideal.refresh_poll_max_attempts", 1.5),
    ("ideal.refresh_poll_delay_seconds", 0),
    ("ideal.refresh_poll_delay_seconds", -1),
    ("ideal.refresh_poll_delay_seconds", "1"),
    ("ideal.stripe_request_timeout_seconds", 0),
    ("ideal.stripe_request_timeout_seconds", -5),
    ("ideal.stripe_request_timeout_seconds", "30"),
    ("ideal.stripe_max_retry_attempts", 0),
    ("ideal.stripe_max_retry_attempts", -1),
    ("ideal.stripe_max_retry_attempts", 1.5),
    ("ideal.stripe_retry_backoff_seconds", 0),
    ("ideal.stripe_retry_backoff_seconds", -1),
    ("ideal.stripe_retry_backoff_seconds", "0.5"),
    ("ideal.device_profiles", "not-a-list"),
    ("ideal.device_profiles", [{"language": "nl-NL"}]),
]


@pytest.mark.parametrize("key,value", _IDEAL_KEY_VALID_CASES)
async def test_ideal_key_accepts_valid_value(tmp_path: Path, key: str, value: object) -> None:
    repo = await _make_repository_with_simulated_ideal_namespace(tmp_path)

    await repo.set(key, value)

    assert await repo.get(key) == value


@pytest.mark.parametrize("key,value", _IDEAL_KEY_INVALID_CASES)
async def test_ideal_key_rejects_invalid_value(tmp_path: Path, key: str, value: object) -> None:
    repo = await _make_repository_with_simulated_ideal_namespace(tmp_path)

    with pytest.raises(SettingsValidationError):
        await repo.set(key, value)

    assert await repo.get(key) is None


def test_ideal_key_table_covers_all_9_ideal_keys() -> None:
    """Đảm bảo bảng test case ở trên không bỏ sót key `ideal.*` nào trong whitelist."""
    expected_keys = {f"ideal.{field}" for field in _IDEAL_NAMESPACE_CONSTRAINTS}
    covered_keys = {key for key, _ in _IDEAL_KEY_VALID_CASES}
    assert covered_keys == expected_keys
    assert len(expected_keys) == 9


def test_whitelist_table_covers_all_15_keys_total() -> None:
    """Tổng cộng 6 key core + 9 key ideal.* = 15 key theo Requirement 11.7.

    (Trước đây có 7 key core gồm `web.auth_token`; sau khi bỏ Auth hoàn toàn,
    namespace `web` không còn key nào → core còn 6 key.)
    """
    core_keys = {key for key, _ in _CORE_KEY_VALID_CASES}
    ideal_keys = {key for key, _ in _IDEAL_KEY_VALID_CASES}
    assert len(core_keys) == 6
    assert len(ideal_keys) == 9
    assert len(core_keys | ideal_keys) == 15
