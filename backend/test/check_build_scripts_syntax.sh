#!/usr/bin/env bash
# Syntax check cho build.sh + scripts/release/setup.sh.
# Chay: bash backend/test/check_build_scripts_syntax.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

echo "[1/4] bash -n build.sh"
bash -n "$ROOT/build.sh" && echo "[PASS] build.sh syntax OK" || { echo "[FAIL] build.sh"; exit 1; }

echo "[2/4] bash -n scripts/release/setup.sh"
bash -n "$ROOT/scripts/release/setup.sh" && echo "[PASS] release setup.sh syntax OK" || { echo "[FAIL] release setup.sh"; exit 1; }

echo "[3/4] check build.bat exists + markers"
if [ -f "$ROOT/build.bat" ] && grep -q '^@echo off' "$ROOT/build.bat" && grep -q 'exit /b' "$ROOT/build.bat"; then
  echo "[PASS] build.bat present ($(wc -l < "$ROOT/build.bat") lines)"
else
  echo "[FAIL] build.bat missing or malformed"
  exit 1
fi

echo "[4/4] check release setup.bat exists + markers"
if [ -f "$ROOT/scripts/release/setup.bat" ] && grep -q '^@echo off' "$ROOT/scripts/release/setup.bat" && grep -q 'exit /b' "$ROOT/scripts/release/setup.bat"; then
  echo "[PASS] release setup.bat present ($(wc -l < "$ROOT/scripts/release/setup.bat") lines)"
else
  echo "[FAIL] release setup.bat missing or malformed"
  exit 1
fi

echo ""
echo "ALL PASS"
