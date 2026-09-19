"""Smoke check cho fix "Telegram notify không retry" (queue + backoff).

Kiểm tra:
    TC-01: Import module `app.notifiers.telegram.notifier` +
           `app.notifiers.telegram.client` không lỗi syntax/import.
    TC-02: `_compute_backoff_delay` trả đúng lịch 2s/5s/10s và tôn trọng
           `retry_after` khi nó lớn hơn lịch mặc định.
    TC-03: `_is_retryable_api_error` phân loại đúng 429/5xx (retryable)
           vs 400/401/403 (không retryable).
    TC-04: `TelegramNotifier.notify_qr_ready` enqueue vào `_queue` và
           return NGAY (không block) — dùng `FakeClient` giả lập
           `send_photo` chậm/fail để verify hook không `await` tới khi
           gửi xong.
    TC-05: `_send_with_retry` retry đúng số lần khi `FakeClient` fail
           liên tục lỗi transient (429), rồi thành công ở lần cuối —
           verify `attempts` ghi nhận đúng trong entry track.
    TC-06: Lỗi permanent (400) KHÔNG retry — chỉ gọi `send_photo` 1 lần.

Chạy: python3 test/check_telegram_retry_queue.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_PASS = 0
_FAIL = 0


def report(tc: str, desc: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    status = "PASS" if ok else "FAIL"
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
    print(f"[{status}] {tc} — {desc} :: {detail}", flush=True)


def tc01_import() -> None:
    try:
        from app.notifiers.telegram import notifier as notifier_mod  # noqa: F401
        from app.notifiers.telegram import client as client_mod  # noqa: F401
        report("TC-01", "import notifier + client module", True, "ok")
    except Exception as exc:  # noqa: BLE001
        report("TC-01", "import notifier + client module", False, f"{type(exc).__name__}: {exc}")
        raise


def tc02_backoff() -> None:
    from app.notifiers.telegram.notifier import _compute_backoff_delay

    d1 = _compute_backoff_delay(1, None)
    d2 = _compute_backoff_delay(2, None)
    d3 = _compute_backoff_delay(3, None)
    d4 = _compute_backoff_delay(4, None)  # attempt vượt bảng — clamp về cuối
    ok = d1 == 2.0 and d2 == 5.0 and d3 == 10.0 and d4 == 10.0
    report("TC-02a", "backoff schedule 2/5/10s clamp", ok, f"{d1},{d2},{d3},{d4}")

    # retry_after lớn hơn lịch mặc định -> dùng retry_after
    d_override = _compute_backoff_delay(1, 30.0)
    ok2 = d_override == 30.0
    report("TC-02b", "retry_after override khi lớn hơn lịch", ok2, f"{d_override}")

    # retry_after nhỏ hơn lịch -> vẫn dùng lịch (không rút ngắn)
    d_small = _compute_backoff_delay(2, 1.0)
    ok3 = d_small == 5.0
    report("TC-02c", "retry_after nhỏ hơn lịch -> giữ lịch", ok3, f"{d_small}")


def tc03_retryable() -> None:
    from app.notifiers.telegram.client import TelegramApiError
    from app.notifiers.telegram.notifier import _is_retryable_api_error

    err_429 = TelegramApiError(200, 429, "Too Many Requests", retry_after=5.0)
    err_500 = TelegramApiError(500, None, "Internal Server Error")
    err_400 = TelegramApiError(400, 400, "Bad Request: chat not found")
    err_401 = TelegramApiError(401, 401, "Unauthorized")
    err_403 = TelegramApiError(403, 403, "Forbidden")

    ok = (
        _is_retryable_api_error(err_429) is True
        and _is_retryable_api_error(err_500) is True
        and _is_retryable_api_error(err_400) is False
        and _is_retryable_api_error(err_401) is False
        and _is_retryable_api_error(err_403) is False
    )
    report("TC-03", "phân loại retryable 429/5xx vs 400/401/403", ok, "checked 5 case")


class _FakeSettings:
    """Fake SettingsRepository — trả config cố định cho `_read_config`."""

    def __init__(self, chat_targets: list[dict[str, Any]]) -> None:
        self._chat_targets = chat_targets

    async def get(self, key: str) -> Any:
        if key == "telegram.enabled":
            return True
        if key == "telegram.bot_token":
            return "123:FAKE_TOKEN"
        if key == "telegram.chat_targets":
            return self._chat_targets
        return None


class _FakeClient:
    """Fake TelegramBotClient — `send_photo` theo scripted behavior."""

    def __init__(self, behaviors: list[Any]) -> None:
        # Mỗi item: None (success) hoặc Exception instance để raise.
        self._behaviors = list(behaviors)
        self.call_count = 0
        self.call_delays_recorded: list[float] = []

    async def send_photo(self, **kwargs: Any) -> dict:
        self.call_count += 1
        if self._behaviors:
            behavior = self._behaviors.pop(0)
        else:
            behavior = None
        if behavior is not None:
            raise behavior
        return {"message_id": 1}

    async def aclose(self) -> None:
        """No-op — `TelegramNotifier.aclose()` gọi method này khi shutdown."""


class _FakeJobManagerTrack:
    """Fake JobManager chỉ implement `record_telegram_notification`."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, Any]]] = []

    async def record_telegram_notification(self, job_id: str, entry: dict[str, Any]) -> None:
        self.entries.append((job_id, entry))


