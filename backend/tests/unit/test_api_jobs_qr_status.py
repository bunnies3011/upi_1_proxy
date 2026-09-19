"""Property test cho `GET /api/jobs/{id}/qr.png` (Property 22 — Task 23.2).

**Property 22: API lấy QR chỉ trả ảnh hợp lệ khi job ở đúng trạng thái qr_ready**

Sinh `job_id` ở trạng thái bất kỳ thuộc `Job_Status` (`pending`/`running`/
`qr_ready`/`error`/`stopped`) HOẶC job không tồn tại, assert 3 bất biến của
endpoint (Requirement 7.5):

- Job không tồn tại → 404 JSON `{"error_code": "job_not_found", ...}`.
- Job status ∈ {PENDING, RUNNING, ERROR, STOPPED} → 409 JSON
  `{"error_code": "qr_not_ready", ...}` kèm status hiện tại trong details.
- Job status = QR_READY + artifact_path trỏ tới file PNG hợp lệ trên đĩa
  → 200 với `content-type: image/png` và body binary bắt đầu bằng magic
  bytes `b"\\x89PNG"`.

**Validates: Requirements 7.5**

Test dùng FastAPI TestClient thật (context manager để trigger lifespan
startup + shutdown đầy đủ). `web.auth_token` được seed vào SQLite TRƯỚC
khi `create_app()` chạy — mô phỏng vận hành sản phẩm đã cấu hình token
qua CLI/Settings API trước đó.

Fixture `api_client` scope module (chỉ startup 1 lần cho toàn bộ file)
để `hypothesis` chạy 20 example dưới cùng TestClient — tránh 20 × overhead
lifespan startup. Mỗi example dùng `job_id` uuid unique + inject
`_JobRecord` trực tiếp vào `JobManager._jobs`, cleanup trong `finally` để
không rò state sang example kế tiếp.

Property test chỉ quan tâm route CONTRACT dựa trên state đầu vào — KHÔNG
đi qua `submit_batch()` (tránh scheduler chạy handler thật gây flake).
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, strategies as st

from app.core.job_manager import _JobRecord
from app.core.payment_flow import Job, JobStatus, SimpleCancellationToken


# ---------------------------------------------------------------------------
# Hằng số bootstrap env + auth token cho toàn bộ file test.
# 2 env var duy nhất được phép bootstrap Backend_Service (Requirement 11.1
# — chicken-and-egg); mọi config khác đi qua Settings_Store SQLite.
# ---------------------------------------------------------------------------

_ENV_DB_PATH = "IDEAL_QR_TOOL_DB_PATH"
_ENV_BIND_HOST = "IDEAL_QR_TOOL_BIND_HOST"
_LOOPBACK_HOST = "127.0.0.1"
_TEST_TOKEN = "prop22-test-token-fixture"

# PNG signature chuẩn (8 bytes) — mọi PNG hợp lệ đều bắt đầu bằng chuỗi
# này (RFC 2083). Route trả FileResponse stream nguyên xi từ đĩa, không
# validate cấu trúc PNG → chỉ cần file bắt đầu bằng magic bytes là đủ
# để test contract "content-type=image/png + body PNG signature".
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Payload tối thiểu dùng cho case QR_READY của property test. KHÔNG cần
# là 1 PNG cấu trúc hợp lệ — chỉ cần bắt đầu bằng magic bytes để assert
# `response.content.startswith(_PNG_MAGIC)` đúng. Unit test task 23.3 sẽ
# dùng qrcode render 1 PNG thật đầy đủ.
_STUB_PNG_PAYLOAD = _PNG_MAGIC + b"\x00" * 32

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
    """Ghi `web.auth_token` vào SQLite qua sqlite3 sync — Mô phỏng người
    vận hành đã set token qua CLI trước khi start Backend_Service.

    Format value = JSON-encoded string (khớp `SettingsRepository.set`) để
    lần `settings.get` trong lifespan startup decode ra đúng chuỗi thô.
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


@dataclass
class _ApiHarness:
    """Bundle những gì hypothesis test cần: TestClient để gọi endpoint +
    `JobManager` để inject/cleanup `_JobRecord`, + thư mục tạm để ghi
    file PNG stub cho case QR_READY.
    """

    client: TestClient
    job_manager: object
    tmp_dir: Path


@pytest.fixture(scope="module")
def api_client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_ApiHarness]:
    """Build FastAPI app 1 lần cho toàn bộ file test.

    Hypothesis chạy 20 example (profile 'fast' đã set ở `tests/conftest.py`)
    dưới cùng 1 TestClient — module scope tránh 20 × overhead lifespan
    startup (~200ms/lần với init_schema + apply_settings + register handler
    ideal). Trade-off: mọi example chia sẻ Settings_Store, JobManager
    singleton — nhưng property này chỉ mutate `job_manager._jobs` với
    uuid unique nên KHÔNG rò state (mỗi example tự cleanup).

    `pytest.MonkeyPatch.context()` cho phép set env ở scope module dù
    fixture `monkeypatch` mặc định của pytest là function scope. Set 2
    env var bootstrap để `create_app()` mở đúng DB tạm + validate bind
    host loopback (không raise `StartupSecurityError` khi token đã seed).
    """
    tmp_dir = tmp_path_factory.mktemp("prop22_qr_status")
    db_path = tmp_dir / "test.db"
    _seed_auth_token(db_path, _TEST_TOKEN)

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(_ENV_DB_PATH, str(db_path))
        mp.setenv(_ENV_BIND_HOST, _LOOPBACK_HOST)

        # Defer import SAU khi env đã set — `create_app()` resolve env mỗi
        # lần gọi (không phải module-level side-effect), nên import ở đây
        # an toàn.
        from app.main import create_app  # noqa: PLC0415

        app = create_app()
        with TestClient(app) as client:
            yield _ApiHarness(
                client=client,
                job_manager=app.state.job_manager,
                tmp_dir=tmp_dir,
            )


