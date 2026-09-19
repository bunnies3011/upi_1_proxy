"""Unit tests for POST /telegram/batch-tally/reset."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from app.api import routes_notifications


class _FakeNotifier:
    def __init__(self, summary: dict[str, Any] | None = None) -> None:
        self.calls = 0
        self._summary = summary or {
            "closed": ["-1001"],
            "skipped": ["-1002"],
            "plus_total": 7,
            "expired_total": 2,
        }

    async def reset_batch_tally(self) -> dict[str, Any]:
        self.calls += 1
        return self._summary


@pytest.fixture(autouse=True)
def _clear_notifier() -> None:
    routes_notifications._telegram_notifier = None
    yield
    routes_notifications._telegram_notifier = None


@pytest.mark.asyncio
async def test_reset_batch_tally_returns_summary() -> None:
    notifier = _FakeNotifier()
    routes_notifications.configure_telegram_notifier(notifier)  # type: ignore[arg-type]

    result = await routes_notifications.reset_batch_tally()

    assert notifier.calls == 1
    assert result == {
        "closed": ["-1001"],
        "skipped": ["-1002"],
        "plus_total": 7,
        "expired_total": 2,
    }


@pytest.mark.asyncio
async def test_reset_batch_tally_unconfigured_returns_503() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await routes_notifications.reset_batch_tally()

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {
        "error_code": "telegram_notifier_not_configured",
    }
