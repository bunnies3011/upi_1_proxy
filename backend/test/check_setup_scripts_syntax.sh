#!/usr/bin/env bash
# Syntax check cho setup.sh (bash -n) va grep ki tu cam trong setup.bat.
# Chay: bash backend/test/check_setup_scripts_syntax.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SH="$ROOT/setup.sh"
BAT="$ROOT/setup.bat"

echo "[1/3] bash -n $SH"
if bash -n "$SH"; then
  echo "[PASS] setup.sh syntax OK"
else
  echo "[FAIL] setup.sh syntax error"
  exit 1
fi

echo "[2/3] check setup.bat exists"
if [ -f "$BAT" ]; then
  echo "[PASS] setup.bat present ($(wc -l < "$BAT") lines)"
else
  echo "[FAIL] setup.bat not found"
  exit 1
fi

echo "[3/3] setup.bat: kiem tra co @echo off + exit /b"
if grep -q '^@echo off' "$BAT" && grep -q 'exit /b' "$BAT"; then
  echo "[PASS] setup.bat has @echo off + exit /b markers"
else
  echo "[FAIL] setup.bat missing @echo off or exit /b"
  exit 1
fi

echo ""
echo "ALL PASS"