class _FakeJob:
    def __init__(self, job_id: str, account_line: str = "user@example.com") -> None:
        self.job_id = job_id
        self.account_line = account_line


class _FakeRecord:
    def __init__(self, job_id: str, artifact_path: str) -> None:
        self.job = _FakeJob(job_id)
        self.artifact_path = artifact_path
        self.finished_at = time.time()
        self.payment_link = "https://pay.example.com/abc"


async def tc04_enqueue_returns_fast(tmp_png: Path) -> None:
    from app.notifiers.telegram.notifier import TelegramNotifier
    from app.notifiers.telegram.distributor import RoundRobinDistributor

    settings = _FakeSettings([{"chat_id": "111", "label": "A", "enabled": True}])
    # Behavior: send_photo "chậm" — sleep 2s trước khi trả OK. Nếu
    # notify_qr_ready block chờ send_photo, test sẽ tốn >= 2s. Nếu enqueue
    # đúng cách, notify_qr_ready phải return trong < 0.5s.
    class _SlowClient(_FakeClient):
        async def send_photo(self, **kwargs: Any) -> dict:
            await asyncio.sleep(2.0)
            return await super().send_photo(**kwargs)

    client = _SlowClient([])
    job_manager = _FakeJobManagerTrack()
    notifier = TelegramNotifier(
        settings=settings,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        distributor=RoundRobinDistributor(),
        job_manager=job_manager,  # type: ignore[arg-type]
    )
    record = _FakeRecord("job-1", str(tmp_png))

    t0 = time.monotonic()
    await notifier.notify_qr_ready(record)  # type: ignore[arg-type]
    elapsed = time.monotonic() - t0

    ok = elapsed < 0.5
    report("TC-04", "notify_qr_ready return nhanh (không block chờ send_photo)", ok, f"elapsed={elapsed:.3f}s")

    # Chờ worker xử lý xong item rồi mới aclose (tránh cancel giữa lúc gửi).
    await asyncio.sleep(2.5)
    await notifier.aclose()

    ok2 = len(job_manager.entries) == 1 and job_manager.entries[0][1]["success"] is True
    report("TC-04b", "worker vẫn gửi thành công sau khi notify_qr_ready return", ok2, f"entries={job_manager.entries}")


