#!/usr/bin/env python3
"""Smoke test luồng sync `ui.input_draft` qua SSE.

Kịch bản:
1. PUT `/api/settings/ui.input_draft` với value = "line1\\nline2" +
   header `X-Client-Id: client-A` → 200 + SettingsRepository lưu.
2. Bên kia (giả lập client B đang subscribe SSE) nhận event
   `setting_updated` với `source_client_id='client-A'` → client B apply,
   client A bỏ qua echo.
3. GET `/api/settings/ui.input_draft` trả đúng nội dung đã lưu.
4. String vượt max=100_000 → 400 `settings_validation_error`.

Chạy: ./.venv/bin/python3 test/smoke_input_draft_sync.py từ backend/
"""
from __future__ import annotations

import asyncio
import json
import sys
import traceback
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.db import DbEngine  # noqa: E402
from app.core.errors import SettingsValidationError  # noqa: E402
from app.core.settings_store import SettingsRepository  # noqa: E402
from app.core.sse import SseBroadcaster  # noqa: E402


async def _main() -> int:
    failed = 0

    # In-memory DB để test cô lập.
    engine = DbEngine(":memory:")
    await engine.init_schema()
    settings = SettingsRepository(engine)
    sse = SseBroadcaster()

    # -------------------------------------------------------------------
    # TC-01: whitelist `ui.input_draft` đã được đăng ký, cho phép ghi/đọc.
    # -------------------------------------------------------------------
    tc = "TC-01"
    try:
        await settings.set("ui.input_draft", "line1\nline2\nline3")
        val = await settings.get("ui.input_draft")
        assert val == "line1\nline2\nline3", f"got {val!r}"
        print(f"[PASS] {tc} — ghi/đọc ui.input_draft OK", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {tc} — {type(exc).__name__}: {exc}", flush=True)
        failed += 1

    # -------------------------------------------------------------------
    # TC-02: value quá dài → SettingsValidationError.
    # -------------------------------------------------------------------
    tc = "TC-02"
    try:
        try:
            await settings.set("ui.input_draft", "x" * 100_001)
            print(f"[FAIL] {tc} — không raise cho value 100_001 ký tự", flush=True)
            failed += 1
        except SettingsValidationError as exc:
            assert "range" in exc.reason.lower() or "vượt" in exc.reason.lower()
            print(f"[PASS] {tc} — reject value > 100_000 ký tự: {exc.reason}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {tc} — {type(exc).__name__}: {exc}", flush=True)
        failed += 1

    # -------------------------------------------------------------------
    # TC-03: SseBroadcaster.broadcast_setting_updated → 2 client nhận
    # event; client có `source_client_id` trùng sẽ tự lọc echo ở tầng FE
    # (test này chỉ verify payload chứa key/value/source_client_id).
    # -------------------------------------------------------------------
    tc = "TC-03"
    try:
        client_a_queue = sse.register_client()
        client_b_queue = sse.register_client()

        await sse.broadcast_setting_updated(
            key="ui.input_draft",
            value="hello world",
            source_client_id="client-A",
        )

        # Cả 2 client cùng nhận event.
        raw_a = client_a_queue.get_nowait()
        raw_b = client_b_queue.get_nowait()
        assert raw_a == raw_b, "payload phải giống nhau cho mọi client"

        # Parse SSE format: `event: setting_updated\ndata: {...}\n\n`
        lines = raw_a.split("\n")
        event_line = next(l for l in lines if l.startswith("event: "))
        data_line = next(l for l in lines if l.startswith("data: "))
        assert event_line == "event: setting_updated"
        payload = json.loads(data_line[len("data: ") :])
        assert payload["key"] == "ui.input_draft"
        assert payload["value"] == "hello world"
        assert payload["source_client_id"] == "client-A"
        print(f"[PASS] {tc} — broadcast_setting_updated payload OK", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {tc} — {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        failed += 1

    # -------------------------------------------------------------------
    # TC-04: source_client_id=None (VD CLI update) — payload không có key
    # đó → mọi client FE apply (không có gì để filter).
    # -------------------------------------------------------------------
    tc = "TC-04"
    try:
        q = sse.register_client()
        await sse.broadcast_setting_updated(
            key="proxy.rotation_mode", value="least_used", source_client_id=None
        )
        raw = q.get_nowait()
        data_line = next(l for l in raw.split("\n") if l.startswith("data: "))
        payload = json.loads(data_line[len("data: ") :])
        assert payload["key"] == "proxy.rotation_mode"
        assert payload["value"] == "least_used"
        assert "source_client_id" not in payload
        print(f"[PASS] {tc} — source_client_id=None → key omitted", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {tc} — {type(exc).__name__}: {exc}", flush=True)
        failed += 1

    await engine.close()
    print(f"\n== Done: fail={failed} ==", flush=True)
    return failed


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
