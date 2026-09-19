"""Syntax + import smoke check cho các module vừa sửa để verify backend hợp lệ.

Chỉ compile + import — KHÔNG chạy server, KHÔNG mở kết nối. Tất cả tiến
trình log 1 dòng theo bước để dev thấy chỗ nào fail ngay.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT / "backend"

# Đưa backend vào sys.path để import theo package `app.*` giống runtime.
sys.path.insert(0, str(BACKEND_DIR))

TARGET_FILES = [
    "app/notifiers/telegram/push_gate.py",
    "app/notifiers/telegram/__init__.py",
    "app/notifiers/telegram/notifier.py",
    "app/core/job_manager.py",
    "app/core/sse.py",
    "app/api/routes_notifications.py",
    "app/bootstrap.py",
]

TARGET_MODULES = [
    "app.notifiers.telegram.push_gate",
    "app.notifiers.telegram",
    "app.notifiers.telegram.notifier",
    "app.core.sse",
    "app.core.job_manager",
    "app.api.routes_notifications",
    # Không import `app.bootstrap` để tránh side-effect payment_ideal
    # import chain (lỗi thiếu dep). Chỉ compile.
]


def check_syntax() -> None:
    """py_compile mọi file mục tiêu — báo dòng lỗi cụ thể nếu sai."""
    total = len(TARGET_FILES)
    for idx, rel in enumerate(TARGET_FILES, start=1):
        abs_path = BACKEND_DIR / rel
        print(f"[{idx}/{total}] SYNTAX check {rel} ...", flush=True)
        if not abs_path.is_file():
            print(f"  [FAIL] file not found: {abs_path}", flush=True)
            sys.exit(1)
        import py_compile
        try:
            py_compile.compile(str(abs_path), doraise=True)
        except py_compile.PyCompileError as exc:
            print(f"  [FAIL] compile error:\n{exc}", flush=True)
            sys.exit(1)
        print(f"  [PASS] syntax OK", flush=True)


def check_imports() -> None:
    """import module để bắt lỗi runtime (missing symbol, import cycle)."""
    total = len(TARGET_MODULES)
    for idx, name in enumerate(TARGET_MODULES, start=1):
        print(f"[{idx}/{total}] IMPORT {name} ...", flush=True)
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            print(
                f"  [FAIL] {name}: {type(exc).__name__}: {exc}",
                flush=True,
            )
            raise
        print(f"  [PASS] import OK", flush=True)


def check_push_gate_symbols() -> None:
    """Kiểm nhanh symbol quan trọng có tồn tại đúng chỗ."""
    print("[1/1] SYMBOL check ...", flush=True)
    from app.notifiers.telegram import (  # type: ignore
        PushSuccessGate,
        register_telegram_notifier,
    )
    from app.notifiers.telegram.notifier import TelegramNotifier  # type: ignore
    from app.core.job_manager import JobManager  # type: ignore
    from app.core.sse import SseBroadcaster  # type: ignore

    # Chỉ verify các attribute mới đã tồn tại — không instantiate (cần
    # settings + engine thật).
    import inspect

    # Class-level attributes (method + class attr).
    for cls, attr in [
        (PushSuccessGate, "on_success_sent"),
        (PushSuccessGate, "resume"),
        (PushSuccessGate, "snapshot"),
        (PushSuccessGate, "is_paused"),
        (PushSuccessGate, "set_wake_scheduler"),
        (JobManager, "register_push_pause_checker"),
        (JobManager, "wake_scheduler"),
        (JobManager, "_is_push_paused"),
        (SseBroadcaster, "broadcast_push_gate_updated"),
    ]:
        if not hasattr(cls, attr):
            print(
                f"  [FAIL] {cls.__name__} thiếu attribute '{attr}'",
                flush=True,
            )
            sys.exit(1)

    # `push_gate` là instance attribute — check qua __init__ signature.
    init_params = inspect.signature(TelegramNotifier.__init__).parameters
    if "push_gate" not in init_params:
        print(
            "  [FAIL] TelegramNotifier.__init__ thiếu param 'push_gate'",
            flush=True,
        )
        sys.exit(1)

    print("  [PASS] mọi symbol mới đều có mặt", flush=True)


if __name__ == "__main__":
    print("=== SYNTAX check ===", flush=True)
    check_syntax()
    print("\n=== IMPORT check ===", flush=True)
    check_imports()
    print("\n=== SYMBOL check ===", flush=True)
    check_push_gate_symbols()
    print("\nAll checks OK.", flush=True)
