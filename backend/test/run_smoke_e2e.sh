#!/usr/bin/env bash
# Smoke E2E: chạy 1 account thật qua CLI, log realtime.
# Timebox 5 phút — nếu quá → kill process.
set -u
cd "$(dirname "$0")/.."

TIMEOUT_S=300
LOG=/tmp/ideal_qr_smoke_e2e.log

echo "[smoke] Starting CLI run-batch with 1 account, timeout ${TIMEOUT_S}s"
echo "[smoke] DB: runtime/e2e_test.db"
echo "[smoke] Account file: runtime/accounts_smoke.txt"
echo "[smoke] Log: $LOG"
echo ""

# Chạy CLI với timeout, output realtime tới stdout + tee tới log
exec timeout ${TIMEOUT_S} .venv/bin/python -m app.cli \
    --db-path runtime/e2e_test.db \
    --format text \
    run-batch runtime/accounts_smoke.txt \
    2>&1 | tee "$LOG"
