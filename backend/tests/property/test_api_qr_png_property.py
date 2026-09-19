"""Property test cho `GET /api/jobs/{job_id}/qr.png` — Property 22 (task 23.2).

**Property 22: API lấy QR chỉ trả ảnh hợp lệ khi job ở đúng trạng thái
`qr_ready`.**

Với mọi `job_id` được sinh ngẫu nhiên KẾT HỢP với 1 trạng thái bất kỳ thuộc
`Job_Status` hoặc trường hợp job không tồn tại, endpoint SHALL trả 200
`image/png` với body binary hợp lệ (magic bytes PNG) **KHI VÀ CHỈ KHI** 3
điều kiện cùng thoả:

1. Job tồn tại trong `JobManager._jobs`.
2. `record.status == JobStatus.QR_READY`.
3. `record.artifact_path` là chuỗi non-empty trỏ tới 1 file PNG tồn tại
   trên đĩa.

Mọi tổ hợp khác (job không tồn tại / status khác `qr_ready` / `qr_ready`
nhưng `artifact_path` rỗng / `qr_ready` nhưng file trên đĩa không tồn tại)
SHALL trả 4xx JSON với `error_code` ổn định (`job_not_found`,
`qr_not_ready`, `qr_artifact_missing`) — KHÔNG file rỗng, KHÔNG placeholder
image ngầm định, KHÔNG binary có content-type sai.

**Validates: Requirements 7.5**

Chiến lược test:

- 1 app FastAPI + `TestClient` được khởi tạo 1 lần cho module (fixture
  `api_env`, `scope="module"`) — giảm chi phí lifespan startup/shutdown
  cho từng hypothesis example.
- Mỗi hypothesis example TỰ reset `job_manager._jobs` (dict rỗng) trước
  khi inject record giả tương ứng với "kịch bản" mà strategy sinh ra —
  đảm bảo state không leak giữa các example.
- File PNG hợp lệ dùng cho scenario `qr_ready_valid` được render 1 lần
  qua `QrRenderer.render_png(...)` (fixture-scope) và tái sử dụng cho
  toàn bộ property — nội dung không phụ thuộc hypothesis input, chỉ đóng
  vai trò "file thực sự tồn tại trên đĩa".
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.job_manager import _JobRecord
from app.core.payment_flow import Job, JobStatus, SimpleCancellationToken
from app.payments.ideal.qr_renderer import QrRenderer


# ---------------------------------------------------------------------------
# Hằng số dùng chung
# ---------------------------------------------------------------------------

_ENV_DB_PATH = "IDEAL_QR_TOOL_DB_PATH"
_ENV_BIND_HOST = "IDEAL_QR_TOOL_BIND_HOST"

#: Auth token seed sẵn vào SQLite trước khi startup hydrate — mọi request
#: trong property test phải kèm `X-Auth-Token` bằng giá trị này.
_AUTH_TOKEN = "prop22-auth-token"

#: PNG magic bytes (ISO/IEC 15948) — 8 byte đầu file PNG hợp lệ. Property
#: assert body binary bắt đầu bằng chuỗi này khi status_code=200.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: Toàn bộ trạng thái `Job_Status` KHÁC `QR_READY` — dùng cho scenario
#: "wrong_status" để verify hợp đồng "chỉ QR_READY mới cho phép trả PNG".
_NON_QR_READY_STATUSES = tuple(s for s in JobStatus if s != JobStatus.QR_READY)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_fake_record(
    job_id: str,
    status: JobStatus,
    artifact_path: str | None,
) -> _JobRecord:
    """Build 1 `_JobRecord` giả để inject trực tiếp vào `JobManager._jobs`.

    KHÔNG chạy handler thực — chỉ cần route đọc được `record.status` và
    `record.artifact_path` để quyết định nhánh response. Vì `Job` là frozen
    dataclass, phải build Job với đúng `job_id` ngay từ đầu (không thể sửa
    sau).
    """
    job = Job(
        job_id=job_id,
        payment_method="ideal",
        account_line="probe@example.com|placeholder-access-token",
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


@pytest.fixture(scope="module")
def api_env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """Khởi tạo 1 lần cho toàn module: DB tạm + PNG fixture + TestClient.

    Vòng đời:
    1. Tạo `tmp_root` riêng cho module này qua `tmp_path_factory`.
    2. Seed `web.auth_token` vào SQLite trước khi `create_app()` chạy.
    3. Render 1 file PNG thật vào `qr_dir` qua `QrRenderer` — dùng cho
       scenario `qr_ready_valid`, đảm bảo file trên đĩa hợp lệ (không phải
       byte string bịa ra bằng tay).
    4. Set 2 env bootstrap → `create_app()` → `TestClient(app)` với
       `with`-context để trigger lifespan startup/shutdown.
    5. Sau khi module kết thúc, khôi phục env cũ.
    """
    tmp_root = tmp_path_factory.mktemp("qr_prop_22")
    db_path = tmp_root / "test.db"
    qr_dir = tmp_root / "qr"

    # 1) Render 1 file PNG thật (auth đã bỏ — không cần seed token). (deeplink giả lập; nội dung deeplink không
    #    ảnh hưởng — QR bytes hợp lệ về mặt format là điều kiện đủ).
    renderer = QrRenderer()
    valid_png_path: Path = renderer.render_png(
        deeplink="https://issuer.example/ideal/deeplink?probe=prop22",
        job_id="prop22_valid_fixture",
        output_dir=qr_dir,
    )

    # 3) Set env bootstrap. Dùng `os.environ` (không phải monkeypatch, vì
    #    monkeypatch là function-scoped mà fixture này là module-scoped);
    #    thủ công backup/restore.
    prev_db = os.environ.get(_ENV_DB_PATH)
    prev_host = os.environ.get(_ENV_BIND_HOST)
    os.environ[_ENV_DB_PATH] = str(db_path)
    os.environ[_ENV_BIND_HOST] = "127.0.0.1"

    try:
        # Import factory SAU khi set env — `create_app()` là function, mỗi
        # lần gọi đều re-resolve env, không cache tại module level.
        from app.main import create_app  # noqa: PLC0415

        fastapi_app = create_app()
        with TestClient(fastapi_app) as client:
            yield {
                "client": client,
                "app": fastapi_app,
                "token": _AUTH_TOKEN,
                "valid_png_path": valid_png_path,
                "qr_dir": qr_dir,
            }
    finally:
        # Khôi phục env — tránh leak sang module test khác chạy sau.
        for key, prev in ((_ENV_DB_PATH, prev_db), (_ENV_BIND_HOST, prev_host)):
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev


# ---------------------------------------------------------------------------
# Hypothesis strategy — 5 kịch bản cover toàn bộ input space của Property 22.
# ---------------------------------------------------------------------------


@st.composite
def _scenario_strategy(draw: st.DrawFn) -> dict[str, Any]:
    """Sinh 1 kịch bản `job_id` × trạng thái cho endpoint qr.png.

    Chiến lược sinh:
    - `nonexistent`: `job_id` bất kỳ, KHÔNG inject vào `_jobs` → route trả
      `job_not_found` (404).
    - `wrong_status`: inject 1 record với status ∈ {PENDING, RUNNING, ERROR,
      STOPPED} (không QR_READY) → route trả `qr_not_ready` (409).
    - `qr_ready_valid`: inject record `QR_READY` với `artifact_path` trỏ
      tới file PNG THẬT trên đĩa → route trả PNG binary (200).
    - `qr_ready_missing_path`: inject `QR_READY` với `artifact_path=None`
      → route trả `qr_artifact_missing` (409). Đây là trạng thái không
      nhất quán mà route phải fail-safe.
    - `qr_ready_missing_file`: inject `QR_READY` với `artifact_path` trỏ
      tới file KHÔNG tồn tại trên đĩa → route trả `qr_artifact_missing`
      (404). Cũng là trạng thái không nhất quán.

    `job_id` được sinh bằng `st.text` giới hạn ký tự URL-safe để tránh
    hypothesis sinh chuỗi phá URL routing (đường dẫn cần path param an
    toàn — dấu `/` sẽ break route matching).
    """
    kind = draw(
        st.sampled_from(
            [
                "nonexistent",
                "wrong_status",
                "qr_ready_valid",
                "qr_ready_missing_path",
                "qr_ready_missing_file",
            ]
        )
    )
    # `job_id` URL-safe: chữ + số + `-` + `_`. Cover chuỗi ngắn/dài (uuid-
    # like hoặc số ít ký tự) để đại diện "job_id bất kỳ" theo yêu cầu
    # property test.
    job_id = draw(
        st.text(
            alphabet=st.characters(
                whitelist_categories=("Lu", "Ll", "Nd"),
                whitelist_characters="-_",
            ),
            min_size=1,
            max_size=32,
        )
    )
    scenario: dict[str, Any] = {"kind": kind, "job_id": job_id}
    if kind == "wrong_status":
        scenario["status"] = draw(st.sampled_from(_NON_QR_READY_STATUSES))
    return scenario


# ---------------------------------------------------------------------------
# Property 22 test
# ---------------------------------------------------------------------------


@settings(
    # 15 example đủ cover 5 kịch bản × biến job_id — mục tiêu là "chạy
    # nhanh, không stuck". Property đơn giản (equivalence), không cần
    # exhaustive search.
    max_examples=15,
    deadline=None,
    # Fixture `api_env` module-scoped, không phải function-scoped — nhưng
    # hypothesis vẫn có thể cảnh báo nếu profile khác kích hoạt check này.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(scenario=_scenario_strategy())
def test_qr_png_returns_valid_image_iff_qr_ready_with_file(
    api_env: dict[str, Any], scenario: dict[str, Any]
) -> None:
    """Property 22: 200 image/png IFF (QR_READY ∧ artifact_path ∧ file tồn tại).

    Kiểm chứng equivalence 2 chiều:
    - Chiều thuận: khi 3 điều kiện đủ → response 200 với content-type
      `image/png` và body bắt đầu bằng PNG magic bytes.
    - Chiều nghịch: mọi trạng thái/tình huống khác → response 4xx JSON
      với `error_code` cụ thể theo hợp đồng. KHÔNG bao giờ trả 200 với
      content-type khác image/png, KHÔNG file rỗng, KHÔNG placeholder.

    **Validates: Requirements 7.5**
    """
    client: TestClient = api_env["client"]
    fastapi_app = api_env["app"]
    valid_png_path: Path = api_env["valid_png_path"]
    qr_dir: Path = api_env["qr_dir"]

    job_manager = fastapi_app.state.job_manager

    # Reset state trước MỖI example — job_manager là shared module-scoped
    # instance. Không có job nào từng submit qua `submit_batch` nên
    # `_pending_order` luôn rỗng, `_jobs.clear()` là đủ để cô lập example.
    job_manager._jobs.clear()
    job_manager._pending_order.clear()

    kind = scenario["kind"]
    job_id = scenario["job_id"]

    # Setup state theo kịch bản.
    if kind == "nonexistent":
        pass  # không inject gì cả → get_job(job_id) trả None
    elif kind == "wrong_status":
        record = _make_fake_record(
            job_id=job_id,
            status=scenario["status"],
            artifact_path=None,
        )
        job_manager._jobs[job_id] = record
    elif kind == "qr_ready_valid":
        record = _make_fake_record(
            job_id=job_id,
            status=JobStatus.QR_READY,
            artifact_path=str(valid_png_path),
        )
        job_manager._jobs[job_id] = record
    elif kind == "qr_ready_missing_path":
        record = _make_fake_record(
            job_id=job_id,
            status=JobStatus.QR_READY,
            artifact_path=None,
        )
        job_manager._jobs[job_id] = record
    elif kind == "qr_ready_missing_file":
        # `artifact_path` trỏ tới file KHÔNG tồn tại trên đĩa — dùng path
        # bên trong `qr_dir` để không đụng file thật.
        missing_path = qr_dir / f"missing_{job_id or 'blank'}.png"
        assert not missing_path.exists(), (
            "Fixture cleanup có vấn đề — file 'missing' đã tồn tại từ trước"
        )
        record = _make_fake_record(
            job_id=job_id,
            status=JobStatus.QR_READY,
            artifact_path=str(missing_path),
        )
        job_manager._jobs[job_id] = record
    else:  # pragma: no cover — strategy chỉ sinh 5 giá trị trên
        raise AssertionError(f"Kịch bản chưa hỗ trợ: {kind!r}")

    # Gọi endpoint — luôn kèm auth token hợp lệ; nếu KHÔNG đúng token thì
    # dependency auth raise 401 và cover property khác (Property 43).
    resp = client.get(
        f"/api/jobs/{job_id}/qr.png",
        headers={"X-Auth-Token": _AUTH_TOKEN},
    )

    # Property assertion — split theo chiều.
    if kind == "qr_ready_valid":
        # Chiều thuận: 200 image/png binary hợp lệ.
        assert resp.status_code == 200, (
            f"[Property 22 vi phạm] `qr_ready + file tồn tại` phải trả 200; "
            f"nhận status={resp.status_code}, body[:80]={resp.content[:80]!r}"
        )
        content_type = resp.headers.get("content-type", "")
        assert content_type == "image/png", (
            f"[Property 22 vi phạm] `qr_ready + file tồn tại` phải trả "
            f"content-type='image/png'; nhận {content_type!r}"
        )
        body = resp.content
        assert body.startswith(_PNG_MAGIC), (
            f"[Property 22 vi phạm] body 200 không bắt đầu bằng PNG magic; "
            f"prefix={body[:8]!r}"
        )
        assert len(body) == valid_png_path.stat().st_size, (
            f"[Property 22 vi phạm] body length {len(body)} khác kích thước "
            f"file thực {valid_png_path.stat().st_size} — file bị truncate."
        )
    else:
        # Chiều nghịch: 4xx JSON với error_code cụ thể.
        assert resp.status_code in (404, 409), (
            f"[Property 22 vi phạm] kịch bản {kind!r} phải trả 404/409; "
            f"nhận {resp.status_code}, body[:200]={resp.content[:200]!r}"
        )
        content_type = resp.headers.get("content-type", "")
        assert content_type.startswith("application/json"), (
            f"[Property 22 vi phạm] error response phải là JSON; "
            f"nhận content-type={content_type!r}"
        )
        # Body KHÔNG được là PNG (không placeholder image ngầm định).
        assert not resp.content.startswith(_PNG_MAGIC), (
            f"[Property 22 vi phạm] error response chứa PNG binary — vi "
            f"phạm 'không placeholder image ngầm định'."
        )
        payload = resp.json()
        error_code = payload.get("error_code")

        if kind == "nonexistent":
            assert resp.status_code == 404, (
                f"job không tồn tại phải trả 404; nhận {resp.status_code}"
            )
            assert error_code == "job_not_found", (
                f"job không tồn tại phải trả error_code='job_not_found'; "
                f"nhận {error_code!r}"
            )
        elif kind == "wrong_status":
            assert resp.status_code == 409, (
                f"status != qr_ready phải trả 409; nhận {resp.status_code}"
            )
            assert error_code == "qr_not_ready", (
                f"status != qr_ready phải trả error_code='qr_not_ready'; "
                f"nhận {error_code!r}"
            )
            expected_status_value = scenario["status"].value
            assert payload.get("details", {}).get("status") == expected_status_value, (
                f"details.status phải chứa trạng thái hiện tại "
                f"{expected_status_value!r}; nhận {payload!r}"
            )
        elif kind in ("qr_ready_missing_path", "qr_ready_missing_file"):
            assert error_code == "qr_artifact_missing", (
                f"kịch bản {kind!r} phải trả error_code='qr_artifact_missing'; "
                f"nhận {error_code!r}"
            )
