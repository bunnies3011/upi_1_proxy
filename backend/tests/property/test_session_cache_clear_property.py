"""Property test cho `core/session_cache.py::AccountSessionCache.clear` (task 7.6).

**Property 36: Clear cache 1 account là idempotent**
Dùng `hypothesis`, gọi `clear` N lần liên tiếp (N sinh ngẫu nhiên) trên
`account_key` có/không có bản ghi — không lần nào raise, và `get()` sau
lần clear đầu tiên luôn trả `None`.

**Validates: Requirements 10.8**
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from hypothesis import given, settings, strategies as st

from app.core.session_cache import AccountSessionCache


class _StubSettingsRepository:
    """Stub `SettingsRepository` — `AccountSessionCache.__init__` chỉ giữ
    reference, không gọi method nào của repo trong luồng hiện tại (hydrate
    đi qua `apply_settings(snapshot)` truyền từ test)."""


def _make_cache(cache_dir: Path) -> AccountSessionCache:
    cache = AccountSessionCache(
        settings=_StubSettingsRepository(),  # type: ignore[arg-type]
        cache_dir=cache_dir,
    )
    cache.apply_settings({"session_cache.enabled": True, "session_cache.ttl_hours": 24})
    return cache


@given(
    account_key=st.text(min_size=1, max_size=100),
    payload=st.dictionaries(
        keys=st.text(min_size=1, max_size=20),
        values=st.one_of(st.text(max_size=50), st.integers(), st.booleans()),
        max_size=5,
    ),
    repeat_count=st.integers(min_value=2, max_value=5),
)
@settings(max_examples=100)
def test_clear_is_idempotent_when_record_exists(
    account_key: str,
    payload: dict,
    repeat_count: int,
) -> None:
    """Clear khi CÓ bản ghi — gọi clear N lần liên tiếp, không lần nào raise."""

    async def _run() -> None:
        with tempfile.TemporaryDirectory() as cache_dir_str:
            cache = _make_cache(Path(cache_dir_str))

            # Tạo bản ghi thật trước khi clear — xác nhận có bản ghi thật.
            await cache.save(account_key, payload)
            assert (await cache.get(account_key)) is not None

            for _ in range(repeat_count):
                await cache.clear(account_key)  # không raise
                assert (await cache.get(account_key)) is None

    asyncio.run(_run())


@given(
    account_key=st.text(min_size=1, max_size=100),
    repeat_count=st.integers(min_value=2, max_value=5),
)
@settings(max_examples=100)
def test_clear_is_idempotent_when_record_absent(
    account_key: str,
    repeat_count: int,
) -> None:
    """Clear khi KHÔNG CÓ bản ghi — gọi clear N lần liên tiếp, không lần nào raise."""

    async def _run() -> None:
        with tempfile.TemporaryDirectory() as cache_dir_str:
            cache = _make_cache(Path(cache_dir_str))

            # account_key này chưa từng save() — chắc chắn không có bản ghi.
            assert (await cache.get(account_key)) is None

            for _ in range(repeat_count):
                await cache.clear(account_key)  # không raise
                assert (await cache.get(account_key)) is None

    asyncio.run(_run())
