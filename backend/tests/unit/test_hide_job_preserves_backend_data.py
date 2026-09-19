"""Task 35.7: Unit test ẩn job khỏi UI KHÔNG xoá log/dữ liệu backend.

Validates: Requirements 8.7

Requirement 8.7 (trích): "WHEN user yêu cầu ẩn 1 IdealJob đã kết thúc
(`qr_ready`, `error`, hoặc `stopped`) khỏi danh sách hiển thị, THE
Frontend_App SHALL không hiển thị job đó nữa trên Single_Screen_UI, nhưng
THE Backend_Service SHALL KHÔNG xoá log/dữ liệu của job đó (giữ nguyên
cho mục đích audit, nhất quán với Requirement 14)."

Kiến trúc: "hide" là hành động **client-side thuần** — `useJobsStore.
hideJob(jobId)` chỉ thêm `jobId` vào set `hiddenJobIds` (local state),
KHÔNG gọi API DELETE/hide xuống backend. Backend KHÔNG có endpoint `hide`
và KHÔNG có concept `hidden` trong `_JobRecord`.

Do đó bằng chứng của R8.7 ở tầng HTTP là **INVARIANT**: bất kể client đã
"hide" job hay chưa, `GET /api/jobs/{id}` LUÔN trả detail đầy đủ (logs,
artifact_path, error_code, error_message) và `GET /api/jobs` LUÔN chứa
job đó trong list. Test này chạy 3 bước:

1. Test 1 (baseline): Inject 1 job ERROR có logs/artifact_path/error_code/
   error_message. Gọi `GET /api/jobs/{id}` → response chứa đầy đủ các
   field trên. Đây là snapshot chuẩn.
2. Test 2 (list): Gọi `GET /api/jobs` → job có trong list. Chứng minh
   backend KHÔNG filter theo hidden state (vì backend không có concept
   này).
3. Test 3 (idempotent read): Gọi `GET /api/jobs/{id}` lần nữa sau khi
   client "hide" (mô phỏng bằng no-op — không có API để gọi) → response
   KHỚP CHÍNH XÁC baseline. Backend không mutate/xoá dữ liệu qua chuỗi
   read-only.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.job_manager import _JobRecord
from app.core.payment_flow import Job, JobStatus, SimpleCancellationToken


# ---------------------------------------------------------------------------
# Hằng số bootstrap env + auth token cho test file.
# 2 env var duy nhất được phép bootstrap Backend_Service (Requirement 11.1
# — chicken-and-egg); mọi config khác đi qua Settings_Store SQLite.
# ---------------------------------------------------------------------------

_ENV_DB_PATH = "IDEAL_QR_TOOL_DB_PATH"
_ENV_BIND_HOST = "IDEAL_QR_TOOL_BIND_HOST"
_LOOPBACK_HOST = "127.0.0.1"
_TEST_TOKEN = "task35_7-hide-preserve-data-token"

# HTTP client timeout — mọi request PHẢI phản hồi trong 2s. TestClient dùng
# httpx sync qua ASGI in-process nên không có network I/O; giới hạn 2s là
# đủ rộng để bắt bất kỳ hang bất thường nào (theo yêu cầu "chạy đừng để
# stuck"). Toàn bộ 3 test cộng lại << 10s.
_HTTP_TIMEOUT_SECONDS = 2.0

# Schema `settings` khớp `app/core/db.py::_CREATE_SETTINGS_TABLE` — seed
# sync qua sqlite3 trước khi aiosqlite của DbEngine mở lại trong lifespan.
_SEED_SETTINGS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


def _seed_auth_token(db_path: Path, token: str) -> None:
    """Ghi `web.auth_token` vào SQLite qua sqlite3 sync — mô phỏng người
    vận hành đã set token qua CLI/Settings API trước khi start Backend.

    Value = JSON-encoded string (khớp `SettingsRepository.set` dùng
    `json.dumps(value)`) để `settings.get` trong lifespan startup decode
    ra đúng chuỗi thô.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute(_SEED_SETTINGS_TABLE_SQL)
        connection.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?);",
            ("web.auth_token", json.dumps(token)),
        )
        connection.commit()
    finally:
        connection.close()


