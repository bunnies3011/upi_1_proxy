#!/usr/bin/env bash
# ideal_qr_tool — 1 lệnh setup full + start (macOS/Linux).
#
# Không quan tâm máy đang có sẵn gì: script sẽ
#   1. Detect / auto-install Python (>= 3.11) qua Homebrew (macOS)
#      hoặc apt/dnf (Linux).
#   2. Detect / auto-install Node.js (>= 18) qua Homebrew hoặc apt/dnf.
#   3. Tạo backend/.venv + `pip install -e '.[dev]'`.
#   4. `npm install` trong frontend/.
#   5. Build frontend (bypass vue-tsc — dev mode, giống dev.sh).
#   6. Seed Settings tối thiểu (default_issuer + 1 device profile NL).
#   7. Exec uvicorn — serve cả UI Vue + REST API cùng origin.
#
# Usage:
#   bash setup.sh
#   bash setup.sh --port 9000
#   bash setup.sh --host 0.0.0.0 --port 8080
#   bash setup.sh --rebuild-frontend    # ép build lại FE
#   bash setup.sh --skip-run            # chỉ setup, không chạy
#   bash setup.sh --db /tmp/test.db     # override DB path
#
# Env override tương đương:
#   BACKEND_HOST, BACKEND_PORT, IDEAL_QR_TOOL_DB_PATH, PYTHON
#
# Fail-fast: mọi bước lỗi → exit non-zero với thông báo rõ ràng.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"
FRONTEND_DIST="$FRONTEND_DIR/dist"

BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8989}"
IDEAL_QR_TOOL_DB_PATH="${IDEAL_QR_TOOL_DB_PATH:-}"
# Track xem env DB có do user set từ ngoài không (để phân biệt với auto-derive theo port).
_DB_FROM_ENV=0
[ -n "${IDEAL_QR_TOOL_DB_PATH:-}" ] && _DB_FROM_ENV=1
# Track xem --db có được truyền qua CLI không.
_DB_FROM_CLI=0

REBUILD_FE=0
SKIP_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --port)              BACKEND_PORT="$2"; shift 2 ;;
    --port=*)            BACKEND_PORT="${1#*=}"; shift ;;
    --host)              BACKEND_HOST="$2"; shift 2 ;;
    --host=*)            BACKEND_HOST="${1#*=}"; shift ;;
    --db)                IDEAL_QR_TOOL_DB_PATH="$2"; _DB_FROM_CLI=1; shift 2 ;;
    --db=*)              IDEAL_QR_TOOL_DB_PATH="${1#*=}"; _DB_FROM_CLI=1; shift ;;
    --rebuild-frontend)  REBUILD_FE=1; shift ;;
    --skip-run)          SKIP_RUN=1; shift ;;
    -h|--help)
      cat <<EOF
Usage: bash setup.sh [OPTIONS]

Options:
  --host H              bind host (default 127.0.0.1)
  --port N              bind port (default 8989)
  --db PATH             SQLite DB path (default: backend/runtime/ideal_qr_tool.db khi
                        --port=8989; các port khác auto dùng ideal_qr_tool_<PORT>.db
                        để mỗi instance có DB riêng)
  --rebuild-frontend    ép build lại frontend/dist
  --skip-run            chỉ setup xong rồi thoát, KHÔNG start uvicorn
  -h, --help            in help

Env override: BACKEND_HOST, BACKEND_PORT, IDEAL_QR_TOOL_DB_PATH, PYTHON
EOF
      exit 0 ;;
    *) echo "ERROR: unknown arg: $1 (xem: bash setup.sh --help)" >&2; exit 1 ;;
  esac
done

# ─── Per-port DB isolation ──────────────────────────────────────────────
# Nếu user không tự set DB (qua --db hoặc env IDEAL_QR_TOOL_DB_PATH) VÀ port
# khác default 8989 → auto derive DB path theo port. Mỗi port một DB riêng
# để tránh 2 instance khác port share chung state (settings, jobs, log…).
if [ "$_DB_FROM_ENV" -eq 0 ] && [ "$_DB_FROM_CLI" -eq 0 ] && [ "$BACKEND_PORT" != "8989" ]; then
  IDEAL_QR_TOOL_DB_PATH="$BACKEND_DIR/runtime/ideal_qr_tool_${BACKEND_PORT}.db"
