"""Unit test cho `core/settings_store.py` — `SettingsRepository`.

Requirements: 9.1, 10.1, 11.1, 11.2, 11.3, 11.4, 11.6, 11.7.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.db import DbEngine
from app.core.errors import NamespaceAlreadyRegisteredError, SettingsValidationError
from app.core.settings_store import SettingsRepository, TypeConstraint


async def _make_repository(tmp_path: Path) -> SettingsRepository:
    engine = DbEngine(tmp_path / "ideal_qr_tool.db")
    await engine.init_schema()
    return SettingsRepository(engine)


async def test_set_get_round_trip_int(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)

    await repo.set("proxy.max_leases_per_proxy", 3)

    assert await repo.get("proxy.max_leases_per_proxy") == 3


async def test_set_get_round_trip_bool(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)

    await repo.set("session_cache.enabled", False)

    assert await repo.get("session_cache.enabled") is False


async def test_set_get_round_trip_string(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)

    await repo.set("proxy.rotation_mode", "least_used")

    assert await repo.get("proxy.rotation_mode") == "least_used"


async def test_set_get_round_trip_list_str(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)

    await repo.set("proxy.list", ["1.2.3.4:8080", "5.6.7.8:8080"])

    assert await repo.get("proxy.list") == ["1.2.3.4:8080", "5.6.7.8:8080"]


async def test_get_unset_key_returns_none(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)

    assert await repo.get("ui.input_draft") is None


async def test_set_wrong_type_raises_and_keeps_old_value(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)
    await repo.set("proxy.max_leases_per_proxy", 2)

    with pytest.raises(SettingsValidationError):
        await repo.set("proxy.max_leases_per_proxy", "not-an-int")

    assert await repo.get("proxy.max_leases_per_proxy") == 2


async def test_set_out_of_range_raises_and_keeps_old_value(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)
    await repo.set("session_cache.ttl_hours", 24)

    with pytest.raises(SettingsValidationError):
        await repo.set("session_cache.ttl_hours", 1000)

    assert await repo.get("session_cache.ttl_hours") == 24


async def test_set_invalid_enum_raises_and_keeps_old_value(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)
    await repo.set("proxy.rotation_mode", "round_robin")

    with pytest.raises(SettingsValidationError):
        await repo.set("proxy.rotation_mode", "random_mode")

    assert await repo.get("proxy.rotation_mode") == "round_robin"


async def test_set_key_outside_whitelist_raises(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)

    with pytest.raises(SettingsValidationError):
        await repo.set("unknown_namespace.some_field", 1)


async def test_get_key_outside_whitelist_raises(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)

    with pytest.raises(SettingsValidationError):
        await repo.get("unknown_namespace.some_field")


async def test_bulk_set_atomic_when_one_item_invalid(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)
    await repo.set("proxy.dead_threshold", 3)

    with pytest.raises(SettingsValidationError):
        await repo.bulk_set(
            {
                "proxy.dead_threshold": 5,
                "proxy.rotation_mode": "invalid_mode",
            }
        )

    # Không item nào được ghi — cả 2 key giữ nguyên giá trị trước bulk_set.
    assert await repo.get("proxy.dead_threshold") == 3
    assert await repo.get("proxy.rotation_mode") is None


async def test_bulk_set_and_bulk_get(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)

    await repo.bulk_set({"proxy.dead_threshold": 4, "session_cache.ttl_hours": 48})

    result = await repo.bulk_get(["proxy.dead_threshold", "session_cache.ttl_hours"])
    assert result == {"proxy.dead_threshold": 4, "session_cache.ttl_hours": 48}


async def test_list_filters_by_prefix(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)
    await repo.set("proxy.dead_threshold", 2)
    await repo.set("ui.input_draft", "secret-token")

    proxy_only = await repo.list(prefix="proxy")

    assert proxy_only == {"proxy.dead_threshold": 2}


async def test_register_namespace_twice_raises(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)

    with pytest.raises(NamespaceAlreadyRegisteredError):
        repo.register_namespace("proxy", {"list": TypeConstraint(type="list_str")})


async def test_register_namespace_allows_new_module_to_extend_registry(tmp_path: Path) -> None:
    repo = await _make_repository(tmp_path)

    repo.register_namespace(
        "ideal", {"max_concurrent": TypeConstraint(type="int", min=1, max=50)}
    )
    await repo.set("ideal.max_concurrent", 5)

    assert await repo.get("ideal.max_concurrent") == 5