def _build_terminal_error_record(job_id: str, artifact_path: str) -> _JobRecord:
    """Build 1 `_JobRecord` ERROR đầy đủ log/artifact/error để test invariant.

    Cấu trúc log entries khớp `_JobRecord.logs` (dict `{ts, message, **extra}`
    theo `JobManager._append_log`). Ở đây thêm `level` vào extra (frontend
    dùng field này để phân biệt info/warn/error khi render), khớp mô tả
    trong task ("list dict `{ts, message, level}`").

    KHÔNG dùng `submit_batch()` — scheduler sẽ chạy handler thật gây flake
    và không cần thiết cho unit test contract HTTP. Inject trực tiếp giữ
    test đơn giản, deterministic.
    """
    job = Job(
        job_id=job_id,
        payment_method="ideal",
        account_line="audit-user@example.com|password123|totpseed",
        created_at=time.time() - 30.0,
        cancellation_token=SimpleCancellationToken(),
    )
    now = time.time()
    logs: list[dict] = [
        {
            "ts": now - 25.0,
            "message": "step-1: payment_intent_created",
            "level": "info",
        },
        {
            "ts": now - 20.0,
            "message": "step-2: elements_session_ready",
            "level": "info",
        },
        {
            "ts": now - 10.0,
            "message": "step-3: confirmation_failed_upstream_5xx",
            "level": "error",
            "error_code": "upstream_confirm_failed",
        },
        {
            "ts": now - 5.0,
            "message": "internal_error",
            "level": "error",
            "error_code": "internal_error",
            "error_type": "RuntimeError",
            "error_message": "upstream 502 after 3 retries",
        },
    ]
    return _JobRecord(
        job=job,
        status=JobStatus.ERROR,
        logs=logs,
        updated_at=now,
        artifact_path=artifact_path,
        error_code="internal_error",
        error_message="upstream 502 after 3 retries",
    )