fi

# ─── Color helpers ──────────────────────────────────────────────────────
if [ -t 1 ]; then
  C_RESET='\033[0m'; C_BOLD='\033[1m'
  C_RED='\033[31m'; C_GREEN='\033[32m'; C_YELLOW='\033[33m'; C_CYAN='\033[36m'
else
  C_RESET=''; C_BOLD=''; C_RED=''; C_GREEN=''; C_YELLOW=''; C_CYAN=''
fi
log()  { printf "%b[setup]%b %s\n" "$C_CYAN$C_BOLD" "$C_RESET" "$*"; }
warn() { printf "%b[setup][warn]%b %s\n" "$C_YELLOW$C_BOLD" "$C_RESET" "$*"; }
die()  { printf "%b[setup][fatal]%b %s\n" "$C_RED$C_BOLD" "$C_RESET" "$*" >&2; exit 1; }

# ─── Detect OS ──────────────────────────────────────────────────────────
OS_KIND=""
case "$(uname -s)" in
  Darwin) OS_KIND="macos" ;;
  Linux)  OS_KIND="linux" ;;
  *)      OS_KIND="unknown" ;;
esac

echo ""
printf "%b═══════════════════════════════════════════════════════════%b\n" "$C_BOLD" "$C_RESET"
printf "%b  ideal_qr_tool — auto setup + start (%s)%b\n" "$C_BOLD" "$OS_KIND" "$C_RESET"
printf "%b  root: %s%b\n" "$C_BOLD" "$ROOT_DIR" "$C_RESET"
printf "%b═══════════════════════════════════════════════════════════%b\n" "$C_BOLD" "$C_RESET"
echo ""

# ─── [1/7] Detect / install Python 3.11+ ────────────────────────────────
log "[1/7] Detect Python (>= 3.11)…"

_py_version_check() {
  # Args: $1 = python binary. Return 0 nếu >= 3.11, else 1.
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

PY_BIN="${PYTHON:-}"
if [ -n "$PY_BIN" ]; then
  _py_version_check "$PY_BIN" || die "PYTHON=$PY_BIN nhưng < 3.11. Set PYTHON tới interpreter >= 3.11 hoặc unset để auto-detect."
else
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && _py_version_check "$candidate"; then
      PY_BIN="$(command -v "$candidate")"
      break
    fi
  done
fi

if [ -z "$PY_BIN" ]; then
  warn "Không tìm thấy Python >= 3.11. Auto-install…"
  case "$OS_KIND" in
    macos)
      if ! command -v brew >/dev/null 2>&1; then
        die "macOS thiếu Homebrew. Cài trước: /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\" rồi chạy lại."
      fi
      log "brew install python@3.13"
      brew install python@3.13
      PY_BIN="$(brew --prefix python@3.13)/bin/python3.13"
      [ -x "$PY_BIN" ] || PY_BIN="$(command -v python3.13 || true)"
      ;;
    linux)
      if command -v apt-get >/dev/null 2>&1; then
        log "sudo apt-get install -y python3 python3-venv python3-pip"
        sudo apt-get update -y
        sudo apt-get install -y python3 python3-venv python3-pip
      elif command -v dnf >/dev/null 2>&1; then
        log "sudo dnf install -y python3 python3-pip"
        sudo dnf install -y python3 python3-pip
      elif command -v pacman >/dev/null 2>&1; then
        log "sudo pacman -Sy --noconfirm python python-pip"
        sudo pacman -Sy --noconfirm python python-pip
      else
        die "Distro không nhận diện (không có apt/dnf/pacman). Cài tay Python >= 3.11 rồi chạy lại."
      fi
      PY_BIN="$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3 || true)"
      ;;
    *)
      die "OS không hỗ trợ auto-install ($OS_KIND). Cài Python >= 3.11 thủ công rồi set PYTHON=/path/to/python3 chạy lại."
      ;;
  esac
  [ -n "$PY_BIN" ] && _py_version_check "$PY_BIN" || die "Auto-install xong nhưng vẫn không có Python >= 3.11."
fi

PY_VERSION="$("$PY_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"
log "python: $PY_BIN ($PY_VERSION)"

