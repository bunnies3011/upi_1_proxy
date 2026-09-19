"""Task 26.2: Unit test startup Backend_Service đọc config từ SQLite, không JSON/YAML.

Requirement 11.1: Settings_Store (SQLite) là nguồn cấu hình duy nhất cho toàn
bộ runtime của Backend_Service. `create_app()` + lifespan startup CHỈ được
đọc từ SQLite (qua `SettingsRepository`); KHÔNG được đọc bất kỳ file JSON/
YAML nào khác để hydrate config.

Bộ test này bổ sung cho `test_startup_config_sqlite_only.py` (bắt Python-level
`open()` trên file config project) bằng 3 kiểm chứng bổ trợ:

    1. `create_app()` + `TestClient` trigger lifespan → `app.state` phải
       chứa singleton core (`SettingsRepository`, `DbEngine`, `db_path`)
       hydrate từ env + SQLite.
    2. Sau startup, thư mục `tmp_path` chỉ chứa artefact SQLite (file DB +
       optional side-file `-wal`/`-shm`/`-journal` do WAL mode). KHÔNG có
       file `.json`/`.yaml`/`.yml`/`.toml` nào — bằng chứng gián tiếp
       nhưng đủ mạnh cho R11.1: nếu startup phải sinh/đọc file config, nó
       sẽ để lại dấu vết trong sandbox `tmp_path`.
    3. Set 1 setting qua `settings_repo.set(...)` ở lần startup 1, restart
       `create_app()` với CÙNG DB, đọc lại qua `settings_repo.get(...)` —
       giá trị PHẢI persist. Chứng minh config đi qua SQLite chứ không đi
       qua file khác/state in-memory ngoài DB.

_Requirements: 11.1_
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.settings_store import SettingsRepository


# ---------------------------------------------------------------------------
# Hằng số dùng chung — env var bootstrap được phép (chicken-and-egg, docstring
# `app/main.py` giải thích) + đuôi file config bị R11.1 cấm.
# ---------------------------------------------------------------------------

_ENV_DB_PATH = "IDEAL_QR_TOOL_DB_PATH"
_ENV_BIND_HOST = "IDEAL_QR_TOOL_BIND_HOST"
_LOOPBACK = "127.0.0.1"

#: Đuôi file config runtime bị R11.1 cấm (JSON/YAML/TOML). `pyproject.toml`
#: là packaging metadata — không phải scope của test này (không nằm trong
#: `tmp_path`).
_FORBIDDEN_CONFIG_EXTENSIONS: frozenset[str] = frozenset(
    {".json", ".yaml", ".yml", ".toml"}
)


def _prepare_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set 2 env var bootstrap và trả về đường dẫn DB tạm cho test.

    Chỉ 2 env này được phép làm nguồn config cấp bootstrap (main.py docstring
    giải thích lý do chicken-and-egg). Mọi cấu hình khác PHẢI đi qua
    Settings_Store.
    """
    db_path = tmp_path / "test.db"
    monkeypatch.setenv(_ENV_DB_PATH, str(db_path))
    monkeypatch.setenv(_ENV_BIND_HOST, _LOOPBACK)
    return db_path


def test_lifespan_startup_populates_app_state_from_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`TestClient(app)` trigger lifespan → `app.state` gắn singleton từ SQLite.

    Validates: Requirements 11.1 — startup Backend_Service hydrate singleton
    core từ Settings_Store (SQLite), expose qua `app.state.*` cho test.
    """
    db_path = _prepare_env(tmp_path, monkeypatch)

    # Import factory SAU khi set env — tránh module-level `app = create_app()`
    # đã cache lifespan trước đó vẫn OK vì `create_app()` là function, mỗi
    # lần gọi đều resolve env lại (không đóng gói giá trị vào module state).
    from app.main import create_app  # noqa: PLC0415 — defer import cho env

    fastapi_app = create_app()

    with TestClient(fastapi_app):
        assert isinstance(fastapi_app.state.settings_repo, SettingsRepository), (
            "app.state.settings_repo phải là instance SettingsRepository — "
            "startup không bơm singleton từ Settings_Store."
        )
        assert fastapi_app.state.db_engine is not None, (
            "app.state.db_engine không được None sau khi lifespan startup chạy."
        )
        assert Path(fastapi_app.state.db_path) == db_path, (
            "app.state.db_path phải khớp env IDEAL_QR_TOOL_DB_PATH — "
            f"expected {db_path}, got {fastapi_app.state.db_path}."
        )


def test_startup_creates_only_sqlite_artefacts_in_tmp_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sau startup, `tmp_path` KHÔNG chứa file JSON/YAML/TOML nào (R11.1).

    Bằng chứng gián tiếp nhưng chắc: nếu startup phải sinh/đọc file config
    ngoài SQLite, nó sẽ để dấu vết trong sandbox `tmp_path` (đường dẫn DB
    tạm mà test set qua env). Chỉ chấp nhận artefact SQLite (file DB chính
    + side-file WAL/SHM/journal).

    Validates: Requirements 11.1.
    """
    db_path = _prepare_env(tmp_path, monkeypatch)

    from app.main import create_app  # noqa: PLC0415

    fastapi_app = create_app()

    with TestClient(fastapi_app):
        pass  # lifespan startup + shutdown chạy bên trong `with`

    assert db_path.exists(), (
        f"SQLite DB không được tạo tại {db_path} — startup không đi qua "
        "Settings_Store."
    )

    forbidden_files = sorted(
        p
        for p in tmp_path.rglob("*")
        if p.is_file() and p.suffix.lower() in _FORBIDDEN_CONFIG_EXTENSIONS
    )
    assert not forbidden_files, (
        "Startup đã sinh/đọc file config JSON/YAML/TOML trong tmp_path — "
        "vi phạm R11.1 (mọi runtime config phải từ Settings_Store SQLite):\n"
        + "\n".join(f"  - {p}" for p in forbidden_files)
    )


async def test_setting_persists_across_app_restart_via_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set setting qua `settings_repo.set()`, restart app, đọc lại → persist.

    Chứng minh config đi qua SQLite (file DB chia sẻ giữa 2 lần khởi tạo
    `create_app()`), không phải in-memory state hay file trung gian khác.
    Dùng `ui.input_draft` (đã có type constraint trong core registry) để
    tránh phải đăng ký namespace mới.

    Validates: Requirements 11.1.
    """
    _prepare_env(tmp_path, monkeypatch)

    from app.main import create_app  # noqa: PLC0415

    injected_value = "task-26-2-persist-probe"

    # 1) Lần startup 1 — inject setting qua SettingsRepository (route DB).
    app_first = create_app()
    async with app_first.router.lifespan_context(app_first):
        settings_repo_first: SettingsRepository = app_first.state.settings_repo
        await settings_repo_first.set("ui.input_draft", injected_value)

    # 2) Lần startup 2 — CÙNG DB path (env không đổi giữa 2 lần), instance
    #    `SettingsRepository` mới đọc từ SQLite. Giá trị PHẢI khớp.
    app_second = create_app()
    async with app_second.router.lifespan_context(app_second):
        settings_repo_second: SettingsRepository = app_second.state.settings_repo
        assert settings_repo_second is not settings_repo_first, (
            "Restart phải tạo instance SettingsRepository mới — nếu là "
            "cùng instance thì persistence check không có ý nghĩa."
        )
        persisted_value = await settings_repo_second.get("ui.input_draft")

    assert persisted_value == injected_value, (
        "Setting không persist qua restart — "
        f"expected {injected_value!r}, got {persisted_value!r}. "
        "Điều này cho thấy config KHÔNG đi qua SQLite (R11.1)."
    )
