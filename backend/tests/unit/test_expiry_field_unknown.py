"""Task 35.5 — Unit test field thời điểm hết hạn của IdealJob = "không
xác định" khi HAR chưa xác nhận TTL.

**Validates: Requirements 7.8**

R7.8: "WHERE HAR log không xác nhận rõ thời điểm hết hạn (TTL) của giao
dịch/QR iDEAL, THE Backend_Service SHALL ghi nhận trường thời điểm hết
hạn của IdealJob là 'không xác định' (KHÔNG gán giá trị số mặc định tự
suy đoán), và SHALL cho phép override qua Settings_Store khi giá trị này
được xác nhận trong quá trình implement/kiểm thử thực tế."

Interpret cho unit test:

    - Design hiện tại: `_JobRecord` (`core/job_manager.py`) và `Job`
      (`core/payment_flow.py`) đều KHÔNG có field `expires_at`/`expiration`/
      `ttl` — đây chính là cách hiện thực R7.8 (không có giá trị số mặc
      định tự suy đoán vì HAR chưa xác nhận TTL).

    - Nếu tương lai thêm field expiration để support override qua
      Settings_Store: giá trị mặc định (khi HAR chưa xác nhận) PHẢI là
      `None` — sentinel "không xác định", KHÔNG được là số.

Test verify cả 2 hình thức:
    (a) Field expiration vắng mặt (mặc định hiện tại) — đúng intent R7.8.
    (b) Field tồn tại nhưng = `None` — sentinel "không xác định" hợp lệ.
Cách hiện thực VI PHẠM: field = số (int/float) khi HAR chưa cấp giá trị.
"""

from __future__ import annotations

import time
from dataclasses import fields
from typing import Any

import pytest

from app.core.job_manager import JobManager, _JobRecord
from app.core.payment_flow import (
    AccountLineError,
    Job,
    JobResult,
    JobStatus,
    ParsedAccount,
    SimpleCancellationToken,
)

# ---------------------------------------------------------------------------
# Danh sách tên field có thể được dùng cho "thời điểm hết hạn" — kiểm cả
# 6 tên để tránh né test bằng cách đổi tên field.
# ---------------------------------------------------------------------------

_EXPIRATION_FIELD_NAMES: tuple[str, ...] = (
    "expires_at",
    "expiration",
    "expiration_at",
    "qr_expires_at",
    "ttl",
    "ttl_seconds",
)


def _extract_expiration_value(obj: Any) -> tuple[str, Any] | None:
    """Trả `(field_name, value)` nếu obj có 1 trong các field expiration;
    `None` nếu không có field nào."""
    for name in _EXPIRATION_FIELD_NAMES:
        if hasattr(obj, name):
            return name, getattr(obj, name)
    return None


# ---------------------------------------------------------------------------
# Test (a) — _JobRecord không có expiration số
# ---------------------------------------------------------------------------


def test_job_record_expiration_field_is_none_or_absent() -> None:
    """`_JobRecord` (core/job_manager.py) KHÔNG được có field expiration
    với giá trị số. Chấp nhận:
        (a) field vắng mặt hoàn toàn (mặc định hiện tại).
        (b) field tồn tại nhưng = `None` (sentinel "không xác định").

    Vi phạm: field expiration = số (int/float).

    **Validates: Requirements 7.8**
    """
    token = SimpleCancellationToken()
    job = Job(
        job_id="job-7-8-probe",
        payment_method="ideal",
        account_line="a@x|p",
        created_at=time.time(),
        cancellation_token=token,
    )
    record = _JobRecord(job=job, status=JobStatus.PENDING)

    extracted = _extract_expiration_value(record)
    if extracted is not None:
        field_name, value = extracted
        assert value is None, (
            f"_JobRecord.{field_name} = {value!r} — vi phạm R7.8: HAR chưa "
            f"xác nhận TTL → field expiration PHẢI là None (sentinel 'không "
            f"xác định'), KHÔNG được là số mặc định tự suy đoán."
        )