# ─── [2/7] Detect / install Node.js 18+ ─────────────────────────────────
log "[2/7] Detect Node.js (>= 18)…"

_node_version_check() {
  # Args: $1 = node binary. Return 0 nếu major >= 18.
  local major
  major="$("$1" -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
  [ "$major" -ge 18 ] 2>/dev/null
}

NODE_BIN=""
if command -v node >/dev/null 2>&1 && _node_version_check node; then
  NODE_BIN="$(command -v node)"
fi

if [ -z "$NODE_BIN" ]; then
  warn "Không tìm thấy Node.js >= 18. Auto-install…"
  case "$OS_KIND" in
    macos)
      command -v brew >/dev/null 2>&1 || die "Cần Homebrew để cài Node. Cài Homebrew trước rồi chạy lại."
      log "brew install node"
      brew install node
      ;;
    linux)
      if command -v apt-get >/dev/null 2>&1; then
        log "cài Node LTS qua NodeSource repo"
        curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
        sudo apt-get install -y nodejs
      elif command -v dnf >/dev/null 2>&1; then
        curl -fsSL https://rpm.nodesource.com/setup_lts.x | sudo -E bash -
        sudo dnf install -y nodejs
      elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -Sy --noconfirm nodejs npm
      else
        die "Distro không nhận diện. Cài Node.js >= 18 thủ công rồi chạy lại."
      fi
      ;;
    *) die "OS không hỗ trợ auto-install Node ($OS_KIND). Cài thủ công rồi chạy lại." ;;
  esac
  NODE_BIN="$(command -v node || true)"
  [ -n "$NODE_BIN" ] && _node_version_check "$NODE_BIN" || die "Auto-install xong nhưng vẫn không có Node >= 18."
fi

NODE_VERSION="$("$NODE_BIN" -v)"
NPM_VERSION="$(npm -v 2>/dev/null || echo '?')"
log "node: $NODE_BIN ($NODE_VERSION), npm: $NPM_VERSION"

# ─── [3/7] Backend venv ─────────────────────────────────────────────────
log "[3/7] Backend virtualenv…"
cd "$BACKEND_DIR"

recreate_venv=0
if [ -x ".venv/bin/python" ]; then
  if .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    # `.venv/bin/python` là native binary → luôn chạy được ngay cả khi
    # thư mục project đã bị đổi tên/di chuyển. Nhưng các entry-point
    # script (`pip`, `uvicorn`, `pytest`, …) có shebang line trỏ tuyệt
    # đối tới path lúc tạo venv → sẽ báo `bad interpreter` khi path cha
    # thay đổi. Xác nhận venv còn "khỏe" bằng cách so shebang của
    # `.venv/bin/pip` với absolute path python trong venv hiện tại.
    VENV_PY_ABS="$BACKEND_DIR/.venv/bin/python"
    PIP_SHEBANG=""
    if [ -f ".venv/bin/pip" ]; then
      PIP_SHEBANG="$(head -n 1 .venv/bin/pip | sed -e 's|^#!||' -e 's|[[:space:]].*$||')"
    fi
    if [ -n "$PIP_SHEBANG" ] && [ ! -x "$PIP_SHEBANG" ]; then
      warn ".venv shebang trỏ path cũ ($PIP_SHEBANG) — recreate."
      recreate_venv=1
    else
      EXISTING_VER="$(.venv/bin/python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"
      log ".venv exists (python $EXISTING_VER) ✓"
    fi
  else
    warn ".venv dùng Python < 3.11 — recreate."
    recreate_venv=1
  fi
else
  recreate_venv=1
fi

if [ $recreate_venv -eq 1 ]; then
  log "creating .venv…"
  rm -rf .venv
  "$PY_BIN" -m venv .venv
fi

VENV_PY="$BACKEND_DIR/.venv/bin/python"
[ -x "$VENV_PY" ] || die ".venv/bin/python không tồn tại sau khi tạo."

# ─── [4/7] pip install backend deps ─────────────────────────────────────
log "[4/7] pip install -e '.[dev]' …"
"$VENV_PY" -m pip install -q --upgrade pip
"$VENV_PY" -m pip install -q -e '.[dev]'
log "backend deps installed ✓"