@pytest.fixture
def api_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> "tuple[TestClient, object, str]":
    """Bootstrap FastAPI app 1 lần cho test file với DB tạm.

    Trả về `(client, job_manager, job_id)` — `job_id` là job ERROR đã
    inject sẵn với logs/artifact/error đầy đủ, các test dùng để gọi
    `GET /api/jobs/{job_id}`.

    Fixture scope function (default) — mỗi test 1 DB tạm để cách ly hoàn
    toàn state (không rò settings/jobs sang test khác).
    """
    db_path = tmp_path / "hide_preserve_data.db"
    _seed_auth_token(db_path, _TEST_TOKEN)
    monkeypatch.setenv(_ENV_DB_PATH, str(db_path))
    monkeypatch.setenv(_ENV_BIND_HOST, _LOOPBACK_HOST)

    # Defer import SAU khi env đã set — `create_app()` resolve env mỗi lần
    # gọi (không phải module-level side-effect), nên import ở đây an toàn.
    from app.main import create_app  # noqa: PLC0415

    app = create_app()
    with TestClient(app) as client:
        # Giới hạn timeout HTTP để test không treo nếu lifespan/route có
        # deadlock bất thường (yêu cầu user: "chạy đừng để stuck").
        client.timeout = _HTTP_TIMEOUT_SECONDS

        # Inject 1 job ERROR đầy đủ log/artifact/error. artifact_path chỉ
        # là chuỗi placeholder — endpoint `GET /api/jobs/{id}` KHÔNG đọc
        # file (chỉ `GET /api/jobs/{id}/qr.png` mới đọc). Không cần tạo
        # file thật để test contract JSON response.
        job_id = uuid.uuid4().hex
        record = _build_terminal_error_record(
            job_id, artifact_path=f"/tmp/audit-qr-{job_id}.png"
        )
        app.state.job_manager._jobs[job_id] = record  # noqa: SLF001

        yield client, app.state.job_manager, job_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_get_job_detail_returns_full_data_baseline(api_client) -> None:
    """Baseline: `GET /api/jobs/{id}` trả detail đầy đủ cho job ERROR.

    Validates: Requirements 8.7 (tiền đề — nếu detail không đầy đủ ở
    baseline thì assertion "sau hide vẫn đầy đủ" mất ý nghĩa).
    """
    client, _job_manager, job_id = api_client

    response = client.get(
        f"/api/jobs/{job_id}",
        headers={"X-Auth-Token": _TEST_TOKEN},
    )

    assert response.status_code == 200, (
        f"Job detail phải trả 200, nhận {response.status_code}: {response.text}"
    )
    body = response.json()

    # Field cấu trúc của job
    assert body["job_id"] == job_id
    assert body["payment_method"] == "ideal"
    assert body["status"] == "error"

    # Logs phải đầy đủ 4 entry đã inject, giữ nguyên thứ tự theo `ts` tăng.
    logs = body["logs"]
    assert isinstance(logs, list) and len(logs) == 4, (
        f"logs phải có đúng 4 entry đã inject, nhận {len(logs)}: {logs}"
    )
    messages = [entry["message"] for entry in logs]
    assert messages == [
        "step-1: payment_intent_created",
        "step-2: elements_session_ready",
        "step-3: confirmation_failed_upstream_5xx",
        "internal_error",
    ], f"Thứ tự/nội dung message log KHÔNG khớp inject: {messages}"

    # `level` (không phải field cố định) phải nằm trong `extra` — schema
    # `JobLogEntry` đưa mọi key ngoài `ts`/`message` vào `extra`.
    for entry in logs:
        assert "extra" in entry, f"Log entry thiếu `extra`: {entry}"
        assert "level" in entry["extra"], (
            f"Log entry `extra` thiếu `level` — inject có, response mất: {entry}"
        )

    # artifact_path / error_code / error_message phải được truyền nguyên
    # xi từ `_JobRecord` (Requirement 12.3).
    assert body["artifact_path"] == f"/tmp/audit-qr-{job_id}.png", (
        f"artifact_path mất/đổi so với inject: {body['artifact_path']!r}"
    )
    assert body["error_code"] == "internal_error"
    assert body["error_message"] == "upstream 502 after 3 retries"


def test_get_jobs_list_still_contains_job_after_client_hide(api_client) -> None:
    """`GET /api/jobs` VẪN chứa job dù client đã "hide".

    Frontend "hide" là hành động client-side thuần (`useJobsStore.
    hideJob()` chỉ mutate `hiddenJobIds` set local), KHÔNG có API backend
    nào để gọi. Backend KHÔNG có concept `hidden` nên list endpoint LUÔN
    trả toàn bộ job — filter (nếu có) là trách nhiệm của Frontend_App.

    Test này mô phỏng "client hide" bằng no-op (không có endpoint để gọi)
    và verify backend list vẫn chứa job.

    Validates: Requirements 8.7.
    """
    client, _job_manager, job_id = api_client

    # "Client hide" xảy ra hoàn toàn ở frontend — không có request nào đi
    # tới backend. Test chỉ verify contract HTTP: list endpoint không lọc
    # theo trạng thái ẩn (vì trạng thái đó không tồn tại ở backend).

    response = client.get(
        "/api/jobs",
        headers={"X-Auth-Token": _TEST_TOKEN},
    )

    assert response.status_code == 200, (
        f"List jobs phải trả 200, nhận {response.status_code}: {response.text}"
    )
    body = response.json()
    assert isinstance(body, list), f"Response phải là list, nhận {type(body)}"

    job_ids_in_list = [item["job_id"] for item in body]
    assert job_id in job_ids_in_list, (
        f"Job {job_id} đã bị filter khỏi list — vi phạm R8.7: backend "
        f"KHÔNG có concept hidden, list PHẢI chứa tất cả job. "
        f"Nhận: {job_ids_in_list}"
    )

    # Job trong list phải giữ đúng status ERROR (không bị mutate qua chuỗi
    # read-only).
    job_entry = next(item for item in body if item["job_id"] == job_id)
    assert job_entry["status"] == "error", (
        f"Job status trong list KHÔNG khớp inject (ERROR): {job_entry}"
    )
    assert job_entry["payment_method"] == "ideal"


