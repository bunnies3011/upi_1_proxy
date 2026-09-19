#!/usr/bin/env bash
# Chạy 3 account qua CLI tuần tự — check anti-fraud trigger rate.
# Mỗi account 1 file log riêng để so sánh.
#
# Chạy: ./test/smoke_cli_batch.sh từ backend/
set -u

ACCOUNTS=(
  "zealous-flint49+e4l26v@icloud.com|Anczz123456789@|JGRCEBRM7CFUKKL2NRHP2EE2DZGWFUV2"
  "spikier_gaze_54+b3ejl01@icloud.com|Anczz123456789@|CPN4HIYKEKCTFACTI7OYUWYNMXQEKFSG"
  "roves_vestige2i+h5l0kw0@icloud.com|Anczz123456789@|7U77ARGBEGZDYUXCQ3OSWVQZOLV6CVTB"
)

mkdir -p test/logs
PYTHON="./.venv/bin/python3"

for i in "${!ACCOUNTS[@]}"; do
  ACC="${ACCOUNTS[$i]}"
  EMAIL="$(echo "$ACC" | cut -d'|' -f1)"
  LOG="test/logs/cli_acc_${i}.log"
  echo ""
  echo "[$((i+1))/${#ACCOUNTS[@]}] BEGIN — $EMAIL"
  START=$(date +%s)

  "$PYTHON" -m app.cli.main run "$ACC" > "$LOG" 2>&1
  RC=$?

  END=$(date +%s)
  DUR=$((END - START))

  # Extract final line có STATUS
  FINAL=$(grep -E "^\|.*\|.*\|" "$LOG" | tail -1 || true)
  ERR_CODE=$(grep -oE "error_code=[a-z_]+" "$LOG" | tail -1 || true)

  if [ "$RC" -eq 0 ]; then
    echo "[$((i+1))/${#ACCOUNTS[@]}] [PASS] $EMAIL (exit=$RC, ${DUR}s) — $ERR_CODE"
  else
    echo "[$((i+1))/${#ACCOUNTS[@]}] [FAIL] $EMAIL (exit=$RC, ${DUR}s) — $ERR_CODE"
  fi
  echo "         log → $LOG"
done

echo ""
echo "== Done =="
