"""Property test cho `core/proxy_pool.py::ProxyPool.release` (Property 29).

**Property 29: Release lease khôi phục đúng trạng thái pool trước khi acquire**
Dùng `hypothesis`, `acquire` rồi `release` ngay, assert số lease active khôi
phục về giá trị trước khi acquire.

**Validates: Requirements 9.5**

`lease_count` là internal state (private) — property này xác minh gián tiếp
qua hành vi observable: lấp đầy toàn bộ pool (mọi proxy đạt
`proxy.max_leases_per_proxy`), `release` đúng 1 lease đã cấp, rồi assert
`acquire` ngay sau đó PHẢI thành công và trả về đúng `proxy_id` của lease vừa
release (proxy duy nhất còn slot trống), và `acquire` thêm 1 lần nữa PHẢI lại
raise `ProxyExhaustedError` (xác nhận state đã khôi phục đúng về "full").
"""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given, settings, strategies as st

from app.core.errors import ProxyExhaustedError
from app.core.proxy_pool import ProxyPool


def _make_pool(
    proxy_list: list[str],
    rotation_mode: str,
    max_leases_per_proxy: int,
) -> ProxyPool:
    pool = ProxyPool(settings=None)  # type: ignore[arg-type] — apply_settings không tự query DB
    pool.apply_settings(
        {
            "proxy.list": proxy_list,
            "proxy.rotation_mode": rotation_mode,
            "proxy.max_leases_per_proxy": max_leases_per_proxy,
            "proxy.dead_threshold": 3,
        }
    )
    return pool


@st.composite
def _full_pool_params(draw: st.DrawFn) -> tuple[list[str], str, int, int]:
    """Sinh (proxy_list, rotation_mode, max_leases_per_proxy, release_index).

    `release_index` được sinh RÀNG BUỘC vào `total = len(proxy_list) *
    max_leases_per_proxy` (0-indexed trong danh sách lease đã cấp khi lấp
    đầy toàn bộ pool), nên phải dùng `st.composite` để draw phụ thuộc.
    """
    proxy_count = draw(st.integers(min_value=1, max_value=5))
    proxy_list = [f"proxy-{i}" for i in range(proxy_count)]
    rotation_mode = draw(st.sampled_from(["round_robin", "least_used"]))
    max_leases_per_proxy = draw(st.integers(min_value=1, max_value=5))
    total = proxy_count * max_leases_per_proxy
    release_index = draw(st.integers(min_value=0, max_value=total - 1))
    return proxy_list, rotation_mode, max_leases_per_proxy, release_index


@given(params=_full_pool_params())
@settings(max_examples=50)
def test_release_restores_full_pool_to_exact_pre_acquire_state(
    params: tuple[list[str], str, int, int],
) -> None:
    proxy_list, rotation_mode, max_leases_per_proxy, release_index = params

    async def _run() -> None:
        pool = _make_pool(proxy_list, rotation_mode, max_leases_per_proxy)
        total = len(proxy_list) * max_leases_per_proxy

        # Lấp đầy toàn bộ pool — acquire đúng `total` lần, lưu lại mọi lease.
        leases = []
        for i in range(total):
            lease = await pool.acquire(job_id=f"job-fill-{i}")
            assert lease is not None
            leases.append(lease)

        # Pool đã full — acquire thêm phải raise ngay (baseline trước khi release).
        with pytest.raises(ProxyExhaustedError):
            await pool.acquire(job_id="job-verify-full-before-release")

        released_lease = leases[release_index]
        pool.release(released_lease)

        # Slot vừa giải phóng phải khả dụng lại — acquire PHẢI thành công và
        # trả về đúng proxy_id của lease vừa release (proxy duy nhất có slot
        # trống, vì mọi proxy khác vẫn đang ở max_leases_per_proxy).
        new_lease = await pool.acquire(job_id="job-after-release")
        assert new_lease is not None
        assert new_lease.proxy_id == released_lease.proxy_id

        # Sau khi slot vừa giải phóng bị dùng lại, pool trở về đúng trạng
        # thái "full" như trước khi release — acquire thêm phải raise lại.
        with pytest.raises(ProxyExhaustedError):
            await pool.acquire(job_id="job-verify-full-after-reacquire")

    asyncio.run(_run())
