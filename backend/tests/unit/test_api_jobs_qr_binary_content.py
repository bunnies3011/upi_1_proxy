"""Unit test `GET /api/jobs/{id}/qr.png` (Task 23.3).

Requirements: 7.4.

Verify hợp đồng binary của endpoint trên đường "happy path":

- `response.headers["content-type"] == "image/png"` — KHÔNG có charset, KHÔNG
  base64 trong JSON (R7.4 yêu cầu binary trực tiếp).
- `response.content` bắt đầu bằng magic bytes `b"\\x89PNG"` — signature PNG
  chuẩn RFC 2083.
- `len(response.content) > 100` — QR PNG thật (render bởi `qrcode` từ 1
  deeplink) luôn > 100 bytes (thực tế 200+ bytes cho QR nhỏ nhất).

Test cover 1 case cụ thể (không hypothesis) — Task 23.2 (property test) đã
cover space status × missing job. Ở đây chỉ chắc chắn khi mọi thứ hợp lệ,
endpoint trả binary PNG THẬT do `qrcode` render, KHÔNG phải stub bytes.

Setup:
- Seed `web.auth_token` vào SQLite trước khi `create_app()` để lifespan
  startup không raise `StartupSecurityError`.
- `TestClient(app)` context manager để trigger lifespan startup + shutdown
  đầy đủ (bootstrap_services + configure_services + close DB).
- Inject 1 `_JobRecord` status=QR_READY vào `JobManager._jobs` với
  `artifact_path` trỏ tới file QR PNG thật render trước bằng `qrcode`.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

import pytest
import qrcode
from fastapi.testclient import TestClient

from app.core.job_manager import _JobRecord
from app.core.payment_flow import Job, JobStatus, SimpleCancellationToken


# ---------------------------------------------------------------------------
# Hằng số bootstrap env + auth token.
# ---------------------------------------------------------------------------

_ENV_DB_PATH = "IDEAL_QR_TOOL_DB_PATH"
_ENV_BIND_HOST = "IDEAL_QR_TOOL_BIND_HOST"
_LOOPBACK_HOST = "127.0.0.1"
_TEST_TOKEN = "task23_3-binary-content-token"

# Deeplink test cố định — mọi deeplink hợp lệ đều render thành PNG > 100
# bytes; giữ nguyên URL này để có thể debug nội dung khi test fail.
_TEST_DEEPLINK = "https://pay.ideal.nl/deeplink?token=task-23-3-binary-content"

_PNG_MAGIC = b"\x89PNG"

# Schema `settings` khớp `app/core/db.py::_CREATE_SETTINGS_TABLE`.
_SEED_SETTINGS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


def _seed_auth_token(db_path: Path, token: str) -> None:
    """Ghi `web.auth_token` vào SQLite qua sqlite3 sync trước khi aiosqlite
    của DbEngine mở lại trong lifespan startup. Value = JSON-encoded string
    (khớp `SettingsRepository.set` — `json.dumps(value)`).
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


def _render_deeplink_png(path: Path, deeplink: str) -> None:
    """Render QR PNG thật từ `deeplink` bằng thư viện `qrcode` (đã có trong
    `pyproject.toml` deps). Ảnh thuần không overlay — khớp R7.1.
    """
    img = qrcode.make(deeplink)
    img.save(str(path))


def test_qr_endpoint_returns_image_png_content_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validates: Requirements 7.4.

    Happy path: job QR_READY + file PNG thật trên đĩa → 200 với
    `content-type: image/png`, body binary bắt đầu bằng PNG magic bytes
    và có kích thước > 100 bytes (QR thật, không phải stub).
    """
    # 1) Set env bootstrap trước khi `create_app()`.
    db_path = tmp_path / "test.db"
    _seed_auth_token(db_path, _TEST_TOKEN)
    monkeypatch.setenv(_ENV_DB_PATH, str(db_path))
    monkeypatch.setenv(_ENV_BIND_HOST, _LOOPBACK_HOST)

    # 2) Render 1 QR PNG THẬT từ deeplink test — đây là điểm phân biệt với
    #    property test task 23.2 (dùng stub bytes chỉ có magic + padding).
    qr_path = tmp_path / "qr_task23_3.png"
    _render_deeplink_png(qr_path, _TEST_DEEPLINK)
    assert qr_path.stat().st_size > 100, (
        "Precondition: qrcode phải render PNG > 100 bytes cho deeplink "
        f"test — nhận {qr_path.stat().st_size} bytes"
    )

    # 3) Bootstrap app (defer import sau khi env đã set).
    from app.main import create_app  # noqa: PLC0415

    app = create_app()
    with TestClient(app) as client:
        # 4) Inject 1 job QR_READY + artifact_path trỏ tới file PNG thật.
        #    KHÔNG dùng `submit_batch()` vì scheduler sẽ chạy handler thật
        #    `IdealFlowHandler.run()` — phụ thuộc network và tốn thời gian
        #    không cần thiết cho unit test này.
        job_id = uuid.uuid4().hex
        job = Job(
            job_id=job_id,
            payment_method="ideal",
            account_line="user@example.com|pw|",
            created_at=time.time(),
            cancellation_token=SimpleCancellationToken(),
        )
        record = _JobRecord(
            job=job,
            status=JobStatus.QR_READY,
            updated_at=time.time(),
            artifact_path=str(qr_path),
        )
        app.state.job_manager._jobs[job_id] = record  # noqa: SLF001

        # 5) Gọi endpoint qua TestClient.
        response = client.get(
            f"/api/jobs/{job_id}/qr.png",
            headers={"X-Auth-Token": _TEST_TOKEN},
        )

    # 6) Assert hợp đồng binary theo R7.4.
    assert response.status_code == 200, (
        f"Endpoint trả {response.status_code}: {response.text}"
    )
    assert response.headers["content-type"] == "image/png", (
        f"Content-Type phải EXACT 'image/png' (không charset, không multipart), "
        f"nhận {response.headers.get('content-type')!r}"
    )
    assert response.content.startswith(_PNG_MAGIC), (
        f"Response body phải bắt đầu bằng PNG magic bytes {_PNG_MAGIC!r}, "
        f"nhận {response.content[:8]!r}"
    )
    assert len(response.content) > 100, (
        f"Response body chỉ có {len(response.content)} bytes — QR PNG thật "
        "phải > 100 bytes (nếu <= 100 nghĩa là file bị truncate hoặc "
        "trả stub/placeholder thay vì file thật)"
    )
