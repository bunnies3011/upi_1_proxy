"""Property test cho `core/job_manager.py::JobManager.stop` (Property 25).

**Property 25: Stop job luôn chuyển stopped và giải phóng lease nếu có**

Sinh `IdealJob` ngẫu nhiên ở `pending`/`running`, có/không có `ProxyLease`.
Gọi `JobManager.stop(job_id)` → assert:
- Job kết thúc ở trạng thái `STOPPED` (trong bounded time, ≤500ms cho case
  running).
- Nếu job có lease → lease được release về `ProxyPool`
  (`FakeProxyPool.released` chứa đúng `proxy_id`).

**Validates: Requirements 8.6**

Reuse `FakeHandler`, `FakeSettings`, `FakeProxyPool`, `FakeSse` từ
`backend/tests/unit/test_job_manager.py` — cùng bộ test double đã dùng cho
unit test của JobManager, tránh double-implementation của stub. Import qua
package `unit.test_job_manager` vì `tests/` KHÔNG có `__init__.py` (được
pytest thêm vào `sys.path`) và `tests/unit/` là package (có `__init__.py`).

Hypothesis: dùng profile `fast` (`max_examples=20`) đã đăng ký ở
`backend/tests/conftest.py` — override explicit qua `@settings` để tài liệu
rõ ràng, đồng thời `deadline=None` vì test có bước `await asyncio.sleep`
polling tới 1s (RUNNING) + 0.5s (STOPPED) vượt deadline mặc định 200ms của
hypothesis.
"""

from __future__ import annotations

import asyncio

from hypothesis import given, settings, strategies as st

from app.core.job_manager import JobManager
from app.core.payment_flow import JobStatus

from unit.test_job_manager import FakeHandler, FakeProxyPool, FakeSettings, FakeSse


