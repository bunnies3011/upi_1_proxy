"""Task 43.2: verify CLI KHÔNG load FastAPI HTTP stack.

Requirement 15.9: CLI process KHÔNG được import `fastapi.applications`,
`fastapi.routing`, `starlette.applications`, `uvicorn` hay bất kỳ module
nào bootstrap FastAPI app runtime. Test này chạy 1 subprocess sạch,
trong đó chỉ import `app.cli.main` + build parser, rồi assert
`sys.modules` không chứa các key runtime FastAPI/Starlette/Uvicorn.

Tại sao subprocess:
- pytest worker có thể đã import `fastapi.applications` từ test khác
  (integration test cho `app.api.*`) → `sys.modules` bị nhiễm → test
  false-negative nếu chạy trong process pytest.
- Subprocess mới với `sys.executable` giữ nguyên interpreter (cùng venv,
  cùng site-packages) nhưng có `sys.modules` sạch.

Tại sao KHÔNG dùng `python -c "..."`:
- Project rule cấm inline `python3 -c` để verify/debug (AGENTS.md +
  steering rules). Thay vào đó gọi probe file thật `_cli_import_probe.py`
  (leading underscore để pytest không collect).

_Requirements: 15.9_
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


# Danh sách module chỉ được load khi FastAPI HTTP runtime thực sự khởi tạo.
# Nếu bất kỳ module nào có mặt sau khi import `app.cli.main` → CLI đã kéo
# theo dependency HTTP → vi phạm Requirement 15.9.
#
# Note: `fastapi` bare package có thể xuất hiện gián tiếp qua tooling khác,
# nên ta target các submodule cụ thể của runtime chứ không phải bare
# package name.
_FASTAPI_RUNTIME_MODULES: tuple[str, ...] = (
    "fastapi.applications",   # FastAPI class (bootstrap app instance)
    "fastapi.routing",        # APIRouter (đăng ký endpoint)
    "starlette.applications", # Starlette base (FastAPI kế thừa)
    "starlette.routing",      # Starlette router
    "uvicorn",                # HTTP server runtime
)


_PROBE_PATH = Path(__file__).with_name("_cli_import_probe.py")


def test_cli_import_does_not_load_fastapi_runtime() -> None:
    """Verify import graph của CLI không kéo theo FastAPI/Starlette/Uvicorn.

    Chạy `_cli_import_probe.py` trong subprocess (Python cùng interpreter,
    cùng venv), truyền danh sách module cấm qua argv. Probe import
    `app.cli.main._build_parser()` rồi kiểm tra `sys.modules`.

    Contract của probe (xem docstring `_cli_import_probe.py`):
    - Exit 0 + stdout "CLEAN" → không leak.
    - Exit 1 + stdout "LEAKED:<modules>" → có leak.
    - Exit khác → probe tự lỗi, test fail với stderr đầy đủ.
    """
    assert _PROBE_PATH.exists(), f"probe file missing: {_PROBE_PATH}"

    result = subprocess.run(
        [sys.executable, str(_PROBE_PATH), *_FASTAPI_RUNTIME_MODULES],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    combined_output = (
        f"return_code={result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )

    # Nếu probe tự crash (import error, syntax error, missing dep) → fail
    # rõ ràng cho dev thấy stderr, tránh confuse với "CLI leak FastAPI".
    if result.returncode not in (0, 1):
        raise AssertionError(f"probe crashed:\n{combined_output}")

    assert result.returncode == 0, (
        "CLI import đang leak FastAPI HTTP runtime — vi phạm "
        f"Requirement 15.9:\n{combined_output}"
    )
    assert "CLEAN" in result.stdout, (
        f"probe không in CLEAN marker:\n{combined_output}"
    )
