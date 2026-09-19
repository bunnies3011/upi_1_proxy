"""Property test cho `core/session_cache.py::AccountSessionCache.clear_all` (task 7.7).

**Property 37: Clear all xoá toàn bộ bản ghi cache**
Dùng `hypothesis`, sinh tập `account_key` đã từng `save`, gọi `clear_all()`
rồi assert KHÔNG còn bản ghi nào sót lại (mọi `get()` trả `None`) và
`cache_dir` không còn file `.json` nào.

**Validates: Requirements 10.9**
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


# dict keys là account_key (unique, non-empty by construction), values là
# payload đơn giản ngẫu nhiên tương ứng cho từng account_key.
_account_payloads_strategy = st.dictionaries(
    keys=st.text(min_size=1, max_size=50),
    values=st.dictionaries(
        keys=st.text(min_size=1, max_size=20),
        values=st.one_of(st.text(max_size=50), st.integers(), st.booleans()),
        max_size=5,
    ),
    min_size=1,
    max_size=10,
)


@given(account_payloads=_account_payloads_strategy)
@settings(max_examples=100)
def test_clear_all_removes_every_saved_record(account_payloads: dict) -> None:
    """clear_all() xoá TOÀN BỘ bản ghi đã save, không sót lại account_key nào."""

    async def _run() -> None:
        with tempfile.TemporaryDirectory() as cache_dir_str:
            cache_dir = Path(cache_dir_str)
            cache = _make_cache(cache_dir)

            for account_key, payload in account_payloads.items():
                await cache.save(account_key, payload)

            # Sanity check: mọi account_key vừa save phải hit trước khi clear_all.
            for account_key in account_payloads:
                assert (await cache.get(account_key)) is not None

            await cache.clear_all()  # không raise

            # Không còn bản ghi nào sót lại cho TOÀN BỘ account_key trong tập.
            for account_key in account_payloads:
                assert (await cache.get(account_key)) is None

            # Không còn file .json nào trong cache_dir.
            assert list(cache_dir.glob("*.json")) == []

    asyncio.run(_run())


def test_clear_all_on_empty_cache_dir_is_idempotent() -> None:
    """clear_all() trên cache_dir rỗng (chưa từng save gì) — không raise."""

    async def _run() -> None:
        with tempfile.TemporaryDirectory() as cache_dir_str:
            cache_dir = Path(cache_dir_str)
            cache = _make_cache(cache_dir)

            await cache.clear_all()  # không raise, idempotent
            await cache.clear_all()  # gọi lại lần 2 vẫn không raise

            assert list(cache_dir.glob("*.json")) == []

    asyncio.run(_run())

