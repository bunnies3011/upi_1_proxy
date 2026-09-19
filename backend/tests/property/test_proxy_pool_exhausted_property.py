"""Property test cho `core/proxy_pool.py::ProxyPool.acquire` (Property 28).

**Property 28: ProxyPool áp dụng Fail_Fast ngay khi hết proxy khả dụng**
Dùng `hypothesis`, sinh trạng thái pool toàn dead/toàn full, assert `acquire`
raise `ProxyExhaustedError` ngay lập tức.

**Validates: Requirements 9.4**
"""

from __future__ import annotations

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.errors import ProxyExhaustedError
from app.core.proxy_pool import ProxyPool

# Danh sách proxy 1-10 phần tử, mỗi phần tử duy nhất (dùng làm dict key nội
# bộ trong ProxyPool nên KHÔNG được trùng, tương tự cách ProxyPool tự dùng
# `proxy_id` làm key).
_proxy_list_strategy = st.lists(
    # Filter: mỗi phần tử phải đã strip (không có whitespace 2 đầu) VÀ
    # non-empty. ProxyPool.apply_settings tự strip + drop dòng rỗng, nên
    # `'\r'` / `'0\r'` / `'  '` sau strip có thể thành `''` hoặc `'0'`,
    # gây lệch identity giữa fixture strategy và raw line pool giữ. Filter
    # ở strategy để test property xoay quanh chính chuỗi đã stripped.
    st.text(min_size=1, max_size=30).filter(lambda s: s == s.strip() and s != ""),
    min_size=1,
    max_size=10,
    unique=True,
)


def _run(coro):
    """Chạy coroutine trong 1 event loop mới cho mỗi hypothesis example —
    cùng pattern với các property test async khác trong repo (ví dụ
    `test_auth_middleware_property.py`)."""
    return asyncio.run(coro)


def _make_pool(
    proxy_list: list[str],
    rotation_mode: str = "round_robin",
    max_leases_per_proxy: int = 1,
    dead_threshold: int = 3,
) -> ProxyPool:
    pool = ProxyPool(settings=None)  # type: ignore[arg-type] — apply_settings không tự query DB
    pool.apply_settings(
        {
            "proxy.list": proxy_list,
            "proxy.rotation_mode": rotation_mode,
            "proxy.max_leases_per_proxy": max_leases_per_proxy,
            "proxy.dead_threshold": dead_threshold,
        }
    )
    return pool


# -- Case 1: toàn bộ proxy đều dead --------------------------------------


@given(
    proxy_list=_proxy_list_strategy,
    dead_threshold=st.integers(min_value=1, max_value=5),
)
@settings(deadline=None, max_examples=30)
def test_acquire_raises_when_all_proxies_dead(
    proxy_list: list[str], dead_threshold: int
) -> None:
    """Toàn bộ proxy trong `proxy.list` bị `mark_dead` đủ `dead_threshold`
    lần → `acquire` raise `ProxyExhaustedError` ngay lập tức với
    `dead_count == total_proxies` và `leased_out_count == 0`."""

    async def _body() -> None:
        pool = _make_pool(
            proxy_list,
            max_leases_per_proxy=5,  # dư slot — chỉ dead mới khiến hết proxy
            dead_threshold=dead_threshold,
        )
        for proxy_id in proxy_list:
            for _ in range(dead_threshold):
                pool.mark_dead(proxy_id)

        try:
            await pool.acquire(job_id="job-exhausted-dead")
        except ProxyExhaustedError as error:
            assert error.total_proxies == len(proxy_list)
            assert error.dead_count == len(proxy_list)
            assert error.leased_out_count == 0
        else:
            raise AssertionError("acquire() phải raise ProxyExhaustedError khi toàn bộ proxy dead")

    _run(_body())


# -- Case 2: toàn bộ proxy đã lease hết (full lease) ----------------------


