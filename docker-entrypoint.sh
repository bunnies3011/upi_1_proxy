#!/usr/bin/env bash
# ideal_qr_tool/docker-entrypoint.sh — entrypoint container.
#
# 1. Đảm bảo runtime dirs tồn tại (session_cache/, qr/) — cần thiết khi
#    volume mount `backend/runtime` từ host lần đầu (dir rỗng).
# 2. Seed Settings tối thiểu qua `seed_settings.py` — idempotent, tự SKIP
#    nếu DB đã có `ideal.default_issuer` + `ideal.device_profiles` (VD sau
#    khi container restart, volume đã persist DB từ lần chạy trước).
# 3. exec CMD (mặc định `uvicorn app.main:app --host 0.0.0.0 --port 8989`)
#    — PID 1 là uvicorn, nhận đúng SIGTERM từ `docker stop`.
set -euo pipefail

mkdir -p runtime/session_cache runtime/qr

echo "[entrypoint] seeding default settings…"
python seed_settings.py

echo "[entrypoint] starting: $*"
exec "$@"
