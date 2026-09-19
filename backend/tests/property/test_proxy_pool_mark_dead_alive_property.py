"""Property test cho `core/proxy_pool.py::ProxyPool.mark_dead/mark_alive` (Property 30).

**Property 30: mark_dead/mark_alive round-trip loại và khôi phục proxy khỏi vòng xoay**
Dùng `hypothesis`, sinh số lỗi liên tiếp N ngẫu nhiên quanh `dead_threshold`.

**Validates: Requirements 9.6**
"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from app.core.proxy_pool import ProxyPool

_PROXY_ID = "http://proxy.example.test:8080"


@given(
    dead_threshold=st.integers(min_value=1, max_value=10),
    n=st.integers(min_value=0, max_value=20),
)
@settings(max_examples=50)
async def test_mark_dead_crosses_threshold_exactly_at_n(dead_threshold: int, n: int) -> None:
    """Sau `N` lần gọi `mark_dead`, proxy `dead=True` khi và chỉ khi
    `N >= dead_threshold`. Khi `N < dead_threshold`, proxy vẫn khả dụng —
    `acquire` phải thành công và trả đúng `proxy_id` này (proxy duy nhất
    trong pool, chưa đạt `max_leases_per_proxy`)."""
    pool = ProxyPool(settings=None)
    pool.apply_settings(
        {
            "proxy.list": [_PROXY_ID],
            "proxy.max_leases_per_proxy": 10,
            "proxy.dead_threshold": dead_threshold,
            "proxy.rotation_mode": "round_robin",
        }
    )

    for _ in range(n):
        pool.mark_dead(_PROXY_ID)

    state = pool._proxies[_PROXY_ID]
    expected_dead = n >= dead_threshold
    assert state.dead is expected_dead

    if not expected_dead:
        lease = await pool.acquire("job-1")
        assert lease is not None
        assert lease.proxy_id == _PROXY_ID


def _dead_threshold_and_n_before() -> st.SearchStrategy[tuple[int, int]]:
    """Sinh cặp `(dead_threshold, n_before)` với `n_before >= dead_threshold`
    (giữa đúng threshold tới gấp đôi), đảm bảo proxy chắc chắn đã dead
    trước khi test round-trip `mark_alive`."""
    return st.integers(min_value=1, max_value=10).flatmap(
        lambda threshold: st.tuples(
            st.just(threshold),
            st.integers(min_value=threshold, max_value=threshold * 2),
        )
    )


@given(
    threshold_and_n_before=_dead_threshold_and_n_before(),
    n_after=st.integers(min_value=1, max_value=10),
)
@settings(max_examples=50)
async def test_mark_alive_resets_state_and_new_mark_dead_cycle_is_independent(
    threshold_and_n_before: tuple[int, int], n_after: int
) -> None:
    """Round-trip: sau khi proxy đã dead (`n_before >= dead_threshold`),
    `mark_alive` phải reset hoàn toàn (`dead=False` VÀ `consecutive_errors
    == 0`), và `acquire` hoạt động lại bình thường. Chu kỳ `mark_dead` mới
    (`n_after` lần, độc lập với lịch sử cũ) phải chỉ khiến proxy dead trở
    lại đúng khi `n_after >= dead_threshold` — không bị ảnh hưởng bởi
    `consecutive_errors` trước lúc `mark_alive`."""
    dead_threshold, n_before = threshold_and_n_before
    pool = ProxyPool(settings=None)
    pool.apply_settings(
        {
            "proxy.list": [_PROXY_ID],
            "proxy.max_leases_per_proxy": 10,
            "proxy.dead_threshold": dead_threshold,
            "proxy.rotation_mode": "round_robin",
        }
    )

    for _ in range(n_before):
        pool.mark_dead(_PROXY_ID)
    assert pool._proxies[_PROXY_ID].dead is True

    pool.mark_alive(_PROXY_ID)
    state = pool._proxies[_PROXY_ID]
    assert state.dead is False
    assert state.consecutive_errors == 0

    lease = await pool.acquire("job-after-alive")
    assert lease is not None
    assert lease.proxy_id == _PROXY_ID
    pool.release(lease)

    for _ in range(n_after):
        pool.mark_dead(_PROXY_ID)

    state = pool._proxies[_PROXY_ID]
    expected_dead_after = n_after >= dead_threshold
    assert state.dead is expected_dead_after
