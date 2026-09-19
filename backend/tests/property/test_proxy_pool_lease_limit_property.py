"""Property test cho `core/proxy_pool.py::ProxyPool` (Property 27).

**Property 27: ProxyPool tôn trọng giới hạn lease đồng thời trên mỗi proxy**
Dùng `hypothesis`, sinh danh sách proxy + `max_leases_per_proxy` ngẫu nhiên,
kiểm tra bất biến `<= L` và thứ tự cyclic round-robin.

**Validates: Requirements 9.3**
"""

from __future__ import annotations

import asyncio

from hypothesis import given, settings, strategies as st

from app.core.errors import ProxyExhaustedError
from app.core.proxy_pool import ProxyPool

# `dead_threshold` không liên quan tới property này (không có proxy nào bị
# `mark_dead` trong toàn bộ test) — dùng 1 giá trị mặc định hợp lệ bất kỳ.
_DEAD_THRESHOLD_DEFAULT = 3

# Tên proxy ASCII đơn giản (giống format raw line thực tế `host:port`),
# `unique=True` để đảm bảo mỗi proxy_id trong `proxy.list` phân biệt được
# rõ ràng khi đếm lease theo từng proxy.
_proxy_list_strategy = st.lists(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789.:_-", min_size=1, max_size=16),
    min_size=2,
    max_size=10,
    unique=True,
)
_max_leases_strategy = st.integers(min_value=1, max_value=5)


def _run(coro):
    """Chạy coroutine trong 1 event loop mới cho mỗi hypothesis example —
    cùng pattern với các property test async khác trong repo (`ProxyPool.acquire`
    là async)."""
    return asyncio.run(coro)


def _make_pool(proxy_list: list[str], rotation_mode: str, max_leases: int) -> ProxyPool:
    """Tạo `ProxyPool` đã `apply_settings` với danh sách proxy cố định.

    `ProxyPool.__init__` chỉ giữ tham chiếu `settings` để dùng ở nơi khác
    (không đọc DB tại đây) — property này không cần `SettingsRepository`
    thật, truyền `None` là đủ vì không method nào trong test dùng tới nó.
    """
    pool = ProxyPool(settings=None)  # type: ignore[arg-type]
    pool.apply_settings(
        {
            "proxy.list": proxy_list,
            "proxy.rotation_mode": rotation_mode,
            "proxy.max_leases_per_proxy": max_leases,
            "proxy.dead_threshold": _DEAD_THRESHOLD_DEFAULT,
        }
    )
    return pool


@given(
    proxy_list=_proxy_list_strategy,
    max_leases=_max_leases_strategy,
    rotation_mode=st.sampled_from(["round_robin", "least_used"]),
)
@settings(max_examples=50, deadline=None)
def test_lease_count_per_proxy_never_exceeds_limit(
    proxy_list: list[str], max_leases: int, rotation_mode: str
) -> None:
    """Gọi `acquire` liên tục (không `release`) cho tới khi hết proxy khả
    dụng: tại MỌI thời điểm, số lease đã cấp cho mỗi `proxy_id` KHÔNG BAO GIỜ
    vượt quá `L`. Khi hết dung lượng (`N * L` lease), `acquire` kế tiếp phải
    raise `ProxyExhaustedError` ngay lập tức (Requirement 9.3, 9.4)."""

    async def _body() -> None:
        pool = _make_pool(proxy_list, rotation_mode, max_leases)

        lease_counts: dict[str, int] = {proxy_id: 0 for proxy_id in proxy_list}
        total_capacity = len(proxy_list) * max_leases

        for _ in range(total_capacity):
            lease = await pool.acquire("job-1")
            assert lease is not None
            lease_counts[lease.proxy_id] += 1
            # Bất biến cốt lõi: không proxy nào vượt quá L tại bất kỳ thời điểm.
            assert lease_counts[lease.proxy_id] <= max_leases

        # Toàn bộ dung lượng N * L phải được cấp hết, chia đều đúng L cho mỗi proxy.
        assert sum(lease_counts.values()) == total_capacity
        for proxy_id in proxy_list:
            assert lease_counts[proxy_id] == max_leases

        # Hết dung lượng → acquire kế tiếp raise ProxyExhaustedError ngay lập tức.
        try:
            await pool.acquire("job-1")
        except ProxyExhaustedError:
            pass
        else:
            raise AssertionError(
                "Expected ProxyExhaustedError khi pool đã cấp hết N * L lease"
            )

    _run(_body())


@given(proxy_list=_proxy_list_strategy, max_leases=_max_leases_strategy)
@settings(max_examples=50, deadline=None)
def test_round_robin_visits_all_proxies_cyclically_before_repeating(
    proxy_list: list[str], max_leases: int
) -> None:
    """Với `rotation_mode="round_robin"` và không có proxy nào `dead`: mỗi
    vòng đủ N proxy phải xoay hết N proxy theo ĐÚNG thứ tự trong `proxy.list`
    trước khi lặp lại vòng kế tiếp — tức là toàn bộ chuỗi `acquire` liên tiếp
    (cho tới khi hết dung lượng) bằng đúng `proxy_list` lặp lại `max_leases`
    lần (Requirement 9.3)."""

    async def _body() -> None:
        pool = _make_pool(proxy_list, "round_robin", max_leases)

        total_capacity = len(proxy_list) * max_leases
        observed_order: list[str] = []
        for _ in range(total_capacity):
            lease = await pool.acquire("job-1")
            assert lease is not None
            observed_order.append(lease.proxy_id)

        expected_order = proxy_list * max_leases
        assert observed_order == expected_order

    _run(_body())