async def _wait_until(predicate, timeout: float) -> bool:
    """Poll `predicate` mỗi 5ms cho tới True hoặc timeout. Trả về giá trị
    của `predicate()` tại thời điểm kết thúc (True nếu thành công trong
    thời gian giới hạn, False nếu hết giờ mà điều kiện vẫn chưa thoả)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


async def _cancel_manager_tasks(manager: JobManager) -> None:
    """Cleanup toàn bộ task nền của JobManager sau mỗi hypothesis example.

    `asyncio.run()` sẽ tự cancel task pending khi loop đóng, nhưng làm sạch
    tay tránh cảnh báo "Task was destroyed but pending" và tránh side-effect
    lan sang example kế (`hypothesis` có thể chạy nhiều example trong cùng
    process). Truy cập attribute private là chấp nhận được ở tầng test —
    JobManager không expose API shutdown công khai."""
    scheduler = manager._scheduler_task  # noqa: SLF001
    if scheduler is not None and not scheduler.done():
        scheduler.cancel()
        try:
            await scheduler
        except BaseException:
            pass
    for record in manager.list_jobs():
        task = record.handler_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass


@given(
    proxy_mode=st.sampled_from(["direct", "lease"]),
    stop_when=st.sampled_from(["pending", "running"]),
)
@settings(max_examples=20, deadline=None)
def test_stop_always_transitions_stopped_and_releases_lease(
    proxy_mode: str, stop_when: str
) -> None:
    async def _run() -> None:
        settings_stub = FakeSettings()
        proxy_pool = FakeProxyPool()
        proxy_pool.mode = proxy_mode
        sse = FakeSse()
        manager = JobManager(
            settings=settings_stub,  # type: ignore[arg-type]
            proxy_pool=proxy_pool,  # type: ignore[arg-type]
            sse=sse,  # type: ignore[arg-type]
        )
        # `run_delay` đủ lâu để job không tự kết thúc trước khi test kịp
        # `stop`. `respect_cancel=True` để handler tôn trọng cancellation
        # token — đây là contract R8.6 mà mọi PaymentFlowHandler phải giữ.
        handler = FakeHandler(run_delay=5.0, respect_cancel=True)
        manager.register_handler("ideal", handler)

        try:
            if stop_when == "pending":
                # `max_concurrent=1` → job thứ 2 nằm ở PENDING chờ job 1
                # xong. Đây là cách reproducible duy nhất để chắc chắn job 2
                # ở trạng thái PENDING tại thời điểm gọi `stop`.
                await manager.update_max_concurrent(1)
                batch = await manager.submit_batch(
                    "ideal", ["a@x|p", "b@x|p"]
                )
                assert len(batch.created_job_ids) == 2
                running_id, pending_id = batch.created_job_ids

                # Chờ job 1 thực sự chuyển RUNNING (scheduler đã acquire slot
                # + spawn handler_task) — bảo đảm job 2 vẫn nằm ở PENDING
                # trong hàng đợi khi ta gọi `stop`.
                ok_running = await _wait_until(
                    lambda: manager.get_job(running_id) is not None
                    and manager.get_job(running_id).status  # type: ignore[union-attr]
                    == JobStatus.RUNNING,
                    timeout=1.0,
                )
                assert ok_running, "Job 1 không chuyển RUNNING trong 1s"
                assert (
                    manager.get_job(pending_id).status  # type: ignore[union-attr]
                    == JobStatus.PENDING
                )

                await manager.stop(pending_id)

                # State-transition cho pending job là đồng bộ ngay trong
                # `stop()` (dưới lock) → assert ngay không cần polling.
                assert (
                    manager.get_job(pending_id).status  # type: ignore[union-attr]
                    == JobStatus.STOPPED
                )
                # Scheduler chưa từng chạm tới job 2 (slot=1 đã bị job 1
                # chiếm) → `proxy_pool.acquire(pending_id)` chưa từng gọi.
                # Bất biến này đúng cho cả `direct` (không có lease vẫn gọi
                # acquire → trả None) và `lease` (acquire → trả FakeLease).
                assert pending_id not in proxy_pool.acquired_for
            else:
                # stop_when == "running"
                batch = await manager.submit_batch("ideal", ["a@x|p"])
                assert len(batch.created_job_ids) == 1
                running_id = batch.created_job_ids[0]

                ok_running = await _wait_until(
                    lambda: manager.get_job(running_id) is not None
                    and manager.get_job(running_id).status  # type: ignore[union-attr]
                    == JobStatus.RUNNING,
                    timeout=1.0,
                )
                assert ok_running, "Job không chuyển RUNNING trong 1s"

                # Trước khi stop, lease (nếu có) đã được acquire nhưng CHƯA
                # release — `_run_handler` chỉ release ở finally block sau
                # khi handler exit. Baseline này bảo đảm assertion release
                # sau `stop` là do đúng `stop` gây ra, không phải lease đã
                # released trước đó.
                if proxy_mode == "lease":
                    assert running_id in proxy_pool.acquired_for
                    assert proxy_pool.released == [], (
                        "Lease bị release trước khi stop — vi phạm baseline"
                    )

                await manager.stop(running_id)

                # Bounded time ≤500ms cho case running: FakeHandler poll
                # cancel mỗi 5ms → thực tế thoát rất nhanh, biên 500ms rộng
                # để tránh flaky trên CI chậm.
                ok_stopped = await _wait_until(
                    lambda: manager.get_job(running_id).status  # type: ignore[union-attr]
                    == JobStatus.STOPPED,
                    timeout=0.5,
                )
                assert ok_stopped, (
                    "Job không chuyển STOPPED trong 500ms sau khi stop"
                )

                # Lease phải được release đúng lease đã acquire. FakeLease
                # dùng proxy_id cố định "proxy-1" — release list phải là
                # đúng ["proxy-1"] (không dup, không thiếu).
                if proxy_mode == "lease":
                    assert proxy_pool.released == ["proxy-1"], (
                        f"Lease chưa release đúng: {proxy_pool.released!r}"
                    )
                else:
                    # Direct_Mode: `acquire` trả `None` → không có lease
                    # nào để release, danh sách released rỗng.
                    assert proxy_pool.released == []
        finally:
            await _cancel_manager_tasks(manager)

    asyncio.run(_run())
