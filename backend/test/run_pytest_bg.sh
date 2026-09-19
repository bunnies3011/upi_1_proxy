#!/bin/bash
# Wrapper để pytest chạy nền và ghi log ra file
cd /Volumes/SSD/Developments/TOOL/gpt_signup_hybrid/ideal_qr_tool/backend
rm -f /tmp/task33_out.log
touch /tmp/task33_out.log
nohup .venv/bin/python -m pytest "$@" > /tmp/task33_out.log 2>&1 &
PID=$!
echo "PID=$PID"
# Wait up to 90s
for i in $(seq 1 180); do
  if ! kill -0 $PID 2>/dev/null; then
    echo "DONE_AT_ITER=$i"
    break
  fi
  sleep 0.5
done
if kill -0 $PID 2>/dev/null; then
  echo "STILL_RUNNING_KILLED"
  kill -9 $PID
fi
tail -n 80 /tmp/task33_out.log
