#!/usr/bin/env python3
"""Debug: chạy test_auth.py với subprocess và log ra chi tiết."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTEST = ROOT / ".venv" / "bin" / "python"

print("Starting test_auth.py with 30s timeout...", flush=True)
t0 = time.time()
try:
    proc = subprocess.run(
        [
            str(PYTEST),
            "-m", "pytest",
            "tests/unit/test_auth.py",
            "-v",
            "--tb=long",
            "-p", "no:cacheprovider",
            "-s",  # không capture stdout
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    elapsed = time.time() - t0
    print(f"Elapsed: {elapsed:.2f}s, rc={proc.returncode}", flush=True)
    print("STDOUT:", flush=True)
    print(proc.stdout, flush=True)
    print("STDERR:", flush=True)
    print(proc.stderr, flush=True)
except subprocess.TimeoutExpired as exc:
    elapsed = time.time() - t0
    print(f"TIMEOUT after {elapsed:.2f}s", flush=True)
    print("PARTIAL STDOUT:", flush=True)
    print(exc.stdout or "<empty>", flush=True)
    print("PARTIAL STDERR:", flush=True)
    print(exc.stderr or "<empty>", flush=True)
