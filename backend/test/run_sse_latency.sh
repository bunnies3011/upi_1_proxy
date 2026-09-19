#!/usr/bin/env bash
# Run test_sse_latency.py in foreground with unbuffered output for realtime log.
# Guard bằng `timeout 30s` (macOS: dùng gtimeout nếu có, fallback perl) để không stuck.
set -u

cd "$(dirname "$0")/.."

echo "[run_sse_latency] $(date '+%H:%M:%S') START pytest tests/integration/test_sse_latency.py"

if command -v gtimeout >/dev/null 2>&1; then
  gtimeout 30 .venv/bin/pytest -v -s --tb=short tests/integration/test_sse_latency.py
elif command -v timeout >/dev/null 2>&1; then
  timeout 30 .venv/bin/pytest -v -s --tb=short tests/integration/test_sse_latency.py
else
  # macOS fallback không có timeout binary
  perl -e '
    $SIG{ALRM} = sub { kill("KILL", -$pid) if $pid; die "TIMEOUT 30s\n" };
    alarm(30);
    $pid = fork();
    if ($pid == 0) { setpgrp(0,0); exec(@ARGV); }
    waitpid($pid, 0);
    exit($? >> 8);
  ' -- .venv/bin/pytest -v -s --tb=short tests/integration/test_sse_latency.py
fi
rc=$?
echo "[run_sse_latency] $(date '+%H:%M:%S') END rc=$rc"
exit $rc
