"""Unit test cơ bản cho `core/proxy_pool.py` — `ProxyPool`.

Property test riêng (Property 26-30) thuộc các task 6.4-6.8, KHÔNG nằm ở
đây. File này chỉ verify các hành vi cơ bản theo Requirement 9.

Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.8.
"""

from __future__ import annotations

import pytest

from app.core.errors import ProxyExhaustedError
from app.core.proxy_pool import ProxyPool


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


async def test_acquire_returns_none_when_proxy_list_empty() -> None:
    """Direct_Mode hợp lệ khi `proxy.list` rỗng (Requirement 9.2)."""
    pool = _make_pool(proxy_list=[])

    lease = await pool.acquire(job_id="job-1")

    assert lease is None


async def test_acquire_release_round_trip_restores_lease_count() -> None:
    """acquire rồi release ngay lập tức khôi phục đúng trạng thái pool (Requirement 9.5)."""
    pool = _make_pool(proxy_list=["proxy-a:8080"], max_leases_per_proxy=2)

    lease = await pool.acquire(job_id="job-1")
    assert lease is not None
    assert lease.proxy_id == "proxy-a:8080"

    pool.release(lease)

    # Sau release, proxy phải khả dụng lại đủ max_leases_per_proxy lượt.
    lease_1 = await pool.acquire(job_id="job-2")
    lease_2 = await pool.acquire(job_id="job-3")
    assert lease_1 is not None
    assert lease_2 is not None


async def test_release_none_is_noop() -> None:
    """release(None) là no-op, không raise (Direct_Mode — Requirement 9.5)."""
    pool = _make_pool(proxy_list=["proxy-a:8080"])

    pool.release(None)  # không raise


async def test_round_robin_rotates_in_order() -> None:
    """round_robin xoay đúng thứ tự trong danh sách (Requirement 9.3)."""
    pool = _make_pool(
        proxy_list=["proxy-a", "proxy-b", "proxy-c"],
        rotation_mode="round_robin",
        max_leases_per_proxy=1,
    )

    lease_1 = await pool.acquire(job_id="job-1")
    lease_2 = await pool.acquire(job_id="job-2")
    lease_3 = await pool.acquire(job_id="job-3")

    assert [lease_1.proxy_id, lease_2.proxy_id, lease_3.proxy_id] == [
        "proxy-a",
        "proxy-b",
        "proxy-c",
    ]


async def test_least_used_picks_proxy_with_fewest_active_leases() -> None:
    """least_used chọn đúng proxy có số lease hiện tại thấp nhất (Requirement 9.3)."""
    pool = _make_pool(
        proxy_list=["proxy-a", "proxy-b", "proxy-c"],
        rotation_mode="least_used",
        max_leases_per_proxy=5,
    )

    # Mỗi proxy nhận đúng 1 lease (tie-break theo thứ tự trong list).
    lease_a = await pool.acquire(job_id="job-1")
    lease_b = await pool.acquire(job_id="job-2")
    lease_c = await pool.acquire(job_id="job-3")
    assert [lease_a.proxy_id, lease_b.proxy_id, lease_c.proxy_id] == [
        "proxy-a",
        "proxy-b",
        "proxy-c",
    ]

    # Trả lại lease của proxy-b — proxy-b giờ có ít lease active nhất (0 so với 1).
    pool.release(lease_b)

    next_lease = await pool.acquire(job_id="job-4")

    assert next_lease.proxy_id == "proxy-b"


async def test_mark_dead_after_threshold_removes_proxy_from_rotation() -> None:
    """mark_dead sau đủ threshold loại proxy khỏi vòng xoay (Requirement 9.6)."""
    pool = _make_pool(
        proxy_list=["proxy-a", "proxy-b"],
        rotation_mode="round_robin",
        max_leases_per_proxy=5,
        dead_threshold=3,
    )

    pool.mark_dead("proxy-a")
    pool.mark_dead("proxy-a")
    # Chưa đủ threshold — proxy-a vẫn khả dụng.
    lease = await pool.acquire(job_id="job-1")
    assert lease.proxy_id == "proxy-a"
    pool.release(lease)

    pool.mark_dead("proxy-a")  # lượt lỗi thứ 3 — đạt threshold, chuyển dead.

    lease_1 = await pool.acquire(job_id="job-2")
    lease_2 = await pool.acquire(job_id="job-3")
    # proxy-a bị loại khỏi vòng xoay — mọi lease tiếp theo đều rơi vào proxy-b.
    assert lease_1.proxy_id == "proxy-b"
    assert lease_2.proxy_id == "proxy-b"


