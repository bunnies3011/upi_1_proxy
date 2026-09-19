"""Property test cho `core/session_cache.py::AccountSessionCache` (Property 34).

**Property 34: `session_cache.enabled=false` khiến cache luôn bị bỏ qua**
Dùng `hypothesis`, sinh `account_key`/`payload` ngẫu nhiên khi `enabled=false`:
- `save()` phải no-op ngay (không tạo file nào trong `cache_dir`).
- `get()` luôn trả `None`, bất kể đã có bản ghi cache hợp lệ tồn tại sẵn
  trên đĩa (được ghi TRƯỚC KHI chuyển sang disabled) hay chưa.

**Validates: Requirements 10.6**
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from hypothesis import given, settings, strategies as st

from app.core.session_cache import AccountSessionCache

_account_key_strategy = st.text(min_size=1, max_size=50)
_payload_value_strategy = st.one_of(
    st.text(max_size=30),
    st.integers(),
    st.booleans(),
    st.none(),
)
_payload_strategy = st.dictionaries(
    keys=st.text(min_size=1, max_size=20),
    values=_payload_value_strategy,
    max_size=5,
)
# "bất kỳ" — ttl_hours không được validate ở `apply_settings` (validate
# thuộc `SettingsRepository`, không thuộc `AccountSessionCache`), nên sinh
# range rộng (kể cả giá trị âm/0) để chứng minh `enabled=False` luôn thắng
# bất kể `ttl_hours` là gì.
_ttl_hours_strategy = st.integers(min_value=-1000, max_value=100_000)


def _run(coro):
    """Chạy coroutine trong 1 event loop mới — pattern chuẩn của repo khi
    kết hợp `hypothesis` (sync `@given`) với code async."""
    return asyncio.run(coro)


def _make_cache(cache_dir: Path, *, enabled: bool, ttl_hours: int) -> AccountSessionCache:
    cache = AccountSessionCache(settings=None, cache_dir=cache_dir)  # type: ignore[arg-type]
    cache.apply_settings(
        {"session_cache.enabled": enabled, "session_cache.ttl_hours": ttl_hours}
    )
    return cache


@given(
    account_key=_account_key_strategy,
    payload=_payload_strategy,
    ttl_hours=_ttl_hours_strategy,
)
@settings(max_examples=50)
def test_disabled_save_never_creates_file(
    account_key: str, payload: dict[str, Any], ttl_hours: int
) -> None:
    """`session_cache.enabled=False` => `save()` KHÔNG tạo bất kỳ file nào
    trong `cache_dir` (no-op ngay, không chạm filesystem — R10.6)."""

    async def _scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            cache = _make_cache(cache_dir, enabled=False, ttl_hours=ttl_hours)

            await cache.save(account_key, payload)

            assert list(cache_dir.glob("*.json")) == []

    _run(_scenario())


@given(
    account_key=_account_key_strategy,
    payload=_payload_strategy,
    ttl_hours=_ttl_hours_strategy,
)
@settings(max_examples=50)
def test_disabled_get_always_none_without_prior_cache(
    account_key: str, payload: dict[str, Any], ttl_hours: int
) -> None:
    """`session_cache.enabled=False` => `get()` luôn trả `None` khi chưa
    từng có bản ghi cache nào được lưu (R10.6)."""

    async def _scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            cache = _make_cache(cache_dir, enabled=False, ttl_hours=ttl_hours)

            result = await cache.get(account_key)

            assert result is None

    _run(_scenario())


@given(
    account_key=_account_key_strategy,
    payload=_payload_strategy,
    ttl_hours=_ttl_hours_strategy,
)
@settings(max_examples=50)
def test_disabled_get_always_none_even_with_existing_cache_file_on_disk(
    account_key: str, payload: dict[str, Any], ttl_hours: int
) -> None:
    """Đã `save()` 1 bản ghi hợp lệ khi CÒN enabled (file cache thật sự tồn
    tại trên đĩa), sau đó chuyển `session_cache.enabled=False` — `get()`
    vẫn PHẢI trả `None`, dù file cache vẫn còn nguyên trên đĩa (R10.6:
    disabled phải chặn đọc ngay từ đầu, không đọc file cũ)."""

    async def _scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)

            # Bước 1: lưu 1 bản ghi hợp lệ lúc còn enabled=True.
            cache = _make_cache(cache_dir, enabled=True, ttl_hours=max(ttl_hours, 1))
            await cache.save(account_key, payload)
            cached_files_before = list(cache_dir.glob("*.json"))
            assert len(cached_files_before) == 1

            # Bước 2: chuyển sang disabled — get() KHÔNG được đọc file cũ.
            cache.apply_settings(
                {"session_cache.enabled": False, "session_cache.ttl_hours": ttl_hours}
            )
            result = await cache.get(account_key)

            assert result is None
            # File cache thực tế vẫn còn tồn tại trên đĩa (get() không xoá
            # nó — chỉ đơn giản bỏ qua vì disabled).
            assert list(cache_dir.glob("*.json")) == cached_files_before

    _run(_scenario())
