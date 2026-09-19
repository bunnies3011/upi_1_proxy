"""Smoke test for Telegram QR notification dedup.

Run:
    python test/check_telegram_notify_dedup.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


class _FakeSettings:
    def __init__(self, chat_targets: list[dict[str, Any]] | None = None) -> None:
        self.chat_targets = chat_targets or [
            {"chat_id": "-100111", "label": "Group A", "enabled": True}
        ]

    async def get(self, key: str) -> Any:
        values: dict[str, Any] = {
            "telegram.push_mode.enabled": True,
            "telegram.bot_token": "123:FAKE",
            "telegram.push_mode.send_photo": True,
            "telegram.push_mode.chat_targets": self.chat_targets,
        }
        return values.get(key)


class _FakeClient:
    def __init__(self) -> None:
        self.send_photo_calls: list[dict[str, Any]] = []

    async def send_photo(self, **kwargs: Any) -> dict[str, int]:
        self.send_photo_calls.append(kwargs)
        await asyncio.sleep(0.2)
        return {"message_id": 9001}

    async def send_message(self, **kwargs: Any) -> dict[str, int]:
        raise AssertionError("send_message should not be called in this test")

    async def aclose(self) -> None:
        return None


class _FakeJobManager:
    def __init__(self, record: "_FakeRecord") -> None:
        self.record = record
        self.entries: list[dict[str, Any]] = []

    async def record_telegram_notification(
        self, job_id: str, entry: dict[str, Any]
    ) -> None:
        self.entries.append(entry)
        self.record.telegram_notifications.append(entry)
        if entry.get("success") is True and entry.get("message_id") is not None:
            self.record.telegram_message_chat_id = str(entry["chat_id"])
            self.record.telegram_message_id = int(entry["message_id"])

    def get_job(self, job_id: str) -> "_FakeRecord":
        return self.record


class _FakeJob:
    def __init__(self) -> None:
        self.job_id = "job-dedup-1"
        self.account_line = "bo@example.com|pw|totp"
        self.payment_method = "upi_direct"


class _FakeRecord:
    def __init__(self, artifact_path: str) -> None:
        self.job = _FakeJob()
        self.pull_assignment_state = None
        self.artifact_path = artifact_path
        self.finished_at = time.time()
        self.payment_link = "https://payments.stripe.com/upi/instructions/demo"
        self.order = 5005
        self.qr_expires_at = self.finished_at + 300
        self.telegram_notifications: list[dict[str, Any]] = []
        self.telegram_message_chat_id: str | None = None
        self.telegram_message_id: int | None = None
        self.plan = None
        self.status = "qr_ready"


class _FakeLiveGate:
    def set_wake_worker_callback(self, callback: Any) -> None:
        self.callback = callback

    async def refresh_config(self) -> None:
        return None

    def snapshot(self) -> dict[str, Any]:
        return {"enabled": True}

    def is_blocked(self) -> bool:
        return False

    def least_loaded_available_chat(self, active: list[str]) -> str | None:
        return active[-1] if active else None

    async def acquire(
        self, job_id: str, chat_id: str, message_id: int | None
    ) -> None:
        return None


async def _run_case() -> None:
    from app.notifiers.telegram.distributor import RoundRobinDistributor
    from app.notifiers.telegram.notifier import TelegramNotifier

    with tempfile.TemporaryDirectory() as tmp:
        png_path = Path(tmp) / "qr.png"
        png_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")

        record = _FakeRecord(str(png_path))
        client = _FakeClient()
        manager = _FakeJobManager(record)
        notifier = TelegramNotifier(
            settings=_FakeSettings(),  # type: ignore[arg-type]
            client=client,  # type: ignore[arg-type]
            distributor=RoundRobinDistributor(),
            job_manager=manager,  # type: ignore[arg-type]
        )

        await asyncio.gather(
            notifier.notify_qr_ready(record),  # type: ignore[arg-type]
            notifier.notify_qr_ready(record),  # type: ignore[arg-type]
        )
        await notifier._queue.join()  # noqa: SLF001 - smoke test checks worker.

        assert len(client.send_photo_calls) == 1, client.send_photo_calls
        assert len(manager.entries) == 1, manager.entries
        assert manager.entries[0]["success"] is True

        # Once success is recorded, a later repeated hook must still be ignored.
        await notifier.notify_qr_ready(record)  # type: ignore[arg-type]
        await asyncio.sleep(0.05)
        assert len(client.send_photo_calls) == 1, client.send_photo_calls

        await notifier.aclose()

        repick_record = _FakeRecord(str(png_path))
        repick_record.job.job_id = "job-repick-1"
        repick_client = _FakeClient()
        repick_manager = _FakeJobManager(repick_record)
        repick_notifier = TelegramNotifier(
            settings=_FakeSettings(
                [
                    {"chat_id": "-100111", "label": "@GroupA", "enabled": True},
                    {"chat_id": "-100222", "label": "@GroupB", "enabled": True},
                ]
            ),  # type: ignore[arg-type]
            client=repick_client,  # type: ignore[arg-type]
            distributor=RoundRobinDistributor(),
            job_manager=repick_manager,  # type: ignore[arg-type]
            live_qr_gate=_FakeLiveGate(),  # type: ignore[arg-type]
        )
        await repick_notifier.notify_qr_ready(repick_record)  # type: ignore[arg-type]
        await repick_notifier._queue.join()  # noqa: SLF001
        assert len(repick_client.send_photo_calls) == 1
        call = repick_client.send_photo_calls[0]
        assert call["chat_id"] == "-100222", call
        assert "@GroupB" in call["caption"], call["caption"]
        assert "@GroupA" not in call["caption"], call["caption"]
        assert repick_manager.entries[0]["chat_id"] == "-100222"
        assert repick_manager.entries[0]["chat_label"] == "@GroupB"
        await repick_notifier.aclose()


def main() -> int:
    try:
        asyncio.run(_run_case())
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] telegram notify dedup :: {type(exc).__name__}: {exc}")
        return 1
    print("[PASS] telegram notify dedup")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
