"""Lightweight smoke checks for Telegram notifier imports and formatting.

Run:
    python test/check_telegram_notifier_syntax.py
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _log(status: str, case_id: str, desc: str, detail: str = "") -> None:
    line = f"[{status}] {case_id} - {desc}"
    if detail:
        line += f" :: {detail}"
    print(line, flush=True)


def _run() -> int:
    failed = 0

    try:
        from app.notifiers.telegram import (  # noqa: F401
            RoundRobinDistributor,
            TelegramBotClient,
            TelegramNotifier,
            register_telegram_namespace,
            register_telegram_notifier,
        )
        _log("PASS", "TC-01", "import app.notifiers.telegram")
    except Exception as exc:  # noqa: BLE001
        _log("FAIL", "TC-01", "import app.notifiers.telegram", f"{type(exc).__name__}: {exc}")
        return 1

    from app.notifiers.telegram.formatter import (
        build_caption,
        format_india_time,
        format_vn_time,
        mask_email,
    )
    from app.notifiers.telegram.notifier import (
        _filter_active_chat_ids,
        _normalize_chat_targets,
    )

    try:
        assert inspect.iscoroutinefunction(TelegramNotifier.notify_qr_ready)
        assert inspect.iscoroutinefunction(TelegramNotifier.send_test_message)
        assert inspect.iscoroutinefunction(TelegramNotifier.reset_round)
        assert inspect.iscoroutinefunction(TelegramNotifier.aclose)
        _log("PASS", "TC-02", "TelegramNotifier async signatures")
    except AssertionError as exc:
        _log("FAIL", "TC-02", "TelegramNotifier async signatures", str(exc))
        failed += 1

    email_cases = [
        ("abc@gmail.com", "ab***@gmail.com"),
        ("abc@gmail.com|pw|totp", "ab***@gmail.com"),
        ("a@x.io", "a***@x.io"),
        ("longlocal@corp.co.uk", "lo***@corp.co.uk"),
        ("noatsign", "***"),
        ("", "***"),
        ("   ", "***"),
    ]
    for idx, (source, expected) in enumerate(email_cases, start=1):
        got = mask_email(source)
        if got == expected:
            _log("PASS", f"TC-03.{idx}", f"mask_email({source!r})", got)
        else:
            _log("FAIL", f"TC-03.{idx}", f"mask_email({source!r})", f"got={got!r} want={expected!r}")
            failed += 1

    if format_vn_time(1704067200.0) == "07:00:00 01/01/2024":
        _log("PASS", "TC-04", "format_vn_time")
    else:
        _log("FAIL", "TC-04", "format_vn_time", format_vn_time(1704067200.0))
        failed += 1

    if format_india_time(1704067200.0) == "05:30:00 01/01/2024":
        _log("PASS", "TC-05", "format_india_time")
    else:
        _log("FAIL", "TC-05", "format_india_time", format_india_time(1704067200.0))
        failed += 1

    cap_default = build_caption(
        account_line="user@example.com|pw|totp",
        finished_at=1704067200.0,
        payment_link="https://pay.ideal.nl/x?sig=abc",
    )
    required_default = [
        "<b>iDEAL QR ready</b>",
        "user@example.com",
        "07:00:00 01/01/2024",
        'href="https://pay.ideal.nl/x?sig=abc"',
    ]
    missing_default = [part for part in required_default if part not in cap_default]
    if not missing_default:
        _log("PASS", "TC-06", "build_caption default path")
    else:
        _log("FAIL", "TC-06", "build_caption default path", str(missing_default))
        failed += 1

    cap_upi = build_caption(
        account_line="user@example.com|pw",
        finished_at=1704067200.0,
        payment_link="https://payments.stripe.com/upi/instructions/demo",
        payment_method="upi_direct",
        order=1,
        qr_expires_at=1704067800.0,
        chat_label="@demo-group",
        chat_id="-100123",
        sent_at=1704067260.0,
    )
    required_upi = [
        "QR #1 - UPI ChatGPT Plus (IN)",
        "user@example.com",
        "Expires: 07:10:00 01/01/2024 VN",
        "Expired: 05:40:00 01/01/2024 IN",
        "https://payments.stripe.com/upi/instructions/demo",
        "Người nhận QR",
        "Nhận lúc: 07:01:00 01/01/2024 VN",
    ]
    missing_upi = [part for part in required_upi if part not in cap_upi]
    if not missing_upi:
        _log("PASS", "TC-07", "build_caption UPI path")
    else:
        _log("FAIL", "TC-07", "build_caption UPI path", str(missing_upi))
        failed += 1

    cap_no_link = build_caption("user@example.com|pw", 1704067200.0, None)
    if "Open payment link" not in cap_no_link:
        _log("PASS", "TC-08", "build_caption hides missing link")
    else:
        _log("FAIL", "TC-08", "build_caption hides missing link", cap_no_link)
        failed += 1

    normed = _normalize_chat_targets(
        [
            {"chat_id": "-100123", "label": "A", "enabled": True},
            {"chat_id": "  456  ", "label": "B", "enabled": False},
            {"chat_id": "", "label": "skip", "enabled": True},
            {"label": "no chat_id", "enabled": True},
            "not a dict",
            {"chat_id": 12345, "label": "int chat", "enabled": True},
        ]
    )
    if len(normed) == 2 and normed[0]["chat_id"] == "-100123" and normed[1]["chat_id"] == "456":
        _log("PASS", "TC-09", "_normalize_chat_targets")
    else:
        _log("FAIL", "TC-09", "_normalize_chat_targets", str(normed))
        failed += 1

    active = _filter_active_chat_ids(
        [
            {"chat_id": "-100123", "label": "A", "enabled": True},
            {"chat_id": "456", "label": "B", "enabled": False},
            {"chat_id": "789", "label": "C", "enabled": True},
        ]
    )
    if active == ["-100123", "789"]:
        _log("PASS", "TC-10", "_filter_active_chat_ids")
    else:
        _log("FAIL", "TC-10", "_filter_active_chat_ids", str(active))
        failed += 1

    async def _rr() -> tuple[list[str], int, list[str], int]:
        dist = RoundRobinDistributor()
        ids = ["A", "B", "C"]
        picks = [await dist.pick(ids) for _ in range(7)]
        old = await dist.reset()
        after = [await dist.pick(ids) for _ in range(2)]
        return picks, old, after, dist.snapshot_cursor()

    picks, old_cursor, picks_after, cursor_now = asyncio.run(_rr())
    if picks == ["A", "B", "C", "A", "B", "C", "A"] and old_cursor == 7 and picks_after == ["A", "B"] and cursor_now == 2:
        _log("PASS", "TC-11", "RoundRobinDistributor pick/reset")
    else:
        _log("FAIL", "TC-11", "RoundRobinDistributor pick/reset", f"{picks=} {old_cursor=} {picks_after=} {cursor_now=}")
        failed += 1

    async def _rr_empty() -> None:
        dist = RoundRobinDistributor()
        await dist.pick([])

    try:
        asyncio.run(_rr_empty())
        _log("FAIL", "TC-12", "pick([]) should raise ValueError")
        failed += 1
    except ValueError:
        _log("PASS", "TC-12", "pick([]) raises ValueError")
    except Exception as exc:  # noqa: BLE001
        _log("FAIL", "TC-12", "pick([]) wrong exception", f"{type(exc).__name__}: {exc}")
        failed += 1

    if failed == 0:
        _log("PASS", "SUMMARY", "all checks passed")
        return 0

    _log("FAIL", "SUMMARY", f"{failed} checks failed")
    return 1


if __name__ == "__main__":
    raise SystemExit(_run())
