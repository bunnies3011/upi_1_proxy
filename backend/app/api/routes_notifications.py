"""Telegram notifier routes: test send, reset cursor, batch-tally close.

Imports `TelegramNotifier` directly (same pattern as settings routes).
Config read stays on `GET /api/settings?prefix=telegram`.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.notifiers.telegram.notifier import TelegramConfigError, TelegramNotifier

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


# Module-level state — inject qua `configure_notifier(...)` trong startup.
# Cùng pattern với `deps._settings_repo`: notifier là singleton tạo 1 lần
# trong bootstrap, các route lấy qua getter.
_telegram_notifier: TelegramNotifier | None = None


def configure_telegram_notifier(notifier: TelegramNotifier) -> None:
    """Inject `TelegramNotifier` singleton — được `main.py` gọi tại startup
    sau khi `bootstrap_services` trả về notifier."""
    global _telegram_notifier
    _telegram_notifier = notifier


def _get_notifier() -> TelegramNotifier:
    if _telegram_notifier is None:
        raise HTTPException(
            status_code=503,
            detail={"error_code": "telegram_notifier_not_configured"},
        )
    return _telegram_notifier


@router.post("/telegram/test")
async def test_telegram() -> dict:
    """Gửi text ping tới tất cả chat_target đang enabled.

    Response:
        200 `{sent: [chat_id, ...], failed: [{chat_id, error}, ...]}`
        400 `{error_code: "telegram_config_error", message: "..."}` khi
            bot_token rỗng / không có chat enabled.
    """
    notifier = _get_notifier()
    try:
        result = await notifier.send_test_message()
    except TelegramConfigError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "telegram_config_error",
                "message": str(exc),
            },
        )
    return result


@router.post("/telegram/reset-cursor")
async def reset_cursor() -> dict:
    """Reset round-robin cursor về 0.

    Trả cursor cũ (trước reset) để UI có thể hiển thị "đã reset (N → 0)".
    Idempotent — gọi lại khi cursor đã 0 vẫn OK.
    """
    notifier = _get_notifier()
    previous = await notifier.reset_round()
    return {"previous_cursor": previous, "current_cursor": notifier.cursor()}


@router.post("/telegram/batch-tally/reset")
async def reset_batch_tally() -> dict:
    """Close period: post receipt per chat, decrement receipted counters."""
    return await _get_notifier().reset_batch_tally()


@router.get("/telegram/push-gate")
async def get_push_gate() -> dict:
    """Trả snapshot Push_Success_Gate.

    Response shape:
        {"enabled": bool, "threshold": int, "counter": int, "paused": bool}

    - `enabled`/`threshold` đọc live từ Settings (`telegram.push_mode.success_wait.*`).
    - `counter`/`paused` là state in-memory của gate (reset khi restart).

    Dùng khi FE lần đầu load để lấy state khởi tạo; sau đó cập nhật qua
    SSE event `push_gate_updated`.
    """
    notifier = _get_notifier()
    if notifier.push_gate is None:
        raise HTTPException(
            status_code=503,
            detail={"error_code": "push_gate_not_configured"},
        )
    return await notifier.push_gate.snapshot()


@router.post("/telegram/push-gate/resume")
async def resume_push_gate() -> dict:
    """Reset counter về 0, `paused=False`, wake scheduler, broadcast SSE.

    Idempotent — gọi khi gate đã ở state (0, False) vẫn broadcast 1
    event để FE sync (rẻ, không side-effect nghiêm trọng).

    Response: snapshot state mới (giống `GET /push-gate`).
    """
    notifier = _get_notifier()
    if notifier.push_gate is None:
        raise HTTPException(
            status_code=503,
            detail={"error_code": "push_gate_not_configured"},
        )
    return await notifier.push_gate.resume()


@router.get("/telegram/live-qr-gate")
async def get_live_qr_gate() -> dict:
    """Snapshot Live QR Gate (per-chat rolling capacity).

    Response:
        {enabled, max_per_chat, live, capacity, blocked, per_chat}
    No resume endpoint — free slots auto-wake on plus/timeout/lifecycle.
    """
    notifier = _get_notifier()
    if notifier.live_qr_gate is None:
        raise HTTPException(
            status_code=503,
            detail={"error_code": "live_qr_gate_not_configured"},
        )
    await notifier.live_qr_gate.refresh_config()
    return notifier.live_qr_gate.snapshot()
