#!/usr/bin/env bash
# ideal_qr_tool/docker.sh — build/run tool qua Docker (macOS/Linux).
#
# Container hoá để môi trường chạy đồng nhất giữa các OS (macOS/Windows/
# Linux) — tránh lệch kết quả runtime (VD: 403 khi curl_cffi TLS fingerprint
# build khác nhau giữa host OS) do luôn chạy trong 1 base image Linux cố
# định (python:3.13-slim).
#
# Usage:
#   ./docker.sh up               # build (nếu cần) + start container (detached)
#   ./docker.sh up --build       # ép build lại image rồi start
#   ./docker.sh down             # stop + remove container
#   ./docker.sh restart          # down rồi up lại
#   ./docker.sh logs             # follow log realtime
#   ./docker.sh build            # chỉ build image, không start
#   ./docker.sh ps               # trạng thái container
#   ./docker.sh shell            # mở bash trong container đang chạy
#
# Env override (giống setup.sh/production.sh):
#   BACKEND_PORT=9000 ./docker.sh up
#   IDEAL_QR_TOOL_DB_PATH=runtime/custom.db ./docker.sh up
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [ -t 1 ]; then
  C_RESET='\033[0m'; C_BOLD='\033[1m'; C_RED='\033[31m'; C_GREEN='\033[32m'; C_CYAN='\033[36m'
else
  C_RESET=''; C_BOLD=''; C_RED=''; C_GREEN=''; C_CYAN=''
fi
log()  { printf "%b[docker]%b %s\n" "$C_CYAN$C_BOLD" "$C_RESET" "$*"; }
die()  { printf "%b[docker][fatal]%b %s\n" "$C_RED$C_BOLD" "$C_RESET" "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "Docker không có trên PATH. Cài Docker Desktop: https://www.docker.com/products/docker-desktop/"

# Detect docker compose v2 plugin (`docker compose`) vs standalone v1
# (`docker-compose`) — ưu tiên v2 vì đã đóng gói sẵn với Docker Desktop.
COMPOSE=()
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  die "Không tìm thấy 'docker compose' hay 'docker-compose'. Cập nhật Docker Desktop lên bản mới."
fi

CMD="${1:-}"
shift || true

case "$CMD" in
  up)
    log "Build + start container (detached)…"
    "${COMPOSE[@]}" up -d "$@"
    log "Container đang chạy → http://127.0.0.1:${BACKEND_PORT:-8989}/"
    log "Xem log: ./docker.sh logs"
    ;;
  down)
    log "Stop + remove container…"
    "${COMPOSE[@]}" down "$@"
    ;;
  restart)
    log "Restart container…"
    "${COMPOSE[@]}" down
    "${COMPOSE[@]}" up -d "$@"
    log "Container đang chạy → http://127.0.0.1:${BACKEND_PORT:-8989}/"
    ;;
  build)
    log "Build image (không start)…"
    "${COMPOSE[@]}" build "$@"
    ;;
  logs)
    "${COMPOSE[@]}" logs -f --tail=200 "$@"
    ;;
  ps)
    "${COMPOSE[@]}" ps "$@"
    ;;
  shell)
    log "Mở bash trong container ideal-qr-tool…"
    docker exec -it ideal-qr-tool bash
    ;;
  ""|-h|--help)
    cat <<EOF
Usage: ./docker.sh <command> [args]

Commands:
  up [--build]     build (nếu cần) + start container (detached)
  down             stop + remove container
  restart          down rồi up lại
  build            chỉ build image, không start
  logs             follow log realtime (Ctrl+C để thoát)
  ps               trạng thái container
  shell            mở bash trong container đang chạy

Env override:
  BACKEND_PORT=9000 ./docker.sh up
  IDEAL_QR_TOOL_DB_PATH=runtime/custom.db ./docker.sh up
EOF
    ;;
  *)
    die "Unknown command: $CMD (xem: ./docker.sh --help)"
    ;;
esac
