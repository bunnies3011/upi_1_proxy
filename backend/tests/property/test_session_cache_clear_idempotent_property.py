"""Property test cho `core/session_cache.py::AccountSessionCache.clear` (task 7.6).

**Property 36: Clear cache 1 account là idempotent**
Dùng `hypothesis`, gọi `clear` 2 lần liên tiếp trên `account_key` có/không có
bản ghi.

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
    has_existing_record=st.booleans(),
)
@settings(max_examples=100)
def test_clear_is_idempotent_for_account_with_or_without_record(
    account_key: str,
    has_existing_record: bool,
) -> None:
    async def _run() -> None:
        with tempfile.TemporaryDirectory() as cache_dir_str:
            cache = _make_cache(Path(cache_dir_str))

            if has_existing_record:
                await cache.save(account_key, {"foo": "bar"})
                # Sanity check: bản ghi thật sự tồn tại trước khi clear.
                assert (await cache.get(account_key)) is not None

            # Lần clear 1 — không raise, get() phải trả None sau đó dù
            # account_key có hay không có bản ghi trước đó.
            await cache.clear(account_key)
            assert (await cache.get(account_key)) is None

            # Lần clear 2 — account_key giờ chắc chắn không có bản ghi
            # (idempotent: không raise, get() vẫn trả None).
            await cache.clear(account_key)
            assert (await cache.get(account_key)) is None

    asyncio.run(_run())
