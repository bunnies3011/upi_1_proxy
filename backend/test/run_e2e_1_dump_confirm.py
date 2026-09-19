"""Chạy 1 account, dump confirm body ra /tmp/ideal_confirm_dump/.

Sau khi kết thúc, so sánh body confirm (form-urlencoded) với HAR event 12.
"""
from __future__ import annotations
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DUMP_DIR = Path("/tmp/ideal_confirm_dump")
if DUMP_DIR.exists():
    shutil.rmtree(DUMP_DIR)
DUMP_DIR.mkdir(parents=True)

# Prepare accounts_smoke.txt with just 1 account
smoke_acc = ROOT / "runtime" / "accounts_smoke.txt"
e2e_acc = ROOT / "runtime" / "accounts_e2e.txt"
first_line = e2e_acc.read_text().splitlines()[0].strip()
smoke_acc.write_text(first_line + "\n")
print(f"[dump] Using account: {first_line.split('|')[0]}", flush=True)
print(f"[dump] DUMP_DIR: {DUMP_DIR}", flush=True)

env = os.environ.copy()
env["IDEAL_DEBUG_DUMP_CONFIRM"] = str(DUMP_DIR)

cmd = [
    str(ROOT / ".venv" / "bin" / "python"),
    "-m", "app.cli",
    "--db-path", "runtime/e2e_test.db",
    "--format", "text",
    "run-batch",
    "runtime/accounts_smoke.txt",
]

t0 = time.time()
proc = subprocess.Popen(
    cmd,
    cwd=str(ROOT),
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    bufsize=1,
    start_new_session=True,
    env=env,
)

TIMEOUT_S = 180  # 3 phút đủ để confirm

try:
    deadline = t0 + TIMEOUT_S
    while True:
        if proc.stdout is None:
            break
        line = proc.stdout.readline()
        if line:
            sys.stdout.write(line)
            sys.stdout.flush()
        elif proc.poll() is not None:
            break
        if time.time() > deadline:
            print(f"\n[dump] TIMEOUT {TIMEOUT_S}s — killing", flush=True)
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                time.sleep(2)
                if proc.poll() is None:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
            break
    proc.wait()
finally:
    dumps = sorted(DUMP_DIR.glob("*.txt"))
    print(f"\n[dump] {len(dumps)} confirm body files:", flush=True)
    for d in dumps:
        print(f"  {d.name}  ({d.stat().st_size} bytes)", flush=True)
