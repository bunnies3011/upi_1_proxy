"""Unit test cho route `POST /api/settings/telegram/mode` (task 34, spec
telegram-pull-job-mode).

Cover (Requirement 3.1, 3.2, 3.3) ở tầng ROUTE — khác `test_job_manager_
mode_switch.py` (task 19) test trực tiếp `JobManager.set_operating_mode`.
Ở đây kiểm tra hợp đồng HTTP của endpoint đổi mode: mapping
`ModeSwitchBlockedError` -> 409 kèm `job_ids`, pass-through `force`, và
đọc-lại giá trị đã persist trả về client.

Cách test: gọi TRỰC TIẾP route coroutine `set_telegram_mode(...)` với các
dependency đã inject (giống pattern "gọi trực tiếp route function") thay vì
dựng full FastAPI app + DB. Lý do:

- `JobManager` là instance THẬT (không mock) — logic Mode_Switch_Guard chạy
  thật, đảm bảo test phản ánh đúng hành vi R3.1/3.2/3.3.
- Chỉ các biên I/O (Settings_Store, ProxyPool, SSE) là test double, tái
  dùng đúng bộ fake đã dùng cho unit test `JobManager` — tránh phụ thuộc
  DB/lifespan để test nhanh và tất định.

`settings_repo` truyền vào route CHÍNH LÀ instance settings mà `JobManager`
đang giữ, nên sau khi `set_operating_mode` gọi `settings.set(...)`, route
đọc lại `settings.get("telegram.mode")` thấy giá trị mới — đúng luồng thật.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.responses import JSONResponse

from app.api.routes_settings import set_telegram_mode
from app.api.schemas import SetTelegramModeRequest, SetTelegramModeResponse
from app.core.job_manager import JobManager, PullOutcome
from app.core.payment_flow import JobResult, JobStatus

from tests.unit.test_job_manager import FakeHandler
from tests.unit.test_job_manager_mode_switch import _make_mode_switch_manager
from tests.unit.test_job_manager_pull_mode import _seed_accounts


class _RouteFakeSse:
    """Test double cho `SseBroadcaster` ở tầng route — chỉ cần
    `broadcast_setting_updated` mà `set_telegram_mode` gọi trên nhánh
    thành công. Ghi lại lời gọi để assert broadcast đã xảy ra."""

    def __init__(self) -> None:
        self.setting_updated_calls: list[dict[str, Any]] = []

    async def broadcast_setting_updated(
        self, key: str, value: Any, source_client_id: str | None = None
    ) -> None:
        self.setting_updated_calls.append(
            {"key": key, "value": value, "source_client_id": source_client_id}
        )


async def _setup_manager_with_inflight_job() -> tuple[JobManager, Any, _RouteFakeSse, str]:
    """Dựng `JobManager` (mode="pull") có ĐÚNG 1 job Pull_Mode đang dở dang
    (`assigned` + `pending`) qua luồng thật: seed account vào pool rồi
    `claim_pull_account`. Trả về (manager, settings, route_sse, job_id)."""
    manager, settings, _proxy, _sse = _make_mode_switch_manager()
    handler = FakeHandler(run_result=JobResult(status=JobStatus.QR_READY))
    manager.register_handler("ideal", handler)
    await _seed_accounts(manager, 1)
    job_id = await manager.claim_pull_account("worker-1", "chat-1", None, None)
    assert job_id is not None
    return manager, settings, _RouteFakeSse(), job_id


# ---------------------------------------------------------------------------
# R3.3 — Đổi mode thành công khi KHÔNG có job dở dang
# ---------------------------------------------------------------------------


async def test_set_telegram_mode_succeeds_when_no_inflight_jobs() -> None:
    """Không có job Pull_Mode đang dở dang → route trả
    `SetTelegramModeResponse(mode="push")`, settings được ghi giá trị mới,
    và broadcast SSE `setting_updated` đúng key/value (R3.3)."""
    manager, settings, _proxy, _sse = _make_mode_switch_manager()
    route_sse = _RouteFakeSse()

    result = await set_telegram_mode(
        payload=SetTelegramModeRequest(mode="push", force=False),
        x_client_id="client-abc",
        job_manager=manager,
        settings_repo=settings,
        sse=route_sse,
    )

    assert isinstance(result, SetTelegramModeResponse)
    assert result.mode == "push"
    assert ("telegram.mode", "push") in settings.set_calls
    assert await settings.get("telegram.mode") == "push"
    assert route_sse.setting_updated_calls == [
        {"key": "telegram.mode", "value": "push", "source_client_id": "client-abc"}
    ]


# ---------------------------------------------------------------------------
# R3.1 — Còn job dở dang + không force → HTTP 409 kèm job_ids
# ---------------------------------------------------------------------------


async def test_set_telegram_mode_blocked_returns_409_with_job_ids() -> None:
    """Còn job Pull_Mode `assigned`/`pending` và `force=False` → route trả
    `JSONResponse` 409 với body `{"error": "mode_switch_blocked",
    "job_ids": [...]}`; mode KHÔNG được đổi (R3.1)."""
    manager, settings, route_sse, job_id = await _setup_manager_with_inflight_job()

    result = await set_telegram_mode(
        payload=SetTelegramModeRequest(mode="push", force=False),
        x_client_id=None,
        job_manager=manager,
        settings_repo=settings,
        sse=route_sse,
    )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 409
    body = json.loads(result.body)
    assert body["error"] == "mode_switch_blocked"
    assert body["job_ids"] == [job_id]

    # Mode KHÔNG đổi, không broadcast setting_updated.
    assert settings.set_calls == []
    assert await settings.get("telegram.mode") == "pull"
    assert route_sse.setting_updated_calls == []


# ---------------------------------------------------------------------------
# R3.2 — force=true đổi mode thành công + job dở dang chuyển FAIL
# ---------------------------------------------------------------------------


async def test_set_telegram_mode_force_switches_and_fails_inflight_jobs() -> None:
    """`force=true` → route trả `SetTelegramModeResponse(mode="push")`,
    mode được đổi, VÀ job Pull_Mode đang dở dang bị kết luận
    `pull_outcome=FAIL` (R3.2)."""
    manager, settings, route_sse, job_id = await _setup_manager_with_inflight_job()

    result = await set_telegram_mode(
        payload=SetTelegramModeRequest(mode="push", force=True),
        x_client_id=None,
        job_manager=manager,
        settings_repo=settings,
        sse=route_sse,
    )

    assert isinstance(result, SetTelegramModeResponse)
    assert result.mode == "push"
    assert ("telegram.mode", "push") in settings.set_calls
    assert await settings.get("telegram.mode") == "push"

    # Job dở dang đã bị force-fail.
    failed_record = manager.get_job(job_id)
    assert failed_record is not None
    assert failed_record.pull_outcome == PullOutcome.FAIL

    # Thành công → broadcast setting_updated đúng 1 lần.
    assert route_sse.setting_updated_calls == [
        {"key": "telegram.mode", "value": "push", "source_client_id": None}
    ]
