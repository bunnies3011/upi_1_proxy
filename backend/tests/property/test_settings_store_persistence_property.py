"""Property test cho `core/settings_store.py::SettingsRepository` (Property 40).

**Property 40: Settings Store persistence round-trip qua restart**
Dùng `hypothesis`, `set` toàn bộ rồi đóng/mở lại `SettingsRepository` trên
cùng file SQLite, so sánh `get`.

**Validates: Requirements 11.6**
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings, strategies as st

from app.core.db import DbEngine
from app.core.settings_store import SettingsRepository

# Tổ hợp giá trị hợp lệ cho toàn bộ 7 key core đã đăng ký sẵn trong
# `SettingsRepository.__init__` — mỗi key theo đúng `TypeConstraint` của nó.
_settings_values_strategy = st.fixed_dictionaries(
    {
        "proxy.list": st.lists(st.text(max_size=20), max_size=5),
        "proxy.rotation_mode": st.sampled_from(["round_robin", "least_used"]),
        "proxy.max_leases_per_proxy": st.integers(min_value=1, max_value=10_000),
        "proxy.dead_threshold": st.integers(min_value=1, max_value=10_000),
        "session_cache.enabled": st.booleans(),
        "session_cache.ttl_hours": st.integers(min_value=1, max_value=720),
    }
)


@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(values=_settings_values_strategy)
async def test_settings_persist_round_trip_across_restart(
    tmp_path: Path, values: dict[str, Any]
) -> None:
    """`bulk_set()` toàn bộ 7 key core rồi đóng engine, mở `DbEngine` MỚI
    trên CÙNG file SQLite → `get()`/`bulk_get()` ở lần mở lại trả đúng giá
    trị đã set ở lần trước, không mất/lệch dữ liệu (Requirement 11.6)."""
    db_path = tmp_path / "settings.db"

    engine_first = DbEngine(db_path)
    await engine_first.init_schema()
    repo_first = SettingsRepository(engine_first)
    await repo_first.bulk_set(values)
    await engine_first.close()

    engine_second = DbEngine(db_path)
    await engine_second.init_schema()
    repo_second = SettingsRepository(engine_second)

    read_back = await repo_second.bulk_get(list(values.keys()))
    assert read_back == values

    for key, expected_value in values.items():
        assert await repo_second.get(key) == expected_value

    await engine_second.close()