def test_get_job_detail_after_hide_matches_baseline_exactly(api_client) -> None:
    """`GET /api/jobs/{id}` LẶP LẠI trả response KHỚP CHÍNH XÁC baseline.

    Đây là kiểm chứng invariant "hide = no-op ở backend": không có tương
    tác nào từ Frontend "hide" (không có API để gọi) làm mutate/xoá dữ
    liệu ở backend. Chuỗi GET read-only PHẢI trả response byte-identical.

    Validates: Requirements 8.7.
    """
    client, _job_manager, job_id = api_client

    # Lần 1 — baseline (trước khi "hide"). Toàn bộ detail nằm ở đây.
    first = client.get(
        f"/api/jobs/{job_id}",
        headers={"X-Auth-Token": _TEST_TOKEN},
    )
    assert first.status_code == 200, first.text
    baseline = first.json()

    # Mô phỏng "client hide" — không có API DELETE hide để gọi, chỉ là
    # thao tác ở view layer của frontend. Chờ tối thiểu để chắc chắn
    # không có timing coincidence (không cần thiết logic-wise, nhưng
    # bắt bug "response giống nhau vì cache" nếu có).
    time.sleep(0.02)

    # Lần 2 — SAU khi "hide" (client đã ẩn khỏi UI). Backend PHẢI trả
    # detail y hệt lần 1.
    second = client.get(
        f"/api/jobs/{job_id}",
        headers={"X-Auth-Token": _TEST_TOKEN},
    )
    assert second.status_code == 200, second.text
    after_hide = second.json()

    # So sánh EXACT JSON — logs, artifact_path, error_code, error_message
    # đều phải khớp từng byte. Không dùng subset match để bắt cả trường
    # hợp thêm/mất field ngầm định.
    assert after_hide == baseline, (
        "Response GET /api/jobs/{id} SAU khi client 'hide' KHÔNG khớp "
        "baseline — backend đã mutate/xoá dữ liệu qua chuỗi read-only, "
        "vi phạm R8.7.\n"
        f"Baseline: {baseline}\n"
        f"Sau hide: {after_hide}"
    )

    # Chốt lại: đảm bảo dữ liệu quan trọng vẫn nguyên vẹn (double-check
    # sau equality — nếu equality pass mà 1 trong 3 field dưới là None
    # thì test setup bị sai chứ không phải backend sai).
    assert len(after_hide["logs"]) == 4, (
        f"logs bị mất sau 'hide': còn {len(after_hide['logs'])} entry, "
        "kỳ vọng 4"
    )
    assert after_hide["artifact_path"] is not None
    assert after_hide["error_code"] == "internal_error"
    assert after_hide["error_message"] == "upstream 502 after 3 retries"


def test_repeated_reads_do_not_mutate_backend_state(api_client) -> None:
    """5 lần GET liên tiếp trả response giống hệt nhau — invariant read-only.

    Bổ trợ cho `test_get_job_detail_after_hide_matches_baseline_exactly`:
    ngoài 2 lần compare, chạy 5 lần để bắt trường hợp mutate xảy ra ở
    lần thứ N (>2). Nếu tương lai có ai đó thêm side-effect vào code
    path GET (log request, decrement counter, xoá field...), test này
    bắt được.

    Validates: Requirements 8.7.
    """
    client, _job_manager, job_id = api_client

    snapshots: list[dict] = []
    for _ in range(5):
        response = client.get(
            f"/api/jobs/{job_id}",
            headers={"X-Auth-Token": _TEST_TOKEN},
        )
        assert response.status_code == 200, response.text
        snapshots.append(response.json())

    first = snapshots[0]
    for index, snap in enumerate(snapshots[1:], start=1):
        assert snap == first, (
            f"Snapshot #{index} khác snapshot #0 — GET detail mutate "
            f"state qua chuỗi read-only, vi phạm R8.7.\n"
            f"#0:  {first}\n"
            f"#{index}: {snap}"
        )
