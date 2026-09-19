"""Smoke check flow pause: cancellation_token.pause() + JobResult.pause_requested.

Verify các thay đổi vừa apply:
- `SimpleCancellationToken` có method `pause`/`is_paused`.
- `JobResult` có field `pause_requested`, default False.
- `PushSuccessGate.set_pause_running_push_jobs_callback` tồn tại.
- `JobManager.signal_pause_running_push_jobs` tồn tại.
- `JobManager._handle_pause_requested` tồn tại.
- `flow.py` có helper `_check_stop_or_pause`.

In tiến trình từng bước để debug được ngay chỗ nào fail.
"""
from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))


def _log(status: str, tc_id: str, desc: str, detail: str = "") -> None:
    line = f"[{status}] {tc_id} — {desc}"
    if detail:
        line += f" :: {detail}"
    print(line, flush=True)


def main() -> int:
    failed = 0

    # TC-01: SimpleCancellationToken có pause + is_paused
    print("[1/6] SimpleCancellationToken pause API ...", flush=True)
    from app.core.payment_flow import SimpleCancellationToken, JobResult, JobStatus

    tok = SimpleCancellationToken()
    if not hasattr(tok, "pause") or not hasattr(tok, "is_paused"):
        _log("FAIL", "TC-01", "thiếu method pause/is_paused")
        return 1
    if tok.is_paused():
        _log("FAIL", "TC-01", "is_paused() phải False khi mới tạo")
        return 1
    tok.pause()
    if not tok.is_paused():
        _log("FAIL", "TC-01", "pause() không set is_paused=True")
        return 1
    # cancel + pause độc lập
    tok.cancel()
    if not tok.is_cancelled() or not tok.is_paused():
        _log("FAIL", "TC-01", "cancel + pause phải cùng True")
        return 1
    _log("PASS", "TC-01", "SimpleCancellationToken pause API đúng")

    # TC-02: JobResult.pause_requested default False
    print("[2/6] JobResult pause_requested field ...", flush=True)
    r_default = JobResult(status=JobStatus.QR_READY)
    if r_default.pause_requested is not False:
        _log("FAIL", "TC-02", "default pause_requested phải False", detail=str(r_default))
        return 1
    r_paused = JobResult(status=JobStatus.STOPPED, pause_requested=True)
    if r_paused.pause_requested is not True:
        _log("FAIL", "TC-02", "explicit pause_requested=True không set")
        return 1
    _log("PASS", "TC-02", "JobResult.pause_requested default False, explicit True OK")

    # TC-03: PushSuccessGate có set_pause_running_push_jobs_callback
    print("[3/6] PushSuccessGate pause callback API ...", flush=True)
    from app.notifiers.telegram.push_gate import PushSuccessGate

    if not hasattr(PushSuccessGate, "set_pause_running_push_jobs_callback"):
        _log("FAIL", "TC-03", "thiếu set_pause_running_push_jobs_callback")
        return 1
    _log("PASS", "TC-03", "PushSuccessGate.set_pause_running_push_jobs_callback OK")

    # TC-04: JobManager có signal_pause_running_push_jobs + _handle_pause_requested
    print("[4/6] JobManager pause hooks ...", flush=True)
    from app.core.job_manager import JobManager

    for name in ("signal_pause_running_push_jobs", "_handle_pause_requested"):
        if not hasattr(JobManager, name):
            _log("FAIL", "TC-04", f"JobManager thiếu {name}")
            return 1
    _log("PASS", "TC-04", "JobManager có signal_pause_running_push_jobs + _handle_pause_requested")

    # TC-05: flow.py có _check_stop_or_pause + _PAUSED_RESULT
    print("[5/6] iDEAL flow.py helpers ...", flush=True)
    from app.payments.ideal import flow as ideal_flow

    if not hasattr(ideal_flow, "_check_stop_or_pause"):
        _log("FAIL", "TC-05", "flow.py thiếu _check_stop_or_pause")
        return 1
    if not hasattr(ideal_flow, "_PAUSED_RESULT"):
        _log("FAIL", "TC-05", "flow.py thiếu _PAUSED_RESULT")
        return 1
    paused_result = ideal_flow._PAUSED_RESULT
    if paused_result.status is not JobStatus.STOPPED:
        _log("FAIL", "TC-05", "_PAUSED_RESULT status phải STOPPED", detail=str(paused_result.status))
        return 1
    if not paused_result.pause_requested:
        _log("FAIL", "TC-05", "_PAUSED_RESULT.pause_requested phải True")
        return 1
    _log("PASS", "TC-05", "iDEAL flow helpers OK")

    # TC-06: verify tất cả check trong run() từ step 3 đã dùng _check_stop_or_pause
    print("[6/6] Verify run() dùng _check_stop_or_pause từ step 3+ ...", flush=True)
    src = (BACKEND_DIR / "app/payments/ideal/flow.py").read_text(encoding="utf-8")
    # Đếm số lần dùng _check_stop_or_pause vs check thô còn lại.
    n_pause_helper = src.count("_check_stop_or_pause(job)")
    # Step 1 (parse) + Step 2 (login) giữ nguyên `is_cancelled` — 2 chỗ.
    # Nhưng dòng "if job.cancellation_token.is_cancelled():" cũng xuất
    # hiện trong body của 2 helper (`_check_stop` + `_check_stop_or_pause`) → 2 lần.
    # → tổng expected là 4 lần trong file.
    n_direct_check = src.count("if job.cancellation_token.is_cancelled():")
    print(f"    _check_stop_or_pause(job) calls: {n_pause_helper}", flush=True)
    print(f"    if job.cancellation_token.is_cancelled(): occurrences: {n_direct_check}", flush=True)
    if n_pause_helper < 8:  # 10 step (3..12) trừ inner loop
        _log("FAIL", "TC-06", f"số lần _check_stop_or_pause quá ít ({n_pause_helper})")
        return 1
    if n_direct_check > 4:
        _log(
            "FAIL",
            "TC-06",
            f"còn {n_direct_check} check thô is_cancelled() (expected ≤ 4: 2 body helper + step 1/2)",
        )
        return 1
    _log("PASS", "TC-06", f"{n_pause_helper} lần dùng helper, {n_direct_check} check thô (đúng)")

    print("\nAll pause-flow checks OK.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