async def test_mark_alive_restores_proxy_to_rotation() -> None:
    """mark_alive khôi phục proxy khả dụng trở lại (Requirement 9.6)."""
    pool = _make_pool(proxy_list=["proxy-a"], dead_threshold=1)

    pool.mark_dead("proxy-a")
    with pytest.raises(ProxyExhaustedError):
        await pool.acquire(job_id="job-1")

    pool.mark_alive("proxy-a")

    lease = await pool.acquire(job_id="job-2")
    assert lease is not None
    assert lease.proxy_id == "proxy-a"


async def test_acquire_raises_proxy_exhausted_when_all_dead() -> None:
    """ProxyExhaustedError raise đúng khi toàn bộ proxy đã dead (Requirement 9.4)."""
    pool = _make_pool(proxy_list=["proxy-a", "proxy-b"], dead_threshold=1)
    pool.mark_dead("proxy-a")
    pool.mark_dead("proxy-b")

    with pytest.raises(ProxyExhaustedError) as exc_info:
        await pool.acquire(job_id="job-1")

    error = exc_info.value
    assert error.total_proxies == 2
    assert error.dead_count == 2
    assert error.leased_out_count == 0


async def test_acquire_raises_proxy_exhausted_when_all_leased_out() -> None:
    """ProxyExhaustedError raise đúng khi toàn bộ proxy đã lease hết theo max_leases_per_proxy."""
    pool = _make_pool(proxy_list=["proxy-a"], max_leases_per_proxy=1)
    await pool.acquire(job_id="job-1")  # dùng hết slot duy nhất của proxy-a

    with pytest.raises(ProxyExhaustedError) as exc_info:
        await pool.acquire(job_id="job-2")

    error = exc_info.value
    assert error.total_proxies == 1
    assert error.dead_count == 0
    assert error.leased_out_count == 1


async def test_pool_returns_raw_line_consumer_materializes_sid() -> None:
    """Pool giữ RAW LINE (kể cả template `{SID}`); consumer gọi `materialize_proxy`.

    Contract mới (pattern gpt_signup_hybrid): pool là "dumb container",
    không hiểu format proxy — chỉ track rotation/lease/dead. Consumer
    (VD `payments/ideal/flow.py`) chịu trách nhiệm chuyển raw line thành
    URL httpx-ready qua `materialize_proxy(lease.materialized_url)` mỗi
    khi bắt đầu 1 job (SID gen ngẫu nhiên per-materialize call).

    Requirement 9.7 vẫn được thoả: mỗi lease tương ứng 1 job → consumer
    materialize 1 lần → 1 SID mới cho lease đó.
    """
    from app.core.proxy_format import materialize_proxy

    pool = _make_pool(
        proxy_list=["1.2.3.4:8080:user-{SID}:pass"], max_leases_per_proxy=2
    )

    lease_1 = await pool.acquire(job_id="job-1")
    lease_2 = await pool.acquire(job_id="job-2")

    # Pool giữ nguyên raw line, KHÔNG materialize sẵn.
    assert lease_1.materialized_url == "1.2.3.4:8080:user-{SID}:pass"
    assert lease_2.materialized_url == "1.2.3.4:8080:user-{SID}:pass"

    # Consumer gọi materialize_proxy → SID được thay, 2 lần khác nhau.
    url_1 = materialize_proxy(lease_1.materialized_url)
    url_2 = materialize_proxy(lease_2.materialized_url)
    assert "{sid}" not in url_1.lower()
    assert "{sid}" not in url_2.lower()
    assert url_1 != url_2  # SID gen ngẫu nhiên → hai URL khác nhau


async def test_apply_settings_preserves_state_for_existing_proxy() -> None:
    """apply_settings GIỮ nguyên trạng thái lease/dead nếu proxy vẫn còn trong list mới."""
    pool = _make_pool(proxy_list=["proxy-a", "proxy-b"], max_leases_per_proxy=2)
    lease = await pool.acquire(job_id="job-1")
    assert lease.proxy_id == "proxy-a"
    pool.mark_dead("proxy-b")
    pool.mark_dead("proxy-b")
    pool.mark_dead("proxy-b")  # dead_threshold=3 mặc định của _make_pool

    # Rebuild settings với cùng danh sách — trạng thái proxy-a/proxy-b phải giữ nguyên.
    pool.apply_settings(
        {
            "proxy.list": ["proxy-a", "proxy-b"],
            "proxy.rotation_mode": "round_robin",
            "proxy.max_leases_per_proxy": 2,
            "proxy.dead_threshold": 3,
        }
    )

    # proxy-a vẫn còn 1 lease active (round_robin bỏ qua proxy-b vì đã dead).
    next_lease = await pool.acquire(job_id="job-2")
    assert next_lease.proxy_id == "proxy-a"
