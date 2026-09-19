"""Property test cho pattern revalidate của `core/session_cache.py` (Property 33).

**Property 33: Revalidate thất bại luôn loại bỏ bản ghi cache**
Logic revalidate HTTP cụ thể (`GET /api/auth/session`) thuộc
`payments/ideal/chatgpt_client.py` (chưa implement — task sau). Tầng
`core/session_cache.py` chỉ cung cấp `get()`/`save()`/`clear()` generic,
KHÔNG có method `revalidate` nào. Test này verify đúng CONTRACT mà
`core/` cung cấp để pattern "revalidate thất bại → loại bỏ cache" hoạt
động đúng ở tầng gọi (caller pattern): giả lập kết quả `revalidate` bằng
mock callback (KHÔNG phải HTTP thật) trả `True`/`False` ngẫu nhiên — nếu
`False`, caller gọi `clear(account_key)` và bản ghi phải bị loại bỏ (`get`
kế tiếp trả `None`); nếu `True`, cache KHÔNG bị đụng và `get` vẫn trả đúng
bản ghi ban đầu.

**Validates: Requirements 10.4**
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.session_cache import AccountSessionCache, CachedSession


class _StubSettingsRepository:
    """Stub `SettingsRepository` — `AccountSessionCache.__init__` chỉ giữ
    reference, không gọi method nào của repo trong luồng hiện tại."""


def _make_cache(cache_dir: Path) -> AccountSessionCache:
    cache = AccountSessionCache(
        settings=_StubSettingsRepository(),  # type: ignore[arg-type]
        cache_dir=cache_dir,
    )
    cache.apply_settings({"session_cache.enabled": True, "session_cache.ttl_hours": 24})
    return cache


def _run(coro):
    """Chạy coroutine trong 1 event loop mới cho mỗi hypothesis example —
    cùng pattern với `test_proxy_pool_exhausted_property.py`."""
    return asyncio.run(coro)


# `payload` dict ngẫu nhiên — key text non-empty, value là kiểu JSON-serializable
# đơn giản (giống dữ liệu cookie/access_token thực tế mà caller sẽ lưu).
_payload_strategy = st.dictionaries(
    keys=st.text(min_size=1, max_size=20),
    values=st.one_of(st.text(max_size=50), st.integers(), st.booleans()),
    max_size=5,
)


@given(
    account_key=st.text(min_size=1, max_size=50),
    payload=_payload_strategy,
    revalidate_result=st.booleans(),
)
@settings(deadline=None, max_examples=50)
def test_revalidate_failure_always_evicts_cache_record(
    account_key: str, payload: dict, revalidate_result: bool
) -> None:
    """save → get (hit) → giả lập revalidate mock trả `revalidate_result`.

    - `revalidate_result=False` → caller gọi `clear(account_key)` → `get`
      kế tiếp PHẢI trả `None` (bản ghi đã bị loại bỏ).
    - `revalidate_result=True` → cache KHÔNG bị đụng → `get` kế tiếp PHẢI
      vẫn trả đúng `CachedSession` với `payload` như đã save.
    """

    async def _body() -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache = _make_cache(Path(tmp_dir))

            await cache.save(account_key, payload)

            first_hit = await cache.get(account_key)
            assert first_hit is not None
            assert isinstance(first_hit, CachedSession)
            assert first_hit.payload == payload

            # Mock revalidate callback — KHÔNG gọi HTTP thật, chỉ trả giá
            # trị hypothesis sinh ra để giả lập kết quả revalidate.
            def _mock_revalidate() -> bool:
                return revalidate_result

            if not _mock_revalidate():
                await cache.clear(account_key)
                after_fail = await cache.get(account_key)
                assert after_fail is None
            else:
                # KHÔNG gọi clear — cache phải còn nguyên như trước.
                after_success = await cache.get(account_key)
                assert after_success is not None
                assert after_success.payload == payload
                assert after_success.account_key == account_key

    _run(_body())
