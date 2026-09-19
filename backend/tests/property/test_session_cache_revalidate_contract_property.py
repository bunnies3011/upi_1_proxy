"""Property test cho CONTRACT revalidate giữa `core/session_cache.py` và caller
(Property 33).

**Property 33: Revalidate thất bại luôn loại bỏ bản ghi cache**

`AccountSessionCache` (`core/`) KHÔNG tự implement logic "revalidate" —
gọi `GET /api/auth/session` là trách nhiệm của
`payments/ideal/chatgpt_client.py` (chưa implement, thuộc task 15.1 sau).
Theo design.md: logic revalidate cụ thể HTTP nằm ở `payments/ideal/`,
không ở `core/` (giữ boundary: `core/` chỉ biết TTL + lưu/đọc file,
không biết cách revalidate của từng payment method).

Test này verify đúng phần thuộc `core/`: khi caller (giả lập payment
module) xác định revalidate thất bại và gọi `cache.clear(account_key)`,
bản ghi cache PHẢI bị loại bỏ hoàn toàn (`get()` trả `None` sau đó).
Hàm `_resolve_session_with_revalidate` bên dưới mô phỏng flow mà
`IdealFlowHandler`/`ChatgptClient` sẽ làm ở task sau — KHÔNG test logic
HTTP revalidate thật (chưa tồn tại).

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
    # ttl_hours lớn để không hết hạn trong lúc test chạy.
    cache.apply_settings(
        {"session_cache.enabled": True, "session_cache.ttl_hours": 24 * 365}
    )
    return cache


def _run(coro):
    """Chạy coroutine trong 1 event loop mới cho mỗi hypothesis example —
    cùng pattern với các property test khác của session_cache."""
    return asyncio.run(coro)


async def _resolve_session_with_revalidate(
    cache: AccountSessionCache, account_key: str, revalidate_result: bool
) -> CachedSession | None:
    """Mô phỏng flow "resolve_session" mà `IdealFlowHandler`/`ChatgptClient`
    sẽ làm ở task sau: đọc cache, nếu hit thì revalidate — revalidate thất
    bại (`revalidate_result=False`) thì loại bỏ bản ghi (`clear`) và trả
    `None`; revalidate thành công thì giữ nguyên bản ghi và trả về nó.

    Đây KHÔNG phải logic HTTP thật (chưa tồn tại) — chỉ là contract giữa
    `core/session_cache.py` và caller mà property test này verify.
    """
    cached = await cache.get(account_key)
    if cached is None:
        return None
    if not revalidate_result:
        await cache.clear(account_key)
        return None
    return cached


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
    """save → `_resolve_session_with_revalidate(revalidate_result)`.

    - `revalidate_result=False` (revalidate thất bại): kết quả trả `None`,
      VÀ `cache.get(account_key)` sau đó PHẢI trả `None` (bản ghi đã bị
      loại bỏ hoàn toàn khỏi cache — Property 33 cốt lõi).
    - `revalidate_result=True` (revalidate thành công): kết quả trả về
      đúng `CachedSession` với `payload` khớp, VÀ bản ghi vẫn còn trong
      cache (`get()` sau đó vẫn trả về đúng, không bị xoá).
    """

    async def _body() -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache = _make_cache(Path(tmp_dir))

            await cache.save(account_key, payload)
            assert (await cache.get(account_key)) is not None

            result = await _resolve_session_with_revalidate(
                cache, account_key, revalidate_result
            )

            if not revalidate_result:
                assert result is None
                after = await cache.get(account_key)
                assert after is None
            else:
                assert isinstance(result, CachedSession)
                assert result.payload == payload
                assert result.account_key == account_key
                after = await cache.get(account_key)
                assert after is not None
                assert after.payload == payload
                assert after.account_key == account_key

    _run(_body())
