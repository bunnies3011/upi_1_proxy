#!/usr/bin/env bash
# ideal_qr_tool/dev.sh — chạy DUY NHẤT 1 process uvicorn phục vụ CẢ API + UI.
#
# Cách hoạt động:
#   1. Build Vue → `frontend/dist/` (chỉ khi chưa build hoặc user pass --rebuild).
#   2. Start uvicorn — main.py tự mount `frontend/dist/` tại `/` (StaticFiles
#      + SPA fallback), API vẫn ở `/api/*`. Cùng origin → không cần Vite dev
#      server + không cần proxy CORS.
#
# Usage:
#   ./dev.sh                        # dev: reload code Python, tự build FE nếu chưa có dist
#   ./dev.sh --rebuild              # ép build lại frontend
#   BACKEND_PORT=8080 ./dev.sh      # override port
#   IDEAL_QR_TOOL_DB_PATH=... ./dev.sh   # override DB (default runtime/ideal_qr_tool.db)
#
# Muốn frontend hot-reload (đang code Vue) → chạy song song `cd frontend && npm run dev`
# và mở http://localhost:5173 (Vite tự proxy /api → :8989 qua vite.config.ts).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT/backend"
FRONTEND_DIR="$ROOT/frontend"
FRONTEND_DIST="$FRONTEND_DIR/dist"

BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8989}"

REBUILD=0
for arg in "$@"; do
  case "$arg" in
    --rebuild) REBUILD=1 ;;
    *) ;;
  esac
done

if [ -t 1 ]; then
  C_RESET='\033[0m'; C_GREEN='\033[32m'; C_RED='\033[31m'; C_BOLD='\033[1m'
else
  C_RESET=''; C_GREEN=''; C_RED=''; C_BOLD=''
fi

log() { printf "%b[dev]%b %s\n" "$C_BOLD" "$C_RESET" "$*"; }
die() { printf "%b[dev][fatal]%b %s\n" "$C_RED" "$C_RESET" "$*" >&2; exit 1; }

# ---- Pre-flight ------------------------------------------------------------
[ -x "$BACKEND_DIR/.venv/bin/uvicorn" ] || die "Chưa có .venv backend — chạy: cd backend && python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'"

# ---- Build frontend nếu cần -----------------------------------------------
if [ $REBUILD -eq 1 ] || [ ! -f "$FRONTEND_DIST/index.html" ]; then
  [ -d "$FRONTEND_DIR/node_modules" ] || die "Chưa có node_modules — chạy: cd frontend && npm install"
  log "Build frontend → $FRONTEND_DIST"
  (
    cd "$FRONTEND_DIR"
    # Skip vue-tsc type-check (build chỉ cần vite bundle). Type-check chạy
    # riêng qua `npm run test` / IDE.
    exec npx vite build
  )
else
  log "Frontend dist đã có: $FRONTEND_DIST (dùng --rebuild để build lại)"
fi

# ---- Cleanup handler -------------------------------------------------------
BACK_PID=""

kill_by_port() {
  local port=$1
  local sig=${2:-TERM}
  local pids
  pids=$(lsof -ti :"$port" 2>/dev/null || true)
  [ -z "$pids" ] && return 0
  # shellcheck disable=SC2086
  kill -"$sig" $pids 2>/dev/null || true
}

cleanup() {
  local code=$?
  trap - INT TERM EXIT
  log "Đang tắt…"
  [ -n "$BACK_PID" ] && kill "$BACK_PID" 2>/dev/null || true
  kill_by_port "$BACKEND_PORT" TERM
  sleep 1
  kill_by_port "$BACKEND_PORT" KILL
  log "Đã dừng."
  exit $code
}
trap cleanup INT TERM EXIT

# ---- Start uvicorn ---------------------------------------------------------
log "Uvicorn → http://${BACKEND_HOST}:${BACKEND_PORT}  (UI + API cùng origin)"
(
  cd "$BACKEND_DIR"
  IDEAL_QR_TOOL_BIND_HOST="$BACKEND_HOST" \
    exec .venv/bin/uvicorn app.main:app \
      --host "$BACKEND_HOST" \
      --port "$BACKEND_PORT" \
      --reload \
      --reload-dir app 2>&1 | sed -u "s/^/$(printf '%b[APP]%b ' "$C_GREEN" "$C_RESET")/"
) &
BACK_PID=$!

log "PID=${BACK_PID} · Ctrl+C để dừng"

while kill -0 "$BACK_PID" 2>/dev/null; do
  sleep 1
done

exit 1
