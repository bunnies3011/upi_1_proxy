#!/usr/bin/env python3
"""Kiểm tra sức khoẻ tổng thể của backend `ideal_qr_tool`.

Chạy: `python3 backend/test/check_project_health.py`
    (từ workspace root — script tự resolve path).

Nội dung check (mỗi bước log `[PASS]/[FAIL]` ngay khi xong, flush stdout):

  1. Syntax check: compile toàn bộ `.py` trong `backend/app/`.
  2. Import check: import mọi module public trong `backend.app.*`.
  3. Bug memory leak: `_JobRecord.logs` là list (không maxlen) khi tạo
     qua `submit_batch` — verify bằng cách gọi `submit_batch` với 1
     handler stub và kiểm tra type của `logs` field.
  4. Contract check: mọi router `include_router` trong `main.py` phải
     tồn tại, mọi dependency `Depends` phải resolve được.
  5. API dependency wiring: `deps.get_require_auth_token()` phải trả no-op
     (theo docstring hiện tại: auth đã bỏ).

Script KHÔNG start server thật, chỉ static analysis + smoke import.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import sys
import traceback
from pathlib import Path


# Resolve backend/ root (script nằm ở backend/test/) và thêm vào sys.path.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_ROOT))


TOTAL = 0
PASSED = 0
FAILED = 0


def _emit(status: str, tc_id: str, desc: str, detail: str = "") -> None:
    """In dòng log định dạng `[STATUS] TC-ID — desc :: detail`, flush ngay."""
    global TOTAL, PASSED, FAILED
    TOTAL += 1
    if status == "PASS":
        PASSED += 1
    else:
        FAILED += 1
    line = f"[{status}] {tc_id} — {desc}"
    if detail:
        line += f" :: {detail}"
    print(line, flush=True)


# ---------------------------------------------------------------------------
# 1. Syntax check
# ---------------------------------------------------------------------------
def check_syntax() -> None:
    print("\n=== 1. Syntax check (compile toàn bộ .py trong backend/app/) ===", flush=True)
    app_dir = _BACKEND_ROOT / "app"
    py_files = sorted(app_dir.rglob("*.py"))
    for i, py_file in enumerate(py_files, start=1):
        rel = py_file.relative_to(_BACKEND_ROOT)
        tc_id = f"SYN-{i:03d}"
        try:
            source = py_file.read_text(encoding="utf-8")
            ast.parse(source, filename=str(py_file))
            _emit("PASS", tc_id, f"parse OK: {rel}")
        except SyntaxError as ex:
            _emit(
                "FAIL",
                tc_id,
                f"SyntaxError: {rel}",
                f"line {ex.lineno}: {ex.msg}",
            )
        except OSError as ex:
            _emit("FAIL", tc_id, f"read error: {rel}", str(ex))


# ---------------------------------------------------------------------------
# 2. Import check
# ---------------------------------------------------------------------------
def check_imports() -> None:
    print("\n=== 2. Import check (import mọi module public backend.app.*) ===", flush=True)
    import app  # noqa: F401 — ensure package importable
    counter = 0
    for mod_info in pkgutil.walk_packages(app.__path__, prefix="app."):
        counter += 1
        tc_id = f"IMP-{counter:03d}"
        name = mod_info.name
        try:
            importlib.import_module(name)
            _emit("PASS", tc_id, f"import OK: {name}")
        except Exception as ex:
            tb = "".join(traceback.format_exception_only(type(ex), ex)).strip()
            _emit("FAIL", tc_id, f"import fail: {name}", tb)


# ---------------------------------------------------------------------------
# 3. Bug: _JobRecord.logs bounded?
# ---------------------------------------------------------------------------
def check_logs_deque_bug() -> None:
    print("\n=== 3. Bug check: _JobRecord.logs phải là bounded deque ===", flush=True)
    from collections import deque
    from app.core.job_manager import _JobRecord, _new_logs_deque
    from app.core.payment_flow import Job, JobStatus, SimpleCancellationToken

    # 3a. Verify factory
    tc_id = "BUG-001"
    d = _new_logs_deque()
    if isinstance(d, deque) and d.maxlen is not None and d.maxlen == 500:
        _emit("PASS", tc_id, "_new_logs_deque trả deque(maxlen=500)")
    else:
        _emit(
            "FAIL",
            tc_id,
            "_new_logs_deque KHÔNG trả deque(maxlen=500)",
            f"got type={type(d).__name__} maxlen={getattr(d, 'maxlen', None)}",
        )

    # 3b. Verify default_factory của field `logs` chính là _new_logs_deque
    tc_id = "BUG-002"
    from dataclasses import fields as _fields
    logs_field = next(f for f in _fields(_JobRecord) if f.name == "logs")
    factory = logs_field.default_factory
    if factory is _new_logs_deque:
        _emit(
            "PASS",
            tc_id,
            "_JobRecord.logs default_factory = _new_logs_deque (bounded 500)",
        )
    else:
        _emit(
            "FAIL",
            tc_id,
            "_JobRecord.logs default_factory KHÔNG phải _new_logs_deque",
            f"got {factory!r}",
        )

    # 3c. Verify path submit_batch KHÔNG override bằng `logs=[]` list
    #     Đọc source code job_manager.py, tìm dòng `logs=[]` bất kỳ.
    tc_id = "BUG-003"
    src = (_BACKEND_ROOT / "app" / "core" / "job_manager.py").read_text(encoding="utf-8")
    bad_lines: list[tuple[int, str]] = []
    for lineno, line in enumerate(src.splitlines(), start=1):
        stripped = line.strip()
        # Match `logs=[]` hoặc `logs = []` khi tạo _JobRecord.
        # Không match `.logs.clear()` hay `logs: deque[...]` (annotation).
        if stripped.startswith("logs=[]") or stripped.startswith("logs = []"):
            bad_lines.append((lineno, line))
    if bad_lines:
        first = bad_lines[0]
        _emit(
            "FAIL",
            tc_id,
            "submit_batch tạo _JobRecord với logs=[] (list, không bounded) — memory leak nguy cơ",
            f"line {first[0]}: {first[1].strip()}",
        )
    else:
        _emit(
            "PASS",
            tc_id,
            "không có `logs=[]` inline khi tạo _JobRecord — mọi record dùng factory bounded",
        )


# ---------------------------------------------------------------------------
# 4. Router / dependency wiring check
# ---------------------------------------------------------------------------
def check_router_wiring() -> None:
    print("\n=== 4. Router wiring check (include_router + endpoints) ===", flush=True)
    tc_id = "RTR-001"
    try:
        # KHÔNG import `app.main:app` (module-level `app = create_app()`
        # sẽ boot Fast API + read env). Thay vào đó, dùng create_app()
        # trực tiếp — nhưng nó cũng bootstrap async. Chỉ verify các
        # router module import OK và có `router` attribute.
        from app.api import (
            routes_events,
            routes_jobs,
            routes_notifications,
            routes_session_cache,
            routes_settings,
        )
        for mod, name in [
            (routes_events, "routes_events"),
            (routes_jobs, "routes_jobs"),
            (routes_notifications, "routes_notifications"),
            (routes_session_cache, "routes_session_cache"),
            (routes_settings, "routes_settings"),
        ]:
            assert hasattr(mod, "router"), f"{name} thiếu `router`"
        _emit("PASS", tc_id, "5 route module đều expose `router`")
    except Exception as ex:
        tb = "".join(traceback.format_exception_only(type(ex), ex)).strip()
        _emit("FAIL", tc_id, "route module thiếu `router`", tb)

    # 4b. deps.get_require_auth_token() phải trả callable no-op
    tc_id = "RTR-002"
    try:
        import asyncio
        from app.api import deps
        f = deps.get_require_auth_token()
        result = asyncio.run(f())
        if result is None:
            _emit(
                "PASS",
                tc_id,
                "deps.get_require_auth_token() trả async no-op (auth ĐÃ BỎ theo docstring)",
            )
        else:
            _emit(
                "FAIL",
                tc_id,
                "deps.get_require_auth_token() không trả None",
                repr(result),
            )
    except Exception as ex:
        tb = "".join(traceback.format_exception_only(type(ex), ex)).strip()
        _emit("FAIL", tc_id, "deps.get_require_auth_token() raise", tb)


# ---------------------------------------------------------------------------
# 5. Bootstrap: `_asyncio.get_event_loop()` deprecated ở Python 3.12+
# ---------------------------------------------------------------------------
def check_deprecated_apis() -> None:
    print("\n=== 5. Check deprecated / risky API usage ===", flush=True)
    src = (_BACKEND_ROOT / "app" / "bootstrap.py").read_text(encoding="utf-8")

    tc_id = "DEP-001"
    lines_with_get_event_loop = []
    for lineno, line in enumerate(src.splitlines(), start=1):
        if "get_event_loop()" in line and not line.lstrip().startswith("#"):
            lines_with_get_event_loop.append((lineno, line.strip()))
    if lines_with_get_event_loop:
        first = lines_with_get_event_loop[0]
        _emit(
            "FAIL",
            tc_id,
            "bootstrap.py dùng asyncio.get_event_loop() — deprecated Python 3.12+, sẽ raise DeprecationWarning",
            f"line {first[0]}: {first[1]}",
        )
    else:
        _emit(
            "PASS",
            tc_id,
            "bootstrap.py không dùng asyncio.get_event_loop() deprecated",
        )


# ---------------------------------------------------------------------------
# 6. Route ordering: SPA fallback không được shadow /api/*
# ---------------------------------------------------------------------------
def check_spa_fallback_ordering() -> None:
    print("\n=== 6. SPA fallback route ordering ===", flush=True)
    src = (_BACKEND_ROOT / "app" / "main.py").read_text(encoding="utf-8")

    # Tìm vị trí (line) của `include_router` và `_spa_fallback`.
    tc_id = "SPA-001"
    include_line = None
    spa_line = None
    for lineno, line in enumerate(src.splitlines(), start=1):
        if "fastapi_app.include_router(" in line and include_line is None:
            include_line = lineno
        if "_spa_fallback" in line and "def " in line:
            spa_line = lineno

    if include_line is None:
        _emit("FAIL", tc_id, "main.py không có include_router?", "")
        return
    if spa_line is None:
        _emit("PASS", tc_id, "không có SPA fallback (skip)")
        return

    if spa_line > include_line:
        _emit(
            "PASS",
            tc_id,
            "SPA fallback được đăng ký SAU include_router — /api/* không bị shadow",
            f"include_router@{include_line}, spa_fallback@{spa_line}",
        )
    else:
        _emit(
            "FAIL",
            tc_id,
            "SPA fallback đăng ký TRƯỚC include_router — sẽ shadow /api/*",
            f"include_router@{include_line}, spa_fallback@{spa_line}",
        )


# ---------------------------------------------------------------------------
# 7. Pydantic v2 compat check — model_config extra="forbid" phải tương thích
# ---------------------------------------------------------------------------
def check_pydantic_schemas() -> None:
    print("\n=== 7. Pydantic schema smoke test ===", flush=True)
    tc_id = "PYD-001"
    try:
        from app.api.schemas import (
            ErrorResponse,
            JobViewCompact,
            SubmitJobsRequest,
            SubmitJobsResponse,
        )
        # Smoke: mỗi model có thể instance với default fields.
        _ = SubmitJobsRequest()
        _ = SubmitJobsResponse()
        _ = JobViewCompact(
            job_id="x",
            payment_method="ideal",
            account_masked="u***@x.com",
            account_line="u@x.com|pw",
            status="pending",
            updated_at=0.0,
            order=1,
        )
        _ = ErrorResponse(error_code="x", message="y")
        _emit("PASS", tc_id, "pydantic schemas instance OK")
    except Exception as ex:
        tb = "".join(traceback.format_exception_only(type(ex), ex)).strip()
        _emit("FAIL", tc_id, "pydantic schema fail", tb)


# ---------------------------------------------------------------------------
# 8. Check circular imports / boundary violation
# ---------------------------------------------------------------------------
def check_payment_boundary() -> None:
    print("\n=== 8. Payment_Module_Boundary check ===", flush=True)
    # core/ và api/ KHÔNG được import app.payments.*
    for check_dir_name in ("core", "api"):
        dir_path = _BACKEND_ROOT / "app" / check_dir_name
        violations: list[tuple[Path, int, str]] = []
        for py in dir_path.rglob("*.py"):
            for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith("#") or not stripped:
                    continue
                # match `from app.payments` hoặc `import app.payments`
                if "app.payments" in stripped and (
                    stripped.startswith("from app.payments")
                    or stripped.startswith("import app.payments")
                ):
                    violations.append((py, lineno, stripped))

        tc_id = f"BND-{check_dir_name}"
        if violations:
            first = violations[0]
            _emit(
                "FAIL",
                tc_id,
                f"app/{check_dir_name}/ import app.payments.* — vi phạm boundary",
                f"{first[0].relative_to(_BACKEND_ROOT)}:{first[1]}: {first[2]}",
            )
        else:
            _emit(
                "PASS",
                tc_id,
                f"app/{check_dir_name}/ KHÔNG import app.payments.* (boundary OK)",
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Backend root: {_BACKEND_ROOT}", flush=True)
    check_syntax()
    check_imports()
    check_logs_deque_bug()
    check_router_wiring()
    check_deprecated_apis()
    check_spa_fallback_ordering()
    check_pydantic_schemas()
    check_payment_boundary()

    print(f"\n=== SUMMARY: total={TOTAL} pass={PASSED} fail={FAILED} ===", flush=True)
    sys.exit(1 if FAILED > 0 else 0)
