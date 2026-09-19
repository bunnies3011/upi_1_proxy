"""Integration tests for UPI license-pool hydration wiring (Phase 4b).

Phase 4 registered `UpiFlowHandler` + its internal `UpiLicensePool`, but the pool
was never fed the `upi.license_codes` setting. These tests pin the two hydration
points that close that gap, mirroring how `ProxyPool` is hydrated:

    1. Startup — `bootstrap_services` must call `apply_settings` on the pool so a
       DB that already lists PK codes yields a NON-empty pool after boot.
    2. Runtime — a settings-API update to `upi.license_codes` must fan out to the
       live pool (via `_write_through_runtime_state`), exactly like a `proxy.*`
       update fans out to `ProxyPool`.

Both assertions read the pool through the handler registered in `JobManager`
(`get_handler("upi").license_pool`) — the same object the route mutates — so no
vendor `key/verify` network call is needed: `apply_settings` populates `_codes`
synchronously without verifying.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.core.db import DbEngine
from app.core.settings_store import SettingsRepository
from app.payments.upi import register_upi_namespace

_ENV_DB_PATH = "IDEAL_QR_TOOL_DB_PATH"
_ENV_BIND_HOST = "IDEAL_QR_TOOL_BIND_HOST"
_LOOPBACK = "127.0.0.1"

_STARTUP_CODES = ["PK-STARTUP-1", "PK-STARTUP-2"]
_RUNTIME_CODE = "PK-RUNTIME-9"


def _prepare_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "hydration.db"
    monkeypatch.setenv(_ENV_DB_PATH, str(db_path))
    monkeypatch.setenv(_ENV_BIND_HOST, _LOOPBACK)
    return db_path


async def _preseed_upi_codes(db_path: Path, codes: list[str]) -> None:
    """Persist `upi.license_codes` into a DB file BEFORE bootstrap opens it.

    Uses a throwaway `SettingsRepository` (namespace registration is in-memory,
    per instance) whose written values persist in the SQLite file. Bootstrap's
    own seed loop only writes defaults when a key is unset, so these codes
    survive.
    """
    engine = DbEngine(db_path)
    await engine.init_schema()
    settings = SettingsRepository(engine)
    await register_upi_namespace(settings)
    await settings.set("upi.license_codes", codes)
    await engine.close()


async def test_startup_hydrates_upi_pool_from_seeded_license_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After bootstrap on a DB that already lists PK codes, the registered UPI
    handler's pool is NON-empty (hydrated from `upi.license_codes`)."""
    db_path = _prepare_env(tmp_path, monkeypatch)
    await _preseed_upi_codes(db_path, _STARTUP_CODES)

    from app.main import create_app  # noqa: PLC0415 — defer import until env set

    app = create_app()
    async with app.router.lifespan_context(app):
        handler = deps.get_job_manager().get_handler("upi")
        assert handler is not None, "UPI handler not registered at startup"
        assert set(handler.license_pool._codes.keys()) == set(_STARTUP_CODES), (
            "UPI pool not hydrated from seeded upi.license_codes at startup — "
            f"got {sorted(handler.license_pool._codes.keys())!r}"
        )


def test_runtime_settings_update_fans_out_to_upi_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settings-API PUT to `upi.license_codes` re-applies to the live pool,
    mirroring the proxy write-through."""
    _prepare_env(tmp_path, monkeypatch)

    from app.main import create_app  # noqa: PLC0415 — defer import until env set

    app = create_app()
    with TestClient(app) as client:
        resp = client.put(
            "/api/settings/upi.license_codes",
            json={"value": [_RUNTIME_CODE]},
        )
        assert resp.status_code == 200, resp.text

        handler = deps.get_job_manager().get_handler("upi")
        assert handler is not None, "UPI handler not registered at startup"
        assert set(handler.license_pool._codes.keys()) == {_RUNTIME_CODE}, (
            "UPI pool did not pick up the new upi.license_codes after settings "
            f"update — got {sorted(handler.license_pool._codes.keys())!r}"
        )
        # The route mutates the pool exposed via the new dependency getter — it
        # must be the very same object the handler holds.
        assert deps.get_upi_license_pool() is handler.license_pool
