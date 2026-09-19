"""Property test cho `core/proxy_pool.py::ProxyPool.acquire` (Property 26).

**Property 26: ProxyPool vận hành Direct_Mode hợp lệ khi danh sách rỗng**
Dùng `hypothesis`, gọi `acquire` liên tiếp N lần khi `proxy.list` rỗng, assert
luôn trả `None`, không raise (đặc biệt không raise `ProxyExhaustedError`).

**Validates: Requirements 9.2**
"""

from __future__ import annotations

import asyncio
import uuid

from hypothesis import given, settings, strategies as st

from app.core.proxy_pool import ProxyPool

# `rotation_mode`/`max_leases_per_proxy`/`dead_threshold` KHÔNG ảnh hưởng
# Direct_Mode — Direct_Mode chỉ phụ thuộc duy nhất vào `proxy.list` rỗng.
# Sinh ngẫu nhiên các giá trị này để đảm bảo property đúng bất kể setting
# khác thế nào.
_rotation_mode_strategy = st.one_of(
    st.none(),
    st.sampled_from(["round_robin", "least_used", "bogus_mode", ""]),
)
_max_leases_strategy = st.one_of(st.none(), st.integers(min_value=0, max_value=100))
_dead_threshold_strategy = st.one_of(st.none(), st.integers(min_value=0, max_value=100))
_job_id_strategy = st.one_of(
    st.uuids().map(str),
    st.text(min_size=1, max_size=32),
)


def _run(coro):
    """Chạy coroutine trong 1 event loop mới — pattern chuẩn khi kết hợp
    `hypothesis` (sync `@given`) với code async, giống các property test
    async khác trong repo."""
    return asyncio.run(coro)


@given(
    n=st.integers(min_value=1, max_value=50),
    rotation_mode=_rotation_mode_strategy,
    max_leases_per_proxy=_max_leases_strategy,
    dead_threshold=_dead_threshold_strategy,
    job_ids=st.lists(_job_id_strategy, min_size=1, max_size=50),
)
@settings(max_examples=25)
def test_acquire_always_returns_none_when_proxy_list_empty(
    n: int,
    rotation_mode: str | None,
    max_leases_per_proxy: int | None,
    dead_threshold: int | None,
    job_ids: list[str],
) -> None:
    """`proxy.list` rỗng => Direct_Mode: `acquire` gọi N lần liên tiếp luôn
    trả `None`, không raise exception nào (đặc biệt không raise
    `ProxyExhaustedError`), bất kể `rotation_mode`/`max_leases_per_proxy`/
    `dead_threshold` được cấu hình thế nào."""

    async def _scenario() -> None:
        pool = ProxyPool(settings=None)
        pool.apply_settings(
            {
                "proxy.list": [],
                "proxy.rotation_mode": rotation_mode,
                "proxy.max_leases_per_proxy": max_leases_per_proxy,
                "proxy.dead_threshold": dead_threshold,
            }
        )

        for i in range(n):
            job_id = job_ids[i % len(job_ids)]
            lease = await pool.acquire(job_id)
            assert lease is None

    _run(_scenario())


@given(job_id=st.uuids().map(str))
@settings(max_examples=10)
def test_acquire_returns_none_without_apply_settings_called(job_id: str) -> None:
    """Trước cả khi `apply_settings` được gọi lần đầu, `ProxyPool` khởi tạo
    ở trạng thái an toàn mặc định (pool rỗng => Direct_Mode) — `acquire`
    vẫn phải trả `None`, không raise."""

    async def _scenario() -> None:
        pool = ProxyPool(settings=None)
        lease = await pool.acquire(job_id)
        assert lease is None

    _run(_scenario())


@given(n=st.integers(min_value=1, max_value=20))
@settings(max_examples=10)
def test_acquire_returns_none_repeatedly_with_random_job_id_each_call(n: int) -> None:
    """Mỗi lần gọi `acquire` với `job_id` ngẫu nhiên khác nhau (uuid mới mỗi
    lần) — vẫn luôn trả `None` khi Direct_Mode, không raise."""

    async def _scenario() -> None:
        pool = ProxyPool(settings=None)
        pool.apply_settings({"proxy.list": []})

        for _ in range(n):
            job_id = uuid.uuid4().hex
            lease = await pool.acquire(job_id)
            assert lease is None

    _run(_scenario())