# ---------------------------------------------------------------------------
# Test (b) — Job dataclass không có expiration field (thuộc scope payments/*)
# ---------------------------------------------------------------------------


def test_job_dataclass_has_no_expiration_field() -> None:
    """`Job` (core.payment_flow) — DTO generic — không được có field
    expiration số. Nếu HAR sau này xác nhận TTL, field expiration nên
    sống ở tầng `payments/*` (nơi biết cấu trúc TTL của payment method
    cụ thể), KHÔNG phải ở tầng `core/` generic.

    **Validates: Requirements 7.8**
    """
    field_names = {f.name for f in fields(Job)}
    conflicts = field_names.intersection(_EXPIRATION_FIELD_NAMES)
    assert not conflicts, (
        f"Job dataclass CÓ field expiration: {sorted(conflicts)}. R7.8: "
        "trường expiration phải sống ở tầng payments/* (khi HAR xác nhận "
        "TTL) hoặc KHÔNG tồn tại (khi HAR chưa xác nhận). Field ở tầng "
        "core generic dễ dẫn tới giá trị số mặc định tự suy đoán."
    )


# ---------------------------------------------------------------------------
# Test (c) — End-to-end: submit_batch tạo record không gán expiration số
# ---------------------------------------------------------------------------


class _FakeSettings:
    async def list(self, prefix: str | None = None) -> dict[str, Any]:  # noqa: ARG002
        return {}


class _FakeProxyPool:
    async def acquire(self, job_id: str):  # noqa: ARG002
        return None

    def release(self, lease) -> None:  # noqa: ARG002
        return None


class _FakeSse:
    async def broadcast_job_status(
        self, job_id: str, status: str, **extra
    ) -> None:
        return None

    async def broadcast_job_log(
        self, job_id: str, message: str, **extra
    ) -> None:
        return None


class _FakeIdealHandler:
    """Parse thành công mọi dòng, `run` không được gọi trong test này —
    chỉ cần `submit_batch` tạo được record để inspect field expiration."""

    def get_max_concurrent_key(self) -> str:
        return "ideal.max_concurrent"

    def parse_account_line(self, line: str):
        if not line.strip():
            return AccountLineError(line=line, reason="empty")
        return ParsedAccount(raw_line=line)

    async def run(self, job: Job, proxy_lease):  # noqa: ARG002
        return JobResult(status=JobStatus.QR_READY)


async def test_new_job_record_via_submit_batch_has_no_numeric_expiration() -> None:
    """End-to-end: sau `JobManager.submit_batch`, record mới tạo KHÔNG có
    field expiration số. Bổ sung cho test (a) — bắt trường hợp
    `submit_batch` gán expiration động qua `setattr` chứ không qua
    dataclass field.

    **Validates: Requirements 7.8**
    """
    manager = JobManager(
        settings=_FakeSettings(),  # type: ignore[arg-type]
        proxy_pool=_FakeProxyPool(),  # type: ignore[arg-type]
        sse=_FakeSse(),  # type: ignore[arg-type]
    )
    manager.register_handler("ideal", _FakeIdealHandler())  # type: ignore[arg-type]

    result = await manager.submit_batch("ideal", ["probe@x|p"])
    assert len(result.created_job_ids) == 1
    job_id = result.created_job_ids[0]
    record = manager.get_job(job_id)
    assert record is not None

    # Kiểm cả record lẫn record.job — tránh né bằng cách đặt field ở
    # record.job thay vì record.
    for target_name, target_obj in (("record", record), ("record.job", record.job)):
        extracted = _extract_expiration_value(target_obj)
        if extracted is not None:
            field_name, value = extracted
            assert value is None, (
                f"{target_name}.{field_name} = {value!r} — vi phạm R7.8: "
                f"expiration PHẢI là None khi HAR chưa xác nhận TTL."
            )

    # Cleanup: cancel scheduler task để không leak pending task khi event
    # loop của test đóng.
    task = manager._scheduler_task  # noqa: SLF001
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (BaseException,):
            pass


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
