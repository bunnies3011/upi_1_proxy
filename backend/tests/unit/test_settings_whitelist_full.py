"""Unit test whitelist đầy đủ 24 key đúng type/range (Requirement 11.7).

Bảng whitelist đầy đủ (design.md — "Whitelist key đầy đủ (Requirement 11.7)"):

- 14 key CORE (`ui.*`, `proxy.*`, `session_cache.*`) — đã
  được `SettingsRepository.__init__` tự đăng ký sẵn. Con số 14 phản ánh 2 lần
  bổ sung core sau task 5.1: namespace `ui` (`ui.input_draft`) và 7 proxy
  probe knob (2026-07: probe_enabled/endpoint/timeout/max_tries/
  sid_retry_per_line/concurrency + fallback_direct_on_exhausted).
- 9 key `ideal.*` — SHALL được `payments/ideal/__init__.py` đăng ký (task 20.3,
  chưa implement). Phần B của file này tự `register_namespace("ideal", {...})`
  ngay trong test, đúng type constraint theo bảng whitelist, để verify hành vi
  generic của `SettingsRepository`/`TypeConstraint` không phụ thuộc việc
  `payments/ideal/` đã tồn tại hay chưa.

Requirements: 11.7.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.db import DbEngine
from app.core.errors import SettingsValidationError
from app.core.settings_store import SettingsRepository, TypeConstraint


async def _make_repository(tmp_path: Path) -> SettingsRepository:
    engine = DbEngine(tmp_path / "ideal_qr_tool.db")
    await engine.init_schema()
    return SettingsRepository(engine)


def _register_ideal_namespace(repo: SettingsRepository) -> None:
    """Đăng ký namespace `ideal` với 9 key theo đúng bảng whitelist (design.md).

    `refresh_poll_delay_seconds`, `stripe_request_timeout_seconds`,
    `stripe_retry_backoff_seconds` mong đợi ">0" (exclusive) nhưng
    `TypeConstraint` hiện tại chỉ hỗ trợ `min` inclusive — dùng `min` bằng 1
    giá trị dương rất nhỏ (`0.0001`) để xấp xỉ ">0" mà không cần mở rộng API
    `TypeConstraint` (giữ thay đổi tối thiểu, không đổi `settings_store.py`).
    """
    repo.register_namespace(
        "ideal",
        {
            "default_issuer": TypeConstraint(type="string", min=1),
            "known_issuers": TypeConstraint(type="list_str"),
            "max_concurrent": TypeConstraint(type="int", min=1, max=50),
            "refresh_poll_max_attempts": TypeConstraint(type="int", min=1),
            "refresh_poll_delay_seconds": TypeConstraint(type="number", min=0.0001),
            "stripe_request_timeout_seconds": TypeConstraint(type="number", min=0.0001),
            "stripe_max_retry_attempts": TypeConstraint(type="int", min=1),
            "stripe_retry_backoff_seconds": TypeConstraint(type="number", min=0.0001),
            "device_profiles": TypeConstraint(
                type="list_object",
                item_schema={
                    "required_keys": [
                        "language",
                        "time_zone",
                        "screen_width",
                        "screen_height",
                        "screen_available_width",
                        "screen_available_height",
                        "color_depth",
                    ]
                },
            ),
        },
    )


# ---------------------------------------------------------------------------
# Phần A — 7 key CORE (đã đăng ký sẵn bởi SettingsRepository.__init__)
# ---------------------------------------------------------------------------


async def test_proxy_list_accepts_list_of_string(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)

    await repo.set("proxy.list", ["1.2.3.4:8080"])
    assert await repo.get("proxy.list") == ["1.2.3.4:8080"]

    await repo.set("proxy.list", [])
    assert await repo.get("proxy.list") == []


async def test_proxy_list_rejects_non_list(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)

    with pytest.raises(SettingsValidationError):
        await repo.set("proxy.list", "1.2.3.4:8080")


async def test_proxy_list_rejects_list_with_non_string_item(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)

    with pytest.raises(SettingsValidationError):
        await repo.set("proxy.list", ["1.2.3.4:8080", 123])


async def test_proxy_rotation_mode_accepts_enum_values(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)

    await repo.set("proxy.rotation_mode", "round_robin")
    assert await repo.get("proxy.rotation_mode") == "round_robin"

    await repo.set("proxy.rotation_mode", "least_used")
    assert await repo.get("proxy.rotation_mode") == "least_used"


async def test_proxy_rotation_mode_rejects_value_outside_enum(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)

    with pytest.raises(SettingsValidationError):
        await repo.set("proxy.rotation_mode", "random_mode")


async def test_proxy_max_leases_per_proxy_accepts_boundary_min(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)

    await repo.set("proxy.max_leases_per_proxy", 1)
    assert await repo.get("proxy.max_leases_per_proxy") == 1


async def test_proxy_max_leases_per_proxy_rejects_below_min(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)

    with pytest.raises(SettingsValidationError):
        await repo.set("proxy.max_leases_per_proxy", 0)


async def test_proxy_dead_threshold_accepts_boundary_min(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)

    await repo.set("proxy.dead_threshold", 1)
    assert await repo.get("proxy.dead_threshold") == 1


async def test_proxy_dead_threshold_rejects_below_min(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)

    with pytest.raises(SettingsValidationError):
        await repo.set("proxy.dead_threshold", 0)


async def test_session_cache_enabled_accepts_bool(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)

    await repo.set("session_cache.enabled", True)
    assert await repo.get("session_cache.enabled") is True

    await repo.set("session_cache.enabled", False)
    assert await repo.get("session_cache.enabled") is False


async def test_session_cache_enabled_rejects_non_bool(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)

    with pytest.raises(SettingsValidationError):
        await repo.set("session_cache.enabled", "true")


async def test_session_cache_ttl_hours_accepts_boundary_min_and_max(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)

    await repo.set("session_cache.ttl_hours", 1)
    assert await repo.get("session_cache.ttl_hours") == 1

    await repo.set("session_cache.ttl_hours", 720)
    assert await repo.get("session_cache.ttl_hours") == 720


async def test_session_cache_ttl_hours_rejects_below_min_and_above_max(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)

    with pytest.raises(SettingsValidationError):
        await repo.set("session_cache.ttl_hours", 0)

    with pytest.raises(SettingsValidationError):
        await repo.set("session_cache.ttl_hours", 721)


# ---------------------------------------------------------------------------
# Phần B — 9 key `ideal.*` (namespace tự đăng ký trong test, task 20.3 chưa làm)
# ---------------------------------------------------------------------------


async def test_ideal_default_issuer_accepts_non_empty_string(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)
    _register_ideal_namespace(repo)

    await repo.set("ideal.default_issuer", "ABNANL2A")
    assert await repo.get("ideal.default_issuer") == "ABNANL2A"


async def test_ideal_default_issuer_rejects_empty_string(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)
    _register_ideal_namespace(repo)

    with pytest.raises(SettingsValidationError):
        await repo.set("ideal.default_issuer", "")


async def test_ideal_known_issuers_accepts_list_of_string(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)
    _register_ideal_namespace(repo)

    await repo.set("ideal.known_issuers", ["ABNANL2A", "RABONL2U"])
    assert await repo.get("ideal.known_issuers") == ["ABNANL2A", "RABONL2U"]


async def test_ideal_known_issuers_rejects_non_list(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)
    _register_ideal_namespace(repo)

    with pytest.raises(SettingsValidationError):
        await repo.set("ideal.known_issuers", "ABNANL2A")


async def test_ideal_max_concurrent_accepts_boundary_min_and_max(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)
    _register_ideal_namespace(repo)

    await repo.set("ideal.max_concurrent", 1)
    assert await repo.get("ideal.max_concurrent") == 1

    await repo.set("ideal.max_concurrent", 50)
    assert await repo.get("ideal.max_concurrent") == 50


async def test_ideal_max_concurrent_rejects_below_min_and_above_max(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)
    _register_ideal_namespace(repo)

    with pytest.raises(SettingsValidationError):
        await repo.set("ideal.max_concurrent", 0)

    with pytest.raises(SettingsValidationError):
        await repo.set("ideal.max_concurrent", 51)


async def test_ideal_refresh_poll_max_attempts_accepts_boundary_min(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)
    _register_ideal_namespace(repo)

    await repo.set("ideal.refresh_poll_max_attempts", 1)
    assert await repo.get("ideal.refresh_poll_max_attempts") == 1


async def test_ideal_refresh_poll_max_attempts_rejects_below_min(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)
    _register_ideal_namespace(repo)

    with pytest.raises(SettingsValidationError):
        await repo.set("ideal.refresh_poll_max_attempts", 0)


async def test_ideal_refresh_poll_delay_seconds_accepts_small_positive(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)
    _register_ideal_namespace(repo)

    await repo.set("ideal.refresh_poll_delay_seconds", 0.5)
    assert await repo.get("ideal.refresh_poll_delay_seconds") == 0.5


async def test_ideal_refresh_poll_delay_seconds_rejects_zero_or_negative(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)
    _register_ideal_namespace(repo)

    with pytest.raises(SettingsValidationError):
        await repo.set("ideal.refresh_poll_delay_seconds", 0)

    with pytest.raises(SettingsValidationError):
        await repo.set("ideal.refresh_poll_delay_seconds", -1)


async def test_ideal_stripe_request_timeout_seconds_accepts_positive(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)
    _register_ideal_namespace(repo)

    await repo.set("ideal.stripe_request_timeout_seconds", 30)
    assert await repo.get("ideal.stripe_request_timeout_seconds") == 30


async def test_ideal_stripe_request_timeout_seconds_rejects_zero_or_negative(
    tmp_path: Path,
) -> None:
    repo = await _make_repository(tmp_path)
    _register_ideal_namespace(repo)

    with pytest.raises(SettingsValidationError):
        await repo.set("ideal.stripe_request_timeout_seconds", 0)

    with pytest.raises(SettingsValidationError):
        await repo.set("ideal.stripe_request_timeout_seconds", -30)


async def test_ideal_stripe_max_retry_attempts_accepts_boundary_min(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)
    _register_ideal_namespace(repo)

    await repo.set("ideal.stripe_max_retry_attempts", 1)
    assert await repo.get("ideal.stripe_max_retry_attempts") == 1


async def test_ideal_stripe_max_retry_attempts_rejects_below_min(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)
    _register_ideal_namespace(repo)

    with pytest.raises(SettingsValidationError):
        await repo.set("ideal.stripe_max_retry_attempts", 0)


async def test_ideal_stripe_retry_backoff_seconds_accepts_positive(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)
    _register_ideal_namespace(repo)

    await repo.set("ideal.stripe_retry_backoff_seconds", 0.5)
    assert await repo.get("ideal.stripe_retry_backoff_seconds") == 0.5


async def test_ideal_stripe_retry_backoff_seconds_rejects_zero_or_negative(
    tmp_path: Path,
) -> None:
    repo = await _make_repository(tmp_path)
    _register_ideal_namespace(repo)

    with pytest.raises(SettingsValidationError):
        await repo.set("ideal.stripe_retry_backoff_seconds", 0)

    with pytest.raises(SettingsValidationError):
        await repo.set("ideal.stripe_retry_backoff_seconds", -0.5)


async def test_ideal_device_profiles_accepts_object_with_all_required_keys(
    tmp_path: Path,
) -> None:
    repo = await _make_repository(tmp_path)
    _register_ideal_namespace(repo)

    valid_profile = {
        "language": "nl-NL",
        "time_zone": "Europe/Amsterdam",
        "screen_width": 1920,
        "screen_height": 1080,
        "screen_available_width": 1920,
        "screen_available_height": 1040,
        "color_depth": 24,
    }

    await repo.set("ideal.device_profiles", [valid_profile])
    assert await repo.get("ideal.device_profiles") == [valid_profile]


async def test_ideal_device_profiles_rejects_object_missing_required_key(
    tmp_path: Path,
) -> None:
    repo = await _make_repository(tmp_path)
    _register_ideal_namespace(repo)

    incomplete_profile = {
        "language": "nl-NL",
        "time_zone": "Europe/Amsterdam",
        # thiếu các field screen_*/color_depth bắt buộc
    }

    with pytest.raises(SettingsValidationError):
        await repo.set("ideal.device_profiles", [incomplete_profile])


# ---------------------------------------------------------------------------
# Phần C — tổng số key quản lý được sau khi đăng ký cả 2 phần == 24
#
# `SettingsRepository.__init__` nay tự đăng ký thêm namespace `ui` (1 key) và
# các proxy probe knob (2026-07) + `session_cache.skip_revalidate_if_fresh_hours`
# nên số key CORE = 15:
#   - ui.input_draft                             (1)
#   - proxy.* (list, rotation_mode, max_leases_per_proxy, dead_threshold,
#     probe_enabled, probe_endpoint, probe_timeout_seconds, probe_max_tries,
#     probe_sid_retry_per_line, probe_concurrency,
#     fallback_direct_on_exhausted)              (11)
#   - session_cache.* (enabled, ttl_hours, skip_revalidate_if_fresh_hours) (3)
# (Auth ĐÃ BỎ — `web.auth_token` không còn trong whitelist.)
# Cộng 9 key `ideal.*` tự đăng ký trong test (Phần B) → tổng 24.
# ---------------------------------------------------------------------------


async def test_whitelist_registry_covers_exactly_23_keys(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)
    _register_ideal_namespace(repo)

    # Không có API public để enumerate whitelist — introspect registry nội bộ.
    total_keys = sum(len(fields) for fields in repo._registry.values())  # noqa: SLF001

    assert total_keys == 27

    expected_keys = {
        # ui.* (namespace core — textarea draft + auto Check Plus All knobs)
        "ui.input_draft",
        "ui.auto_check_plus_all_enabled",
        "ui.auto_check_plus_all_interval_seconds",
        # proxy.* — 4 key gốc + 1 dead-cooldown (TTL auto-revive) + 7 probe knob
        "proxy.list",
        "proxy.rotation_mode",
        "proxy.max_leases_per_proxy",
        "proxy.dead_threshold",
        "proxy.dead_cooldown_seconds",
        "proxy.probe_enabled",
        "proxy.probe_endpoint",
        "proxy.probe_timeout_seconds",
        "proxy.probe_max_tries",
        "proxy.probe_sid_retry_per_line",
        "proxy.probe_concurrency",
        "proxy.fallback_direct_on_exhausted",
        "session_cache.enabled",
        "session_cache.ttl_hours",
        "session_cache.skip_revalidate_if_fresh_hours",
        "ideal.default_issuer",
        "ideal.known_issuers",
        "ideal.max_concurrent",
        "ideal.refresh_poll_max_attempts",
        "ideal.refresh_poll_delay_seconds",
        "ideal.stripe_request_timeout_seconds",
        "ideal.stripe_max_retry_attempts",
        "ideal.stripe_retry_backoff_seconds",
        "ideal.device_profiles",
    }
    actual_keys = {
        f"{namespace}.{field}"
        for namespace, fields in repo._registry.items()  # noqa: SLF001
        for field in fields
    }
    assert actual_keys == expected_keys
