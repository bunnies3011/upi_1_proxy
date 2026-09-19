"""Property test cho `core/settings_store.py::SettingsRepository` (Property 39).

**Property 39: Settings Store từ chối key ngoài whitelist**
Dùng `hypothesis`, sinh chuỗi key ngẫu nhiên không thuộc whitelist.

**Validates: Requirements 11.4**
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from app.core.db import DbEngine
from app.core.errors import SettingsValidationError
from app.core.settings_store import SettingsRepository

# 7 key core đã được `SettingsRepository.__init__` tự đăng ký whitelist.
_WHITELISTED_KEYS = frozenset(
    {
        "proxy.list",
        "proxy.rotation_mode",
        "proxy.max_leases_per_proxy",
        "proxy.dead_threshold",
        "session_cache.enabled",
        "session_cache.ttl_hours",
        "web.auth_token",
    }
)

# 3 namespace đã đăng ký (prefix hợp lệ) nhưng field cụ thể KHÔNG thuộc
# whitelist — dùng để đảm bảo validate whitelist theo TỪNG field, không chỉ
# theo namespace prefix.
_REGISTERED_NAMESPACES = ("proxy", "session_cache", "web")


def _is_outside_whitelist(key: str) -> bool:
    """Đảm bảo tuyệt đối `key` không match bất kỳ key nào trong whitelist.

    So sánh chuỗi trực tiếp (không strip/normalize) — chỉ loại đúng 7 giá
    trị whitelist, giữ lại mọi dạng "gần giống" (khác hoa/thường, thừa/thiếu
    khoảng trắng, sai dấu chấm, unicode...) vì các dạng đó THỰC SỰ không
    thuộc whitelist và phải bị `SettingsRepository` từ chối.
    """
    return key not in _WHITELISTED_KEYS


def _is_unknown_field_in_registered_namespace(key: str) -> bool:
    """`key` thuộc 1 trong 3 namespace ĐÃ đăng ký nhưng field cụ thể không
    khớp bất kỳ field nào trong whitelist của namespace đó."""
    if key in _WHITELISTED_KEYS:
        return False
    namespace, sep, field = key.partition(".")
    return bool(sep) and bool(field) and namespace in _REGISTERED_NAMESPACES


# Key ngẫu nhiên bất kỳ (không giới hạn namespace), loại trừ 7 key whitelist.
_non_whitelisted_key_strategy = st.text().filter(_is_outside_whitelist)

# Key có namespace ĐÃ đăng ký (`proxy.`, `session_cache.`, `web.`) nhưng field
# ngẫu nhiên không thuộc whitelist của namespace đó (ví dụ `proxy.unknown_field`).
_unknown_field_in_registered_namespace_strategy = st.builds(
    lambda namespace, field: f"{namespace}.{field}",
    namespace=st.sampled_from(_REGISTERED_NAMESPACES),
    field=st.text(min_size=1).filter(lambda s: "." not in s),
).filter(_is_unknown_field_in_registered_namespace)

_arbitrary_value_strategy = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(),
    lambda children: st.lists(children, max_size=3)
    | st.dictionaries(st.text(), children, max_size=3),
    max_leaves=5,
)


async def _make_repository(tmp_path: Path) -> SettingsRepository:
    engine = DbEngine(tmp_path / "ideal_qr_tool.db")
    await engine.init_schema()
    return SettingsRepository(engine)


@settings(deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(key=_non_whitelisted_key_strategy, value=_arbitrary_value_strategy)
async def test_set_key_outside_whitelist_always_raises(
    tmp_path: Path, key: str, value: object
) -> None:
    """`set()` với key ngoài whitelist (bất kỳ chuỗi ngẫu nhiên nào không
    khớp đúng 7 key core đã đăng ký) LUÔN raise `SettingsValidationError`,
    bất kể `value` thuộc kiểu gì (Requirement 11.4)."""
    repo = await _make_repository(tmp_path)

    with pytest.raises(SettingsValidationError) as exc_info:
        await repo.set(key, value)

    assert exc_info.value.key == key


@settings(deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(key=_non_whitelisted_key_strategy)
async def test_get_key_outside_whitelist_always_raises(tmp_path: Path, key: str) -> None:
    """`get()` với key ngoài whitelist cũng LUÔN raise `SettingsValidationError`
    (khớp hành vi thực tế của `_resolve_constraint`, được gọi ở cả `get()` và
    `set()`, Requirement 11.4) — key ngoài whitelist không có khái niệm
    "chưa từng ghi nên trả None", mà bị từ chối truy cập ngay từ bước
    resolve constraint."""
    repo = await _make_repository(tmp_path)

    with pytest.raises(SettingsValidationError) as exc_info:
        await repo.get(key)

    assert exc_info.value.key == key


@settings(deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(key=_unknown_field_in_registered_namespace_strategy, value=_arbitrary_value_strategy)
async def test_set_unknown_field_in_registered_namespace_always_raises(
    tmp_path: Path, key: str, value: object
) -> None:
    """`set()` với key thuộc namespace ĐÃ đăng ký (`proxy.`, `session_cache.`,
    `web.`) nhưng field cụ thể KHÔNG có trong whitelist (ví dụ
    `proxy.unknown_field`, `web.some_random_field`) LUÔN raise
    `SettingsValidationError` — đảm bảo whitelist được validate theo TỪNG
    field, không chỉ theo namespace prefix (Requirement 11.4)."""
    repo = await _make_repository(tmp_path)

    with pytest.raises(SettingsValidationError) as exc_info:
        await repo.set(key, value)

    assert exc_info.value.key == key
