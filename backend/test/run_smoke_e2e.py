#!/usr/bin/env python3
"""Smoke E2E: chạy 1 account thật qua CLI, log realtime, timebox 5 phút.

Chạy `python -m app.cli run-batch runtime/accounts_smoke.txt` với subprocess
timeout. Log stream tới stdout của test/run_smoke_e2e.py (agent đọc qua
get_process_output) và tee tới `/tmp/ideal_qr_smoke_e2e.log`.

Fail-fast: nếu vượt timeout → kill process group, in dòng `[smoke] TIMEOUT`
để agent nhận biết. Exit code:
  0 = job qr_ready (all pass)
  1 = config error
  2 = job error/stopped
  124 = timeout
  130 = SIGINT
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

TIMEOUT_S = 300
LOG_PATH = Path("/tmp/ideal_qr_smoke_e2e.log")
ROOT = Path(__file__).resolve().parents[1]

print(f"[smoke] Starting CLI run-batch with 1 account, timeout {TIMEOUT_S}s", flush=True)
print(f"[smoke] DB: runtime/e2e_test.db", flush=True)
print(f"[smoke] Account file: runtime/accounts_smoke.txt", flush=True)
print(f"[smoke] Log: {LOG_PATH}", flush=True)
print("", flush=True)

cmd = [
    str(ROOT / ".venv" / "bin" / "python"),
    "-m", "app.cli",
    "--db-path", "runtime/e2e_test.db",
    "--format", "text",
    "run-batch",
    "runtime/accounts_smoke.txt",
]

t0 = time.time()

with LOG_PATH.open("w", encoding="utf-8") as logf:
    # start_new_session=True để có process group riêng, dễ kill toàn bộ subtree
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
            # Peek stdout với timeout ngắn
            if proc.stdout is None:
                break
            line = proc.stdout.readline()
            if line:
                print(line, end="", flush=True)
                logf.write(line)
                logf.flush()
            elif proc.poll() is not None:
                # EOF + process exited
                break

            if time.time() > deadline:
                print(f"\n[smoke] TIMEOUT sau {TIMEOUT_S}s — killing process", flush=True)
                # Kill toàn process group
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    time.sleep(2)
                    if proc.poll() is None:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                elapsed = time.time() - t0
                print(f"[smoke] elapsed={elapsed:.1f}s exit=124(TIMEOUT)", flush=True)
                sys.exit(124)

        proc.wait()
        elapsed = time.time() - t0
        print(f"\n[smoke] elapsed={elapsed:.1f}s exit={proc.returncode}", flush=True)
        sys.exit(proc.returncode)
    except KeyboardInterrupt:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        print("\n[smoke] Interrupted by SIGINT", flush=True)
        sys.exit(130)
