#!/usr/bin/env python3
"""Runner pytest property suite với per-file timeout 120s (hypothesis chậm hơn)."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROP_DIR = ROOT / "tests" / "property"
PYTEST = ROOT / ".venv" / "bin" / "python"

files = sorted(f for f in PROP_DIR.glob("test_*.py") if f.is_file())

print("=" * 70, flush=True)
print(f"PYTEST PROPERTY SUITE — {len(files)} test files (timeout 120s each)", flush=True)
print("=" * 70, flush=True)

total = len(files)
passed_count = 0
failed_files: list[tuple[str, str]] = []
started = time.time()

for idx, f in enumerate(files, start=1):
    rel = f.relative_to(ROOT).as_posix()
    t0 = time.time()
    try:
        proc = subprocess.run(
            [
                str(PYTEST),
                "-m", "pytest",
                str(f),
                "--tb=short",
                "-q",
                "-p", "no:cacheprovider",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        elapsed = time.time() - t0
        output = proc.stdout + proc.stderr
        summary_line = ""
        for line in reversed(output.splitlines()):
            if "passed" in line or "failed" in line or "error" in line.lower():
                summary_line = line.strip()
                break
        if proc.returncode == 0:
            passed_count += 1
            print(
                f"[{idx:3d}/{total}] [PASS] {rel:<62s} ({elapsed:6.2f}s) :: {summary_line}",
                flush=True,
            )
        else:
            failed_files.append((rel, output))
            print(
                f"[{idx:3d}/{total}] [FAIL] {rel:<62s} ({elapsed:6.2f}s) rc={proc.returncode} :: {summary_line}",
                flush=True,
            )
    except subprocess.TimeoutExpired:
        elapsed = time.time() - t0
        failed_files.append((rel, "[TIMEOUT after 120s]"))
        print(
            f"[{idx:3d}/{total}] [TIME] {rel:<62s} ({elapsed:6.2f}s) :: TIMEOUT",
            flush=True,
        )

print("", flush=True)
print("=" * 70, flush=True)
print(
    f"PROPERTY SUMMARY: total={total} passed={passed_count} "
    f"failed={len(failed_files)} elapsed={time.time() - started:.1f}s",
    flush=True,
)
print("=" * 70, flush=True)

if failed_files:
    print("\nFAILED DETAIL:\n", flush=True)
    for rel, output in failed_files:
        print(f"---- {rel} ----", flush=True)
        lines = output.splitlines() if isinstance(output, str) else []
        for line in lines[-40:]:
            print(line, flush=True)
        print(f"---- END {rel} ----\n", flush=True)
    sys.exit(1)

sys.exit(0)