# ─── [5/7] Frontend npm install ─────────────────────────────────────────
log "[5/7] Frontend npm install…"
cd "$FRONTEND_DIR"

need_npm_install=0
if [ ! -d "node_modules" ]; then
  need_npm_install=1
elif [ -f "package-lock.json" ] && [ "package-lock.json" -nt "node_modules/.package-lock.json" ]; then
  # Lock file mới hơn marker → có thay đổi dependency, install lại.
  log "package-lock.json mới hơn — npm install lại"
  need_npm_install=1
fi

if [ $need_npm_install -eq 1 ]; then
  npm install --no-audit --no-fund
else
  log "node_modules đã có ✓"
fi

# ─── [6/7] Build frontend ───────────────────────────────────────────────
log "[6/7] Build frontend (vite)…"
if [ $REBUILD_FE -eq 1 ] || [ ! -f "$FRONTEND_DIST/index.html" ]; then
  # Bypass vue-tsc type-check để build ổn định (giống dev.sh). Type-check
  # đã chạy riêng qua `npm run test` / IDE.
  npx --yes vite build
  log "frontend built → $FRONTEND_DIST"
else
  log "frontend/dist đã có ✓ (dùng --rebuild-frontend để build lại)"
fi

# ─── [7/7] Seed Settings ────────────────────────────────────────────────
log "[7/7] Seed Settings tối thiểu…"
cd "$BACKEND_DIR"

# Runtime dirs (dev.sh runtime, session_cache, qr) — tạo trước để tránh
# race với bootstrap khi seed script chạy song song với UI khác.
mkdir -p runtime runtime/session_cache runtime/qr

# Export env để script seed dùng đúng DB path uvicorn sắp mở.
if [ -n "$IDEAL_QR_TOOL_DB_PATH" ]; then
  export IDEAL_QR_TOOL_DB_PATH
fi
"$VENV_PY" test/setup_default_settings.py

# ─── Summary + exec uvicorn ─────────────────────────────────────────────
echo ""
printf "%b═══════════════════════════════════════════════════════════%b\n" "$C_GREEN$C_BOLD" "$C_RESET"
printf "%b  ✓ Setup done%b\n" "$C_GREEN$C_BOLD" "$C_RESET"
printf "%b  → http://%s:%s/%b\n" "$C_GREEN$C_BOLD" "$BACKEND_HOST" "$BACKEND_PORT" "$C_RESET"
if [ -n "$IDEAL_QR_TOOL_DB_PATH" ]; then
  printf "%b  DB: %s%b\n" "$C_GREEN$C_BOLD" "$IDEAL_QR_TOOL_DB_PATH" "$C_RESET"
else
  printf "%b  DB: backend/runtime/ideal_qr_tool.db%b\n" "$C_GREEN$C_BOLD" "$C_RESET"
fi
printf "%b═══════════════════════════════════════════════════════════%b\n" "$C_GREEN$C_BOLD" "$C_RESET"
echo ""

if [ $SKIP_RUN -eq 1 ]; then
  log "--skip-run → không start uvicorn. Chạy tay: cd backend && .venv/bin/uvicorn app.main:app --host $BACKEND_HOST --port $BACKEND_PORT"
  exit 0
fi

# Cleanup handler — kill uvicorn khi Ctrl+C.
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
  log "đang tắt…"
  [ -n "$BACK_PID" ] && kill "$BACK_PID" 2>/dev/null || true
  kill_by_port "$BACKEND_PORT" TERM
  sleep 1
  kill_by_port "$BACKEND_PORT" KILL
  log "đã dừng."
  exit $code
}
trap cleanup INT TERM EXIT

# Chạy uvicorn. IDEAL_QR_TOOL_BIND_HOST match --host để validate_startup
# đọc đúng (dù hiện tại validate_startup=False, giữ pattern cho tương lai).
IDEAL_QR_TOOL_BIND_HOST="$BACKEND_HOST" \
  "$BACKEND_DIR/.venv/bin/uvicorn" app.main:app \
    --host "$BACKEND_HOST" \
    --port "$BACKEND_PORT" &
BACK_PID=$!

log "uvicorn PID=$BACK_PID — Ctrl+C để dừng"

while kill -0 "$BACK_PID" 2>/dev/null; do
  sleep 1
done

exit 0
