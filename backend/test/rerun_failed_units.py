#!/usr/bin/env python3
"""Rerun 6 unit test file bị TIMEOUT/rc=-9 ở lần 1 với timeout 180s."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTEST = ROOT / ".venv" / "bin" / "python"

FAILED_FILES = [
    "tests/unit/test_auth.py",
    "tests/unit/test_no_request_after_qr_ready.py",
    "tests/unit/test_no_stripe_consumers_lookup.py",
    "tests/unit/test_settings_store.py",
    "tests/unit/test_settings_whitelist_core_keys.py",
    "tests/unit/test_settings_whitelist_full.py",
]

print("=" * 70, flush=True)
print(f"RERUN {len(FAILED_FILES)} failed unit files with 180s timeout", flush=True)
print("=" * 70, flush=True)

results = []
for idx, rel in enumerate(FAILED_FILES, start=1):
    t0 = time.time()
    try:
        proc = subprocess.run(
            [
                str(PYTEST),
                "-m", "pytest",
                rel,
                "--tb=short",
                "-v",
                "-p", "no:cacheprovider",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=180,
        )
        elapsed = time.time() - t0
        output = proc.stdout + proc.stderr
        # Trích summary
        summary = ""
        for line in reversed(output.splitlines()):
            if "passed" in line or "failed" in line or "error" in line.lower():
                summary = line.strip()
                break
        status = "PASS" if proc.returncode == 0 else f"FAIL rc={proc.returncode}"
        print(
            f"[{idx}/{len(FAILED_FILES)}] [{status}] {rel:<60s} ({elapsed:6.2f}s) :: {summary}",
            flush=True,
        )
        results.append((rel, proc.returncode, summary, output if proc.returncode != 0 else ""))
    except subprocess.TimeoutExpired:
        elapsed = time.time() - t0
        print(
            f"[{idx}/{len(FAILED_FILES)}] [TIMEOUT] {rel:<60s} ({elapsed:6.2f}s)",
            flush=True,
        )
        results.append((rel, -1, "TIMEOUT 180s", ""))

print("", flush=True)
print("=" * 70, flush=True)
passed = sum(1 for _, rc, _, _ in results if rc == 0)
print(f"RERUN SUMMARY: {passed}/{len(FAILED_FILES)} passed", flush=True)
print("=" * 70, flush=True)

# Dump chi tiết fail
for rel, rc, summary, output in results:
    if rc == 0:
        continue
    print(f"\n---- {rel} (rc={rc}) ----", flush=True)
    for line in output.splitlines()[-50:]:
        print(line, flush=True)
    print(f"---- END {rel} ----", flush=True)