async def tc05_retry_then_success(tmp_png: Path) -> None:
    from app.notifiers.telegram.notifier import TelegramNotifier
    from app.notifiers.telegram.distributor import RoundRobinDistributor
    from app.notifiers.telegram.client import TelegramApiError

    settings = _FakeSettings([{"chat_id": "222", "label": "B", "enabled": True}])
    err_429 = TelegramApiError(200, 429, "Too Many Requests", retry_after=0.1)
    # Fail 2 lần với 429, thành công lần 3. `retry_after=0.1` NHỎ hơn lịch
    # backoff mặc định (2s/5s) nên schedule mặc định thắng (đúng semantic
    # đã verify ở TC-02c) — để test chạy nhanh, monkeypatch
    # `_sleep_before_retry` bỏ qua thời gian chờ thật, chỉ verify logic
    # đếm retry/attempts, KHÔNG verify độ dài delay thật (đã có TC-02).
    client = _FakeClient([err_429, err_429, None])
    job_manager = _FakeJobManagerTrack()
    notifier = TelegramNotifier(
        settings=settings,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        distributor=RoundRobinDistributor(),
        job_manager=job_manager,  # type: ignore[arg-type]
    )

    async def _instant_sleep(delay: float) -> None:
        return None

    notifier._sleep_before_retry = _instant_sleep  # type: ignore[assignment]

    record = _FakeRecord("job-2", str(tmp_png))

    await notifier.notify_qr_ready(record)  # type: ignore[arg-type]
    await asyncio.sleep(0.3)  # đợi worker retry xong (không còn delay thật)
    await notifier.aclose()

    ok = (
        client.call_count == 3
        and len(job_manager.entries) == 1
        and job_manager.entries[0][1]["success"] is True
        and job_manager.entries[0][1]["attempts"] == 3
    )
    report(
        "TC-05",
        "retry 2 lần lỗi 429 rồi thành công lần 3, track attempts=3",
        ok,
        f"call_count={client.call_count} entries={job_manager.entries}",
    )


async def tc06_permanent_error_no_retry(tmp_png: Path) -> None:
    from app.notifiers.telegram.notifier import TelegramNotifier
    from app.notifiers.telegram.distributor import RoundRobinDistributor
    from app.notifiers.telegram.client import TelegramApiError

    settings = _FakeSettings([{"chat_id": "333", "label": "C", "enabled": True}])
    err_400 = TelegramApiError(400, 400, "Bad Request: chat not found")
    client = _FakeClient([err_400])
    job_manager = _FakeJobManagerTrack()
    notifier = TelegramNotifier(
        settings=settings,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        distributor=RoundRobinDistributor(),
        job_manager=job_manager,  # type: ignore[arg-type]
    )
    record = _FakeRecord("job-3", str(tmp_png))

    await notifier.notify_qr_ready(record)  # type: ignore[arg-type]
    await asyncio.sleep(0.3)
    await notifier.aclose()

    ok = (
        client.call_count == 1
        and len(job_manager.entries) == 1
        and job_manager.entries[0][1]["success"] is False
        and job_manager.entries[0][1]["attempts"] == 1
    )
    report(
        "TC-06",
        "lỗi permanent 400 KHÔNG retry, chỉ gọi 1 lần",
        ok,
        f"call_count={client.call_count} entries={job_manager.entries}",
    )


def main() -> int:
    print("=== Telegram retry+queue smoke check ===", flush=True)
    tc01_import()
    tc02_backoff()
    tc03_retryable()

    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_png = Path(tmp_dir) / "qr.png"
        tmp_png.write_bytes(b"\x89PNG\r\n\x1a\nfake-png-bytes")

        asyncio.run(tc04_enqueue_returns_fast(tmp_png))
        asyncio.run(tc05_retry_then_success(tmp_png))
        asyncio.run(tc06_permanent_error_no_retry(tmp_png))

    print(f"=== Kết quả: {_PASS} PASS / {_FAIL} FAIL ===", flush=True)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
