"""Task 26.2: verify startup Backend_Service đọc config từ SQLite, không JSON/YAML.

Requirement 11.1: Settings_Store (SQLite) là single source of truth cho runtime
config. Backend_Service KHÔNG được đọc bất kỳ file JSON/YAML nào để hydrate cấu
hình khi khởi động — mọi giá trị phải đi qua `SettingsRepository`.

Chiến lược:

    1. Set 2 env var bootstrap được phép (`IDEAL_QR_TOOL_DB_PATH`,
       `IDEAL_QR_TOOL_BIND_HOST`) — chỉ 2 giá trị này là chicken-and-egg,
       không thể lưu trong DB (main.py docstring giải thích).

    2. Monkey-patch `builtins.open` + `io.open` để ghi lại mọi lượt mở file
       cấp Python (aiosqlite/sqlite3 đi qua C-level fd nên không lộ ra —
       đúng ý đồ: SQLite READ được bỏ qua, JSON/YAML READ sẽ bị bắt).
       Patch cả 2 vì `pathlib.Path.open()` gọi `io.open()` (không phải
       `builtins.open()`), còn code dùng bare `open(...)` mới đi qua
       `builtins.open`.

    3. Gọi `create_app()` rồi enter/exit `app.router.lifespan_context(app)`
       để trigger toàn bộ bootstrap I/O (schema init, apply_settings,
       register namespace/handler). API chuẩn FastAPI, không cần uvicorn.

    4. Lọc `read_paths`: chỉ FLAG file nằm trong scope project
       (`ideal_qr_tool/backend/`) có đuôi config (`.json`/`.yaml`/`.yml`/`.toml`),
       loại trừ:
         - `pyproject.toml` (packaging metadata, không phải runtime config).
         - Các subdir dependency/cache (`.venv`, `site-packages`,
           `.pytest_cache`, `.hypothesis`, `__pycache__`).
         - Runtime artefact (`runtime/session_cache`, `runtime/qr`) — hiện tại
           startup KHÔNG đọc, nhưng loại trừ defensively vì đây là dữ liệu
           runtime chứ không phải config.

Nếu ai đó thêm code đọc file config JSON/YAML để hydrate settings tại
startup, test này sẽ fail với danh sách file vi phạm — Fail_Fast theo đúng
Payment_Module_Boundary + R11.1.

_Requirements: 11.1_
"""

from __future__ import annotations

import builtins
import io
from pathlib import Path

import pytest
from fastapi import FastAPI


# ---------------------------------------------------------------------------
# Filter constants — quyết định file nào là "vi phạm R11.1".
# ---------------------------------------------------------------------------

#: Đuôi file config bị cấm dùng làm nguồn runtime config.
_CONFIG_EXTENSIONS: tuple[str, ...] = (".json", ".yaml", ".yml", ".toml")

#: Marker nhận diện file thuộc project (không phải dependency/venv).
#: Dùng dạng POSIX cho ổn định (Path.__str__ trên macOS là POSIX).
_PROJECT_MARKER: str = "ideal_qr_tool/backend/"

#: Whitelist substring cho phép bỏ qua (dependency, cache, venv, artefact runtime).
_WHITELIST_SUBSTRINGS: tuple[str, ...] = (
    "/.venv/",
    "/venv/",
    "/site-packages/",
    "/.pytest_cache/",
    "/.hypothesis/",
    "/__pycache__/",
    "/runtime/session_cache/",
    "/runtime/qr/",
)

#: File packaging metadata của Python — hatchling/hatch có thể đọc, không
#: phải runtime config (không hydrate `SettingsRepository`).
_PACKAGING_TOML_NAME: str = "pyproject.toml"


def _is_forbidden_config_read(raw_path: str) -> bool:
    """Trả về True nếu `raw_path` là file config project bị R11.1 cấm đọc.

    Điều kiện đủ để FLAG:
    - Có đuôi thuộc `_CONFIG_EXTENSIONS`.
    - Nằm trong scope project (`_PROJECT_MARKER`).
    - KHÔNG phải `pyproject.toml` (packaging metadata).
    - KHÔNG nằm trong subdir dependency/cache/artefact (`_WHITELIST_SUBSTRINGS`).
    """
    lower = raw_path.lower()
    if not any(lower.endswith(ext) for ext in _CONFIG_EXTENSIONS):
        return False
    if _PROJECT_MARKER not in raw_path:
        return False
    if any(sub in raw_path for sub in _WHITELIST_SUBSTRINGS):
        return False
    if Path(raw_path).name == _PACKAGING_TOML_NAME:
        return False
    return True


async def test_startup_does_not_read_project_config_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`create_app()` + lifespan startup KHÔNG được đọc file JSON/YAML/TOML project (R11.1).

    Validates: Requirement 11.1 — Backend_Service đọc toàn bộ runtime config
    từ Settings_Store (SQLite), không file JSON/YAML.
    """
    # 1) Set 2 env var bootstrap được phép (chicken-and-egg).
    db_path = tmp_path / "startup_probe.db"
    monkeypatch.setenv("IDEAL_QR_TOOL_DB_PATH", str(db_path))
    monkeypatch.setenv("IDEAL_QR_TOOL_BIND_HOST", "127.0.0.1")

    # 2) Wrap `builtins.open` + `io.open` để ghi log mọi lượt mở file cấp
    #    Python. Cần patch cả 2 vì `pathlib.Path.open()` dùng `io.open()`
    #    (không phải `builtins.open()`), còn `open(...)` bare dùng builtins.
    #    aiosqlite/sqlite3 đi qua C-level fd nên KHÔNG đi qua 2 hàm này —
    #    đúng ý đồ (SQLite được bỏ qua, chỉ bắt Python-level JSON/YAML open).
    read_paths: list[str] = []
    original_builtins_open = builtins.open
    original_io_open = io.open

    def _track_builtins_open(file, *args, **kwargs):
        read_paths.append(str(file))
        return original_builtins_open(file, *args, **kwargs)

    def _track_io_open(file, *args, **kwargs):
        read_paths.append(str(file))
        return original_io_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _track_builtins_open)
    monkeypatch.setattr(io, "open", _track_io_open)

    # 3) Import factory SAU khi set env (module-level `app = create_app()`
    #    trong main.py chỉ build FastAPI object, KHÔNG chạy lifespan I/O —
    #    an toàn kể cả nếu module đã được cache từ test trước).
    from app.main import create_app  # noqa: PLC0415 — defer để env đã set

    app: FastAPI = create_app()

    # Trigger toàn bộ lifespan startup + shutdown qua API chuẩn FastAPI.
    # Enter → bootstrap_services chạy; exit → engine.close().
    async with app.router.lifespan_context(app):
        pass

    # 4) Lọc read_paths → chỉ giữ file vi phạm.
    violations = sorted({p for p in read_paths if _is_forbidden_config_read(p)})

    assert not violations, (
        "Startup đã đọc project config file — vi phạm R11.1 "
        "(mọi runtime config phải từ Settings_Store SQLite):\n"
        + "\n".join(f"  - {p}" for p in violations)
    )
