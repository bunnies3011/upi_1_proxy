"""Chạy E2E 9 accounts iCloud thật, in log realtime, output kết quả per-account.

Sử dụng file accounts_e2e.txt đã có sẵn (9 accounts iCloud + password + TOTP).
Timeout tổng: 25 phút (9 × 3 phút = 27 phút, MVP không cần chính xác).
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

TIMEOUT_S = 1500  # 25 phút
LOG_PATH = Path("/tmp/ideal_qr_e2e_9.log")
ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS = ROOT / "runtime" / "accounts_e2e.txt"

if not ACCOUNTS.exists():
    print(f"[e2e] MISS: {ACCOUNTS}", flush=True)
    sys.exit(1)

lines = [l for l in ACCOUNTS.read_text().splitlines() if l.strip()]
print(f"[e2e] {len(lines)} accounts từ {ACCOUNTS.name}", flush=True)
print(f"[e2e] Timeout: {TIMEOUT_S}s ({TIMEOUT_S//60}phút)", flush=True)
print(f"[e2e] Log: {LOG_PATH}", flush=True)

cmd = [
    str(ROOT / ".venv" / "bin" / "python"),
    "-m", "app.cli",
    "--db-path", "runtime/e2e_test.db",
    "--format", "text",
    "run-batch",
    "runtime/accounts_e2e.txt",
]

t0 = time.time()

result_counters = {
    "qr_ready": 0,
    "error": 0,
    "stopped": 0,
    "other": 0,
}

with LOG_PATH.open("w", encoding="utf-8") as logf:
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    try:
        deadline = t0 + TIMEOUT_S
        while True:
            if proc.stdout is None:
                break
            line = proc.stdout.readline()
            if line:
                # Print realtime + append counters
                sys.stdout.write(line)
                sys.stdout.flush()
                logf.write(line)
                logf.flush()
                low = line.lower()
                if "qr_ready" in low or "artifact_path" in low:
                    result_counters["qr_ready"] += 1
                elif '"status": "error"' in low or "flow error" in low:
                    result_counters["error"] += 1
                elif '"status": "stopped"' in low:
                    result_counters["stopped"] += 1
            elif proc.poll() is not None:
                break
            if time.time() > deadline:
                print(f"\n[e2e] TIMEOUT sau {TIMEOUT_S}s — killing", flush=True)
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    time.sleep(3)
                    if proc.poll() is None:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                elapsed = time.time() - t0
                print(f"[e2e] elapsed={elapsed:.1f}s exit=124(TIMEOUT)", flush=True)
                sys.exit(124)

        proc.wait()
        elapsed = time.time() - t0
        print(f"\n[e2e] elapsed={elapsed:.1f}s exit={proc.returncode}", flush=True)
        print(f"[e2e] counters: {result_counters}", flush=True)

        # Đếm QR files trong runtime/qr/
        qr_dir = ROOT / "runtime" / "qr"
        pngs = sorted(qr_dir.glob("*.png"))
        print(f"[e2e] QR files: {len(pngs)}", flush=True)

        sys.exit(proc.returncode)
    except KeyboardInterrupt:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        print("\n[e2e] SIGINT", flush=True)
        sys.exit(130)
