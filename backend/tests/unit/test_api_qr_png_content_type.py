"""Unit test cho `GET /api/jobs/{id}/qr.png` — content-type + body binary (task 23.3).

_Requirements: 7.4_

Yêu cầu 7.4: Backend_Service SHALL cung cấp API endpoint trả TRỰC TIẾP nội
dung binary PNG theo `job_id`, KHÔNG encode base64 trong JSON response. Ba
kịch bản representative trong file này verify:

1. Job không tồn tại → 404 JSON `error_code=job_not_found` (content-type
   `application/json`).
2. Job `qr_ready` với artifact_path là file PNG THẬT trên đĩa → 200,
   content-type CHÍNH XÁC `image/png`, body là binary PNG (bắt đầu bằng
   PNG magic bytes ISO 15948) và có độ dài khớp file gốc — chứng minh
   endpoint stream binary trực tiếp, không base64/không truncate.
3. Job `running` (status non-terminal, không phải `qr_ready`) → 409 JSON
   `error_code=qr_not_ready`.

Bổ trợ cho property test (task 23.2, Property 22) — file này chốt các
"example điểm" cụ thể cho content-type và magic bytes, trong khi property
test cover không gian input rộng hơn.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.job_manager import _JobRecord
from app.core.payment_flow import Job, JobStatus, SimpleCancellationToken
from app.payments.ideal.qr_renderer import QrRenderer


# ---------------------------------------------------------------------------
# Hằng số
# ---------------------------------------------------------------------------

_ENV_DB_PATH = "IDEAL_QR_TOOL_DB_PATH"
_ENV_BIND_HOST = "IDEAL_QR_TOOL_BIND_HOST"

#: PNG magic bytes (ISO/IEC 15948) — 8 byte đầu file PNG hợp lệ. Kiểm
#: chứng body 200 thực sự là PNG binary, không phải JSON base64 lồng byte.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_record(
    job_id: str,
    status: JobStatus,
    artifact_path: str | None,
) -> _JobRecord:
    """Build `_JobRecord` giả để inject trực tiếp vào `JobManager._jobs`.

    `Job` là frozen dataclass → phải build với đúng `job_id` từ đầu.
    """
    job = Job(
        job_id=job_id,
        payment_method="ideal",
        account_line="unit@example.com|placeholder-access-token",
        created_at=time.time(),
        cancellation_token=SimpleCancellationToken(),
    )
    return _JobRecord(
        job=job,
        status=status,
        logs=[],
        lease=None,
        settings_snapshot={},
        updated_at=time.time(),
        handler_task=None,
        artifact_path=artifact_path,
    )


@pytest.fixture
def qr_api_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, Any]]:
    """Fresh app + `TestClient` cho MỖI test (isolation mạnh nhất).

    Sự khác biệt so với property test: 3 test unit này chạy nhanh và mỗi
    test cần state độc lập, nên fixture function-scoped (mặc định) là
    hợp lý — không cần chia sẻ app giữa các test.
    """
    db_path = tmp_path / "unit.db"
    qr_dir = tmp_path / "qr"

    # 1) Render 1 file PNG thật cho scenario "qr_ready".
    renderer = QrRenderer()
    valid_png_path: Path = renderer.render_png(
        deeplink="https://issuer.example/ideal/deeplink?unit-test=23.3",
        job_id="unit_23_3_valid_fixture",
        output_dir=qr_dir,
    )

    # 2) Set env bootstrap qua monkeypatch (function-scoped, auto-restore).
    monkeypatch.setenv(_ENV_DB_PATH, str(db_path))
    monkeypatch.setenv(_ENV_BIND_HOST, "127.0.0.1")

    # 3) Build app + TestClient (lifespan startup/shutdown qua `with`).
    from app.main import create_app  # noqa: PLC0415 — defer import cho env

    fastapi_app = create_app()
    with TestClient(fastapi_app) as client:
        yield {
            "client": client,
            "app": fastapi_app,
            "valid_png_path": valid_png_path,
            "qr_dir": qr_dir,
        }


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


def test_qr_png_returns_404_json_when_job_not_found(
    qr_api_client: dict[str, Any],
) -> None:
    """Job không tồn tại → 404 JSON `error_code=job_not_found`.

    Đây là contract R7.5 phần "không tồn tại" — verify content-type là
    JSON (không phải image/png) và body chứa error_code ổn định để
    Frontend_App phân loại lỗi.
    """
    client: TestClient = qr_api_client["client"]

    resp = client.get("/api/jobs/does-not-exist-anywhere/qr.png")

    assert resp.status_code == 404, (
        f"job không tồn tại phải trả 404, nhận {resp.status_code}: "
        f"body={resp.content[:200]!r}"
    )
    content_type = resp.headers.get("content-type", "")
    assert content_type.startswith("application/json"), (
        f"error response phải là JSON, nhận content-type={content_type!r}"
    )
    payload = resp.json()
    assert payload["error_code"] == "job_not_found", (
        f"error_code phải bằng 'job_not_found', nhận {payload.get('error_code')!r}"
    )
    assert payload["details"]["job_id"] == "does-not-exist-anywhere"


def test_qr_png_returns_image_png_binary_when_qr_ready(
    qr_api_client: dict[str, Any],
) -> None:
    """Job `qr_ready` + file PNG thật → 200 `image/png` binary hợp lệ.

    Kiểm chứng đúng Requirement 7.4 chuẩn hồi đáp:
    - `status_code == 200`.
    - `content-type == "image/png"` CHÍNH XÁC (không `application/json`,
      không thêm `; charset=...`).
    - Body là binary PNG THẬT: 8 byte đầu khớp PNG magic (`\\x89PNG\\r\\n\\x1a\\n`).
    - `len(body) == size của file trên đĩa` — chứng minh stream nguyên
      vẹn, không truncate, KHÔNG encode base64 (nếu base64 thì size sẽ
      lớn hơn ~4/3 lần).
    """
    client: TestClient = qr_api_client["client"]
    fastapi_app = qr_api_client["app"]
    valid_png_path: Path = qr_api_client["valid_png_path"]

    job_id = "unit-qr-ready-happy-path"
    record = _make_fake_record(
        job_id=job_id,
        status=JobStatus.QR_READY,
        artifact_path=str(valid_png_path),
    )
    fastapi_app.state.job_manager._jobs[job_id] = record

    resp = client.get(f"/api/jobs/{job_id}/qr.png")

    assert resp.status_code == 200, (
        f"qr_ready + file thật phải trả 200, nhận {resp.status_code}: "
        f"body[:200]={resp.content[:200]!r}"
    )
    content_type = resp.headers.get("content-type", "")
    assert content_type == "image/png", (
        f"content-type phải CHÍNH XÁC 'image/png', nhận {content_type!r}"
    )
    body = resp.content
    assert body.startswith(_PNG_MAGIC), (
        f"body 200 phải bắt đầu bằng PNG magic bytes; nhận prefix={body[:8]!r}"
    )
    expected_size = valid_png_path.stat().st_size
    assert len(body) == expected_size, (
        f"body length {len(body)} khác kích thước file thực {expected_size} "
        f"— endpoint đang truncate hoặc encode lại thay vì stream binary."
    )


def test_qr_png_returns_409_json_when_status_not_qr_ready(
    qr_api_client: dict[str, Any],
) -> None:
    """Job `running` (non-terminal, ≠ qr_ready) → 409 JSON `qr_not_ready`.

    Contract R7.5 phần "chưa đạt qr_ready": Frontend_App cần phân biệt
    "job đang chạy" với "job không tồn tại" (case 404) và "qr sẵn sàng"
    (case 200). `qr_not_ready` là error_code ổn định giữa mọi status
    non-terminal (pending/running/error/stopped).
    """
    client: TestClient = qr_api_client["client"]
    fastapi_app = qr_api_client["app"]

    job_id = "unit-running-not-ready"
    record = _make_fake_record(
        job_id=job_id,
        status=JobStatus.RUNNING,
        artifact_path=None,
    )
    fastapi_app.state.job_manager._jobs[job_id] = record

    resp = client.get(f"/api/jobs/{job_id}/qr.png")

    assert resp.status_code == 409, (
        f"status != qr_ready phải trả 409, nhận {resp.status_code}"
    )
    content_type = resp.headers.get("content-type", "")
    assert content_type.startswith("application/json"), (
        f"error response phải là JSON, nhận content-type={content_type!r}"
    )
    payload = resp.json()
    assert payload["error_code"] == "qr_not_ready", (
        f"error_code phải bằng 'qr_not_ready', nhận {payload.get('error_code')!r}"
    )
    assert payload["details"]["job_id"] == job_id
    assert payload["details"]["status"] == "running", (
        f"details.status phải phản ánh trạng thái hiện tại 'running', "
        f"nhận {payload['details'].get('status')!r}"
    )
