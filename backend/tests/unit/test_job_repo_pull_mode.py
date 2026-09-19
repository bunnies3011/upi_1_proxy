"""Unit test cho `JobRepository` mở rộng Pull_Mode (task 3, spec
telegram-pull-job-mode).

Cover:
- Round-trip `upsert()`/`list_all()` cho đủ 10 field mới trên `JobRow`.
- `upsert_worker_stat_delta()` cộng dồn đúng qua nhiều lần gọi (không ghi
  đè — Requirement 15.3, 15.4, 22.12).
- `username`/`first_name` giữ nguyên (COALESCE) khi truyền `None` ở lần
  gọi sau.
- Chống lost-update: N task `asyncio.gather` cùng tăng `success_count`
  cho 1 `telegram_user_id` → tổng đúng bằng N.
- `list_worker_stats()` sort đúng thứ tự (Requirement 16.1).
- `reset_worker_stats()` xoá điểm nhưng giữ danh sách worker (Requirement
  17.2).

Dùng `DbEngine` THẬT với file SQLite tạm qua fixture `sqlite_path` (định
nghĩa ở `tests/conftest.py`) — không mock DB.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.core.db import DbEngine
from app.core.job_repo import JobRepository, JobRow


async def _make_repo(sqlite_path: Path) -> tuple[DbEngine, JobRepository]:
    engine = DbEngine(sqlite_path)
    await engine.init_schema()
    return engine, JobRepository(engine)


def _sample_job_row(job_id: str = "job-1") -> JobRow:
    """1 `JobRow` với đủ 10 field mới populate giá trị non-default để
    round-trip test phát hiện được nếu field nào bị bỏ sót khi
    serialize/deserialize.
    """
    return JobRow(
        job_id=job_id,
        payment_method="ideal",
        account_line="acc-line-1",
        status="pending",
        order_num=1,
        created_at=1000.0,
        updated_at=1000.0,
        dedup_key="dedup-1",
        artifact_path=None,
        error_code=None,
        error_message=None,
        payment_link=None,
        settings_snapshot={"foo": "bar"},
        pull_assignment_state="assigned",
        pull_assigned_telegram_user_id="tg-user-42",
        pull_origin_chat_id="chat-99",
        pull_outcome="success",
        pull_assigned_username="worker_username",
        pull_assigned_first_name="Worker First Name",
        plus_check_state="checking",
        plus_check_attempts=1,
        telegram_message_chat_id="chat-msg-1",
        telegram_message_id=555,
    )


async def test_upsert_list_all_round_trip_10_new_fields(sqlite_path: Path) -> None:
    """`upsert()` rồi `list_all()` phải trả lại đúng 10 field Pull_Mode/
    Plus_Verify — không field nào bị mất hoặc lệch tên cột.
    """
    engine, repo = await _make_repo(sqlite_path)
    try:
        row = _sample_job_row()
        await repo.upsert(row)

        loaded = await repo.list_all()
        assert len(loaded) == 1
        got = loaded[0]

        assert got.pull_assignment_state == "assigned"
        assert got.pull_assigned_telegram_user_id == "tg-user-42"
        assert got.pull_origin_chat_id == "chat-99"
        assert got.pull_outcome == "success"
        assert got.pull_assigned_username == "worker_username"
        assert got.pull_assigned_first_name == "Worker First Name"
        assert got.plus_check_state == "checking"
        assert got.plus_check_attempts == 1
        assert got.telegram_message_chat_id == "chat-msg-1"
        assert got.telegram_message_id == 555
    finally:
        await engine.close()


async def test_upsert_worker_stat_delta_accumulates_across_calls(
    sqlite_path: Path,
) -> None:
    """Gọi `upsert_worker_stat_delta` nhiều lần liên tiếp cho cùng
    `telegram_user_id` phải CỘNG DỒN, không ghi đè (Requirement 15.3, 15.4).
    """
    engine, repo = await _make_repo(sqlite_path)
    try:
        await repo.upsert_worker_stat_delta(
            "tg-1", success_delta=1, username="alice", first_name="Alice"
        )
        await repo.upsert_worker_stat_delta("tg-1", success_delta=1)
        await repo.upsert_worker_stat_delta("tg-1", fail_delta=1)
        await repo.upsert_worker_stat_delta("tg-1", success_delta=2, fail_delta=1)

        stats = await repo.list_worker_stats()
        assert len(stats) == 1
        assert stats[0].telegram_user_id == "tg-1"
        assert stats[0].success_count == 4  # 1 + 1 + 0 + 2
        assert stats[0].fail_count == 2  # 0 + 0 + 1 + 1
    finally:
        await engine.close()


async def test_upsert_worker_stat_delta_preserves_username_when_none(
    sqlite_path: Path,
) -> None:
    """Lần gọi sau truyền `username=None`/`first_name=None` phải GIỮ
    NGUYÊN giá trị cũ đã lưu (hành vi COALESCE)."""
    engine, repo = await _make_repo(sqlite_path)
    try:
        await repo.upsert_worker_stat_delta(
            "tg-2", success_delta=1, username="bob", first_name="Bob"
        )
        await repo.upsert_worker_stat_delta("tg-2", success_delta=1)

        stats = await repo.list_worker_stats()
        assert len(stats) == 1
        assert stats[0].username == "bob"
        assert stats[0].first_name == "Bob"
        assert stats[0].success_count == 2
    finally:
        await engine.close()


async def test_upsert_worker_stat_delta_concurrent_no_lost_update(
    sqlite_path: Path,
) -> None:
    """N task `asyncio.gather` cùng tăng `success_count` cho 1
    `telegram_user_id` — atomic `success_count = success_count + delta`
    trong SQL phải chống lost-update, tổng cuối cùng đúng bằng N.
    """
    engine, repo = await _make_repo(sqlite_path)
    try:
        n = 20

        async def _bump() -> None:
            await repo.upsert_worker_stat_delta("tg-concurrent", success_delta=1)

        await asyncio.gather(*[_bump() for _ in range(n)])

        stats = await repo.list_worker_stats()
        assert len(stats) == 1
        assert stats[0].success_count == n
    finally:
        await engine.close()


async def test_list_worker_stats_sort_order(sqlite_path: Path) -> None:
    """Sort: `success_count DESC, fail_count ASC, telegram_user_id ASC`
    (Requirement 16.1)."""
    engine, repo = await _make_repo(sqlite_path)
    try:
        # worker "b": success=5, fail=1
        await repo.upsert_worker_stat_delta("worker-b", success_delta=5, fail_delta=1)
        # worker "a": success=5, fail=0 -> cùng success cao hơn fail thấp hơn "b" -> đứng trước "b"
        await repo.upsert_worker_stat_delta("worker-a", success_delta=5, fail_delta=0)
        # worker "c": success=10 -> cao nhất, đứng đầu
        await repo.upsert_worker_stat_delta("worker-c", success_delta=10)
        # worker "d": success=1 -> thấp nhất, đứng cuối
        await repo.upsert_worker_stat_delta("worker-d", success_delta=1)

        stats = await repo.list_worker_stats()
        ordered_ids = [s.telegram_user_id for s in stats]
        assert ordered_ids == ["worker-c", "worker-a", "worker-b", "worker-d"]
    finally:
        await engine.close()


async def test_reset_worker_stats_zeroes_counts_preserves_identity(
    sqlite_path: Path,
) -> None:
    """`reset_worker_stats()` set `success_count`/`fail_count` về 0 cho
    TOÀN BỘ worker, giữ nguyên `telegram_user_id`/`username`/`first_name`
    (Requirement 17.2)."""
    engine, repo = await _make_repo(sqlite_path)
    try:
        await repo.upsert_worker_stat_delta(
            "tg-3", success_delta=3, fail_delta=2, username="carol", first_name="Carol"
        )
        await repo.upsert_worker_stat_delta(
            "tg-4", success_delta=7, fail_delta=1, username="dave", first_name="Dave"
        )

        await repo.reset_worker_stats()

        stats = await repo.list_worker_stats()
        by_id = {s.telegram_user_id: s for s in stats}
        assert by_id["tg-3"].success_count == 0
        assert by_id["tg-3"].fail_count == 0
        assert by_id["tg-3"].username == "carol"
        assert by_id["tg-3"].first_name == "Carol"
        assert by_id["tg-4"].success_count == 0
        assert by_id["tg-4"].fail_count == 0
        assert by_id["tg-4"].username == "dave"
        assert by_id["tg-4"].first_name == "Dave"
    finally:
        await engine.close()