def _inject_job(
    job_manager,
    job_id: str,
    status: JobStatus,
    artifact_path: str | None,
) -> None:
    """Tạo `_JobRecord` với `status`/`artifact_path` yêu cầu và thêm vào
    `job_manager._jobs`.

    KHÔNG gọi `submit_batch()` — property này chỉ test contract route
    `GET /api/jobs/{id}/qr.png` khi state đã ở trạng thái nhất định,
    KHÔNG quan tâm cách nào tới được state đó. `submit_batch` sẽ spawn
    scheduler chạy handler thật `IdealFlowHandler.run()` gây flake vì phụ
    thuộc network (chatgpt.com/api.stripe.com).
    """
    job = Job(
        job_id=job_id,
        payment_method="ideal",
        account_line="user@example.com|pw|",
        created_at=time.time(),
        cancellation_token=SimpleCancellationToken(),
    )
    record = _JobRecord(
        job=job,
        status=status,
        updated_at=time.time(),
        artifact_path=artifact_path,
    )
    job_manager._jobs[job_id] = record  # noqa: SLF001


def _remove_job(job_manager, job_id: str) -> None:
    """Xóa record khỏi `_jobs` để cleanup sau mỗi example."""
    job_manager._jobs.pop(job_id, None)  # noqa: SLF001


# ---------------------------------------------------------------------------
# Hypothesis strategy — chọn ngẫu nhiên 1 trong 5 status hợp lệ HOẶC
# "missing" (job không tồn tại). QR_READY hiếm nhưng vẫn phải cover —
# `sampled_from` uniform 1/6 mỗi option để đảm bảo hypothesis chạm được
# cả nhánh 200 (success) lẫn 409 (qr_not_ready) trong 20 example.
# ---------------------------------------------------------------------------

_STATUS_CHOICES: list[str] = [s.value for s in JobStatus] + ["missing"]
_status_strategy = st.sampled_from(_STATUS_CHOICES)


@given(status_choice=_status_strategy)
def test_qr_endpoint_only_serves_png_on_qr_ready(
    api_client: _ApiHarness, status_choice: str
) -> None:
    """Validates: Requirements 7.5.

    Cover 3 nhánh của contract endpoint:
    - `status_choice == "missing"` → 404 `job_not_found`.
    - `status_choice == "qr_ready"` → 200 `image/png` + body PNG magic.
    - `status_choice ∈ {pending, running, error, stopped}` → 409 `qr_not_ready`.
    """
    job_id = uuid.uuid4().hex
    artifact_path: Path | None = None

    try:
        if status_choice == "missing":
            # Job không tồn tại — KHÔNG inject vào JobManager._jobs.
            response = api_client.client.get(
                f"/api/jobs/{job_id}/qr.png",
                headers={"X-Auth-Token": _TEST_TOKEN},
            )
            assert response.status_code == 404, (
                f"Job không tồn tại phải trả 404, nhận {response.status_code}: "
                f"{response.text}"
            )
            body = response.json()
            assert body.get("error_code") == "job_not_found", (
                f"Body thiếu error_code=job_not_found: {body}"
            )
            return

        job_status = JobStatus(status_choice)

        if job_status == JobStatus.QR_READY:
            # Ghi file stub PNG với magic bytes chuẩn → route stream nguyên
            # xi qua FileResponse. artifact_path unique theo job_id nên
            # cleanup sau mỗi example không đụng file example khác.
            artifact_path = api_client.tmp_dir / f"{job_id}.png"
            artifact_path.write_bytes(_STUB_PNG_PAYLOAD)
            _inject_job(
                api_client.job_manager,
                job_id,
                job_status,
                str(artifact_path),
            )
            response = api_client.client.get(
                f"/api/jobs/{job_id}/qr.png",
                headers={"X-Auth-Token": _TEST_TOKEN},
            )
            assert response.status_code == 200, (
                f"Job QR_READY + file tồn tại phải trả 200, nhận "
                f"{response.status_code}: {response.text}"
            )
            assert response.headers["content-type"] == "image/png", (
                f"Content-Type phải image/png, nhận "
                f"{response.headers.get('content-type')!r}"
            )
            assert response.content.startswith(_PNG_MAGIC), (
                f"Body phải bắt đầu bằng PNG magic {_PNG_MAGIC!r}, "
                f"nhận {response.content[:8]!r}"
            )
        else:
            # Non-QR_READY: pending/running/error/stopped → 409 qr_not_ready.
            _inject_job(api_client.job_manager, job_id, job_status, None)
            response = api_client.client.get(
                f"/api/jobs/{job_id}/qr.png",
                headers={"X-Auth-Token": _TEST_TOKEN},
            )
            assert response.status_code == 409, (
                f"Job status={status_choice} phải trả 409, nhận "
                f"{response.status_code}: {response.text}"
            )
            body = response.json()
            assert body.get("error_code") == "qr_not_ready", (
                f"Body thiếu error_code=qr_not_ready: {body}"
            )
            assert body.get("details", {}).get("status") == status_choice, (
                f"details.status phải là {status_choice!r}, nhận {body}"
            )
    finally:
        # Cleanup: xóa record + file stub để example kế tiếp bắt đầu sạch.
        _remove_job(api_client.job_manager, job_id)
        if artifact_path is not None and artifact_path.exists():
            artifact_path.unlink()