@given(
    proxy_list=_proxy_list_strategy,
    max_leases_per_proxy=st.integers(min_value=1, max_value=5),
)
@settings(deadline=None, max_examples=30)
def test_acquire_raises_when_all_proxies_fully_leased(
    proxy_list: list[str], max_leases_per_proxy: int
) -> None:
    """Acquire đủ `len(proxy_list) * max_leases_per_proxy` lần (không
    release) để lấp đầy toàn bộ slot → lần acquire kế tiếp raise
    `ProxyExhaustedError` với `leased_out_count == total_proxies` và
    `dead_count == 0`."""

    async def _body() -> None:
        pool = _make_pool(proxy_list, max_leases_per_proxy=max_leases_per_proxy)

        total_slots = len(proxy_list) * max_leases_per_proxy
        for i in range(total_slots):
            lease = await pool.acquire(job_id=f"job-fill-{i}")
            assert lease is not None  # còn slot trống — không được raise sớm

        try:
            await pool.acquire(job_id="job-exhausted-full")
        except ProxyExhaustedError as error:
            assert error.total_proxies == len(proxy_list)
            assert error.leased_out_count == len(proxy_list)
            assert error.dead_count == 0
        else:
            raise AssertionError(
                "acquire() phải raise ProxyExhaustedError khi toàn bộ proxy đã full lease"
            )

    _run(_body())


# -- Case 3: mix — 1 phần dead, phần còn lại full lease -------------------


@given(
    proxy_list=_proxy_list_strategy,
    dead_threshold=st.integers(min_value=1, max_value=5),
    max_leases_per_proxy=st.integers(min_value=1, max_value=5),
    dead_group_size=st.data(),
)
@settings(deadline=None, max_examples=30)
def test_acquire_raises_when_mixed_dead_and_fully_leased(
    proxy_list: list[str],
    dead_threshold: int,
    max_leases_per_proxy: int,
    dead_group_size: st.DataObject,
) -> None:
    """Chia `proxy_list` thành 2 nhóm ngẫu nhiên: nhóm dead (`mark_dead` đủ
    threshold) và nhóm full lease (acquire đủ `max_leases_per_proxy` lần,
    không release). Khi TOÀN BỘ proxy thuộc 1 trong 2 nhóm trên (không còn
    proxy khả dụng nào), `acquire` phải raise `ProxyExhaustedError` với
    `dead_count + leased_out_count == total_proxies`."""
    total = len(proxy_list)
    # Kích thước nhóm dead ngẫu nhiên trong [0, total] — nhóm còn lại
    # (total - k) sẽ được full-lease. k=0 → toàn full lease, k=total → toàn
    # dead (2 case biên trùng case 1/2 ở trên, vẫn hợp lệ để cover thêm).
    dead_group_count = dead_group_size.draw(st.integers(min_value=0, max_value=total))
    dead_group = proxy_list[:dead_group_count]
    leased_group = proxy_list[dead_group_count:]

    async def _body() -> None:
        pool = _make_pool(
            proxy_list,
            max_leases_per_proxy=max_leases_per_proxy,
            dead_threshold=dead_threshold,
        )

        for proxy_id in dead_group:
            for _ in range(dead_threshold):
                pool.mark_dead(proxy_id)

        # Lấp đầy toàn bộ slot của nhóm còn lại (không release) — proxy dead
        # bị round-robin/least-used bỏ qua nên chỉ nhóm leased_group nhận lease.
        job_counter = 0
        for _ in leased_group:
            for _ in range(max_leases_per_proxy):
                lease = await pool.acquire(job_id=f"job-mix-{job_counter}")
                assert lease is not None
                assert lease.proxy_id in leased_group
                job_counter += 1

        try:
            await pool.acquire(job_id="job-exhausted-mix")
        except ProxyExhaustedError as error:
            assert error.total_proxies == total
            assert error.dead_count == len(dead_group)
            assert error.leased_out_count == len(leased_group)
            assert error.dead_count + error.leased_out_count == total
        else:
            raise AssertionError(
                "acquire() phải raise ProxyExhaustedError khi mọi proxy đều dead hoặc full lease"
            )

    _run(_body())
