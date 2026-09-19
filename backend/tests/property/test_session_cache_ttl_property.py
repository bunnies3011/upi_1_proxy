"""Property test cho `core/session_cache.py::AccountSessionCache` (Property 32).

**Property 32: AccountSessionCache round-trip theo TTL**
Dùng `hypothesis`, sinh `account_key`/payload/(T, T') quanh `session_cache.ttl_hours`:
- Khi tuổi bản ghi (tính từ `saved_at`) CHƯA vượt `ttl_hours` (age <= T), `get()`
  PHẢI trả đúng `CachedSession` với `payload` khớp payload đã `save()` (R10.2,
  R10.3, R10.5).
- Khi tuổi bản ghi ĐÃ vượt `ttl_hours` (age > T), `get()` PHẢI trả `None`
  (hết hạn, R10.3).

**Validates: Requirements 1.2, 10.2, 10.3, 10.5**
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

from hypothesis import given, settings, strategies as st

from app.core.session_cache import AccountSessionCache, CachedSession

_SECONDS_PER_HOUR = 3600

# `account_key` non-empty — tầng core hash lại account_key nên chấp nhận
# unicode/ký tự đặc biệt bất kỳ.
_account_key_strategy = st.text(min_size=1, max_size=50)

# payload dict đơn giản — vài field string/int, giống dữ liệu cookie/token
# thực tế mà payment module sẽ lưu (core/ không quan tâm ý nghĩa nghiệp vụ).
_payload_strategy = st.dictionaries(
    keys=st.text(
        alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), max_codepoint=122),
        min_size=1,
        max_size=15,
    ),
    values=st.one_of(st.text(max_size=30), st.integers()),
    min_size=1,
    max_size=5,
)

# ttl_hours = T ngẫu nhiên trong whitelist range [1, 720] (Requirement 11.7),
# thu hẹp về [1, 100] theo mô tả task để giữ test nhanh nhưng vẫn phủ nhiều
# giá trị T khác nhau.
_ttl_hours_strategy = st.integers(min_value=1, max_value=100)

# Base epoch cố định (giây) dùng làm `saved_at` giả lập — giá trị bất kỳ,
# không ảnh hưởng tới logic (chỉ cần offset chính xác theo age).
_BASE_EPOCH = 1_700_000_000.0


def _run(coro):
    """Chạy coroutine trong 1 event loop mới cho mỗi hypothesis example —
    cùng pattern với các property test async khác trong repo (ví dụ
    `test_proxy_pool_exhausted_property.py`, `test_session_cache_disabled_property.py`)."""
    return asyncio.run(coro)


def _make_cache(cache_dir: Path, *, ttl_hours: int) -> AccountSessionCache:
    cache = AccountSessionCache(settings=None, cache_dir=cache_dir)  # type: ignore[arg-type]
    cache.apply_settings(
        {"session_cache.enabled": True, "session_cache.ttl_hours": ttl_hours}
    )
    return cache


# ---------------------------------------------------------------------------
# Case 1: age (giây) trong TTL (0 <= age_seconds <= ttl_hours * 3600) —
# get() PHẢI trả đúng CachedSession với payload khớp.
# ---------------------------------------------------------------------------


@given(
    account_key=_account_key_strategy,
    payload=_payload_strategy,
    ttl_hours=_ttl_hours_strategy,
    age_seconds_within=st.data(),
)
@settings(max_examples=50)
def test_get_within_ttl_returns_matching_cached_session(
    account_key: str,
    payload: dict[str, Any],
    ttl_hours: int,
    age_seconds_within: st.DataObject,
) -> None:
    """`age_seconds` được sinh trong đúng khoảng `[0, ttl_hours * 3600]`
    (không vượt) — `get()` phải trả `CachedSession` với `payload` khớp
    payload đã `save()` và `account_key` khớp (R10.2, R10.3, R10.5)."""
    ttl_seconds = ttl_hours * _SECONDS_PER_HOUR
    age_seconds = age_seconds_within.draw(st.integers(min_value=0, max_value=ttl_seconds))

    async def _scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            cache = _make_cache(cache_dir, ttl_hours=ttl_hours)

            with patch("app.core.session_cache.time.time", return_value=_BASE_EPOCH):
                await cache.save(account_key, payload)

            with patch(
                "app.core.session_cache.time.time",
                return_value=_BASE_EPOCH + age_seconds,
            ):
                result = await cache.get(account_key)

            assert result is not None, (
                f"get() trả None trong khi age_seconds={age_seconds} <= "
                f"ttl_seconds={ttl_seconds} — bản ghi phải còn hợp lệ"
            )
            assert isinstance(result, CachedSession)
            assert result.account_key == account_key
            assert result.payload == payload
            assert result.saved_at == _BASE_EPOCH

    _run(_scenario())


# ---------------------------------------------------------------------------
# Case 2: age (giây) vượt TTL (ttl_hours * 3600 < age_seconds) — get() PHẢI
# trả None (hết hạn).
# ---------------------------------------------------------------------------


@given(
    account_key=_account_key_strategy,
    payload=_payload_strategy,
    ttl_hours=_ttl_hours_strategy,
    extra_seconds_over=st.data(),
)
@settings(max_examples=50)
def test_get_after_ttl_exceeded_returns_none(
    account_key: str,
    payload: dict[str, Any],
    ttl_hours: int,
    extra_seconds_over: st.DataObject,
) -> None:
    """`age_seconds` được sinh VƯỢT `ttl_hours * 3600` (strictly greater) —
    `get()` phải trả `None` vì bản ghi đã hết hạn (R10.3), và tự xoá file
    cache khỏi đĩa (self-heal expired)."""
    ttl_seconds = ttl_hours * _SECONDS_PER_HOUR
    # Đảm bảo age_seconds > ttl_seconds strictly — cộng thêm ít nhất 1 giây,
    # tối đa (T + 100h) tính theo giây.
    extra_seconds = extra_seconds_over.draw(
        st.integers(min_value=1, max_value=100 * _SECONDS_PER_HOUR)
    )
    age_seconds = ttl_seconds + extra_seconds

    async def _scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            cache = _make_cache(cache_dir, ttl_hours=ttl_hours)

            with patch("app.core.session_cache.time.time", return_value=_BASE_EPOCH):
                await cache.save(account_key, payload)

            with patch(
                "app.core.session_cache.time.time",
                return_value=_BASE_EPOCH + age_seconds,
            ):
                result = await cache.get(account_key)

            assert result is None, (
                f"get() phải trả None khi age_seconds={age_seconds} > "
                f"ttl_seconds={ttl_seconds} (đã hết hạn)"
            )

    _run(_scenario())
