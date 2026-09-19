#!/usr/bin/env bash
# ideal_qr_tool — build release bundle (macOS/Linux).
#
# Output: 1 thư mục staging + 2 archive sẵn để bê đi cho:
#   release/<name>/           (staging - có thể chạy trực tiếp)
#   release/<name>.tar.gz     (Linux/macOS)
#   release/<name>.zip        (Windows-friendly)
#
# Nội dung bundle:
#   backend/                  Python source (bắt buộc — Python cần source để chạy)
#     app/**                  (đã strip __pycache__)
#     pyproject.toml
#     seed_settings.py        (copy từ backend/test/setup_default_settings.py)
#     runtime/.gitkeep
#   frontend/
#     dist/**                 (JS/CSS đã minified, KHÔNG có sourcemap, KHÔNG có .vue)
#   setup.sh                  (RELEASE mode — không cần Node.js)
#   setup.bat                 (parity Windows)
#
# Loại bỏ (không đưa vào bundle):
#   .git/, .venv/, runtime/*, node_modules/, __pycache__/, .pytest_cache/,
#   .hypothesis/, frontend/src/**, frontend/*.json, frontend/vite.config.ts,
#   frontend/tsconfig.json, frontend/index.html (đã có trong dist/),
#   backend/test/, backend/tests/, .kiro/, .vscode/, .idea/, dev.sh,
#   package.json (root), setup.sh/setup.bat (dev version — thay bằng RELEASE).
#
# Usage:
#   bash build.sh
#   bash build.sh --name my_qr_tool_v1
#   bash build.sh --no-archive          # chỉ tạo staging, không đóng gói
#   bash build.sh --clean               # xoá release/ trước
#   bash build.sh --no-timestamp        # KHÔNG append timestamp vào tên bundle
#
# Tên bundle mặc định: <base>_YYYYMMDD_HHMMSS (append timestamp để phân biệt
# giữa các lần build). Dùng --no-timestamp để tắt.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"
RELEASE_TEMPLATE_DIR="$ROOT_DIR/scripts/release"
RELEASE_DIR="$ROOT_DIR/release"

BUILD_TS="$(date +%Y%m%d_%H%M%S)"
BUNDLE_BASE="ideal_qr_tool"
NO_ARCHIVE=0
CLEAN=0
NO_TIMESTAMP=0

while [ $# -gt 0 ]; do
  case "$1" in
    --name)         BUNDLE_BASE="$2"; shift 2 ;;
    --name=*)       BUNDLE_BASE="${1#*=}"; shift ;;
    --no-archive)   NO_ARCHIVE=1; shift ;;
    --clean)        CLEAN=1; shift ;;
    --no-timestamp) NO_TIMESTAMP=1; shift ;;
    -h|--help)
      cat <<EOF
Usage: bash build.sh [OPTIONS]

Options:
  --name NAME       base name của bundle (default: ideal_qr_tool)
                    Timestamp sẽ được append trừ khi có --no-timestamp.
  --no-archive      chỉ tạo thư mục staging, không đóng gói .tar.gz/.zip
  --clean           xoá release/ trước khi build
  --no-timestamp    KHÔNG append _YYYYMMDD_HHMMSS vào tên bundle
  -h, --help        in help

Ví dụ:
  bash build.sh                             # ideal_qr_tool_20260706_143025
  bash build.sh --name qr_v1                # qr_v1_20260706_143025
  bash build.sh --name qr_v1 --no-timestamp # qr_v1
EOF
      exit 0 ;;
    *) echo "ERROR: unknown arg: $1 (xem: bash build.sh --help)" >&2; exit 1 ;;
  esac
done

if [ $NO_TIMESTAMP -eq 1 ]; then
  BUNDLE_NAME="$BUNDLE_BASE"
else
  BUNDLE_NAME="${BUNDLE_BASE}_${BUILD_TS}"
fi

STAGING_DIR="$RELEASE_DIR/$BUNDLE_NAME"

if [ -t 1 ]; then
  C_RESET='\033[0m'; C_BOLD='\033[1m'
  C_RED='\033[31m'; C_GREEN='\033[32m'; C_CYAN='\033[36m'
else
  C_RESET=''; C_BOLD=''; C_RED=''; C_GREEN=''; C_CYAN=''
fi
log()  { printf "%b[build]%b %s\n" "$C_CYAN$C_BOLD" "$C_RESET" "$*"; }
die()  { printf "%b[build][fatal]%b %s\n" "$C_RED$C_BOLD" "$C_RESET" "$*" >&2; exit 1; }

# ─── Sanity check ──────────────────────────────────────────────────────
[ -f "$RELEASE_TEMPLATE_DIR/setup.sh" ] || die "Missing template $RELEASE_TEMPLATE_DIR/setup.sh"
[ -f "$RELEASE_TEMPLATE_DIR/setup.bat" ] || die "Missing template $RELEASE_TEMPLATE_DIR/setup.bat"
[ -f "$BACKEND_DIR/pyproject.toml" ] || die "Missing backend/pyproject.toml"
[ -d "$FRONTEND_DIR" ] || die "Missing frontend/"
[ -f "$BACKEND_DIR/test/setup_default_settings.py" ] || die "Missing backend/test/setup_default_settings.py"

log "Bundle: $BUNDLE_NAME  (build timestamp: $BUILD_TS)"

# ─── [1/6] Clean staging ──────────────────────────────────────────────
if [ $CLEAN -eq 1 ]; then
  log "[1/6] Clean $RELEASE_DIR"
  rm -rf "$RELEASE_DIR"
elif [ -d "$STAGING_DIR" ]; then
  log "[1/6] Xoá staging cũ: $STAGING_DIR"
  rm -rf "$STAGING_DIR"
else
  log "[1/6] Staging chưa tồn tại — skip clean"
fi
mkdir -p "$STAGING_DIR"

# ─── [2/6] Build frontend production ──────────────────────────────────
log "[2/6] Build frontend production (vite build, no sourcemap)…"

# Node phải có sẵn để build. Nếu thiếu → gợi ý chạy setup.sh trước.
if ! command -v node >/dev/null 2>&1; then
  die "Node.js không có trên PATH. Chạy 'bash setup.sh --skip-run' trước để install Node."
fi
if ! command -v npm >/dev/null 2>&1; then
  die "npm không có trên PATH."
fi

cd "$FRONTEND_DIR"
if [ ! -d "node_modules" ]; then
  log "npm install (chưa có node_modules)…"
  npm install --no-audit --no-fund
fi

# Xoá dist cũ để build sạch.
rm -rf dist

# Build production. Bypass vue-tsc (giống dev.sh) — tránh build fail vì
# TS warning non-critical. Vite mặc định KHÔNG sinh sourcemap ⇒ output
# không cách nào reverse về .vue source.
NODE_ENV=production npx --yes vite build

[ -f "dist/index.html" ] || die "Build frontend fail: dist/index.html không tồn tại"

# Double-check: đảm bảo không có sourcemap lọt vào dist (bảo vệ nguồn).
if find dist -name '*.map' | grep -q .; then
  log "warn: tìm thấy .map trong dist — xoá để tránh leak source"
  find dist -name '*.map' -delete
fi

FE_SIZE="$(du -sh dist | awk '{print $1}')"
log "frontend/dist built ($FE_SIZE) ✓"

# ─── [3/6] Copy backend (Python source, strip cache) ──────────────────
log "[3/6] Copy backend/ → staging (strip cache/test)…"
cd "$ROOT_DIR"

STAGING_BACKEND="$STAGING_DIR/backend"
mkdir -p "$STAGING_BACKEND"

# Dùng rsync với exclude patterns — đơn giản, tin cậy hơn cp -r + rm.
if command -v rsync >/dev/null 2>&1; then
  rsync -a \
    --exclude '.venv/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude '.pytest_cache/' \
    --exclude '.hypothesis/' \
    --exclude '.mypy_cache/' \
    --exclude '.ruff_cache/' \
    --exclude 'test/' \
    --exclude 'tests/' \
    --exclude 'runtime/*' \
    --include 'runtime/.gitkeep' \
    "$BACKEND_DIR/" "$STAGING_BACKEND/"
else
  # Fallback không có rsync — cp + xoá thủ công.
  cp -R "$BACKEND_DIR/" "$STAGING_BACKEND/"
  rm -rf "$STAGING_BACKEND/.venv" \
         "$STAGING_BACKEND/.pytest_cache" \
         "$STAGING_BACKEND/.hypothesis" \
         "$STAGING_BACKEND/.mypy_cache" \
         "$STAGING_BACKEND/.ruff_cache" \
         "$STAGING_BACKEND/test" \
         "$STAGING_BACKEND/tests"
  find "$STAGING_BACKEND" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
  find "$STAGING_BACKEND" -type f -name '*.pyc' -delete 2>/dev/null || true
  # Runtime dir: xoá nội dung, giữ dir + .gitkeep.
  find "$STAGING_BACKEND/runtime" -mindepth 1 -not -name '.gitkeep' -delete 2>/dev/null || true
fi

# Đảm bảo runtime dir + .gitkeep tồn tại (nếu source chưa có).
mkdir -p "$STAGING_BACKEND/runtime"
[ -f "$STAGING_BACKEND/runtime/.gitkeep" ] || touch "$STAGING_BACKEND/runtime/.gitkeep"

# Copy seed script ra root của backend (setup.sh slim gọi `seed_settings.py`).
cp "$BACKEND_DIR/test/setup_default_settings.py" "$STAGING_BACKEND/seed_settings.py"

# ─── [4/6] Copy frontend/dist (bundle minified) ──────────────────────
log "[4/6] Copy frontend/dist → staging (KHÔNG kèm src/, config)…"
STAGING_FRONTEND="$STAGING_DIR/frontend"
mkdir -p "$STAGING_FRONTEND"
cp -R "$FRONTEND_DIR/dist" "$STAGING_FRONTEND/dist"

# Đảm bảo staging KHÔNG chứa file .vue nào (defensive).
if find "$STAGING_DIR" -name '*.vue' | grep -q .; then
  die "Bundle vẫn còn .vue file — logic copy sai!"
fi

# ─── [5/6] Copy setup.sh / setup.bat slim (release mode) ─────────────
log "[5/6] Copy setup templates (release mode)…"
cp "$RELEASE_TEMPLATE_DIR/setup.sh" "$STAGING_DIR/setup.sh"
cp "$RELEASE_TEMPLATE_DIR/setup.bat" "$STAGING_DIR/setup.bat"
chmod +x "$STAGING_DIR/setup.sh"

# ─── [6/6] Archive .tar.gz + .zip ────────────────────────────────────
if [ $NO_ARCHIVE -eq 1 ]; then
  log "[6/6] --no-archive → skip đóng gói"
else
  log "[6/6] Đóng gói archive…"
  cd "$RELEASE_DIR"

  TARBALL="$BUNDLE_NAME.tar.gz"
  ZIPBALL="$BUNDLE_NAME.zip"
  rm -f "$TARBALL" "$ZIPBALL"

  # tar.gz
  tar -czf "$TARBALL" "$BUNDLE_NAME"
  TAR_SIZE="$(du -sh "$TARBALL" | awk '{print $1}')"
  log "  ✓ $TARBALL ($TAR_SIZE)"

  # zip — check zip binary có sẵn.
  if command -v zip >/dev/null 2>&1; then
    zip -qr "$ZIPBALL" "$BUNDLE_NAME"
    ZIP_SIZE="$(du -sh "$ZIPBALL" | awk '{print $1}')"
    log "  ✓ $ZIPBALL ($ZIP_SIZE)"
  else
    log "  warn: 'zip' không có trên PATH — skip .zip (Linux: apt install zip)"
  fi
fi

echo ""
printf "%b═══════════════════════════════════════════════════════════%b\n" "$C_GREEN$C_BOLD" "$C_RESET"
printf "%b  ✓ Build release done%b\n" "$C_GREEN$C_BOLD" "$C_RESET"
printf "%b  Staging: %s%b\n" "$C_GREEN$C_BOLD" "$STAGING_DIR" "$C_RESET"
if [ $NO_ARCHIVE -eq 0 ]; then
  printf "%b  Archive: %s/%s.{tar.gz,zip}%b\n" "$C_GREEN$C_BOLD" "$RELEASE_DIR" "$BUNDLE_NAME" "$C_RESET"
fi
printf "%b═══════════════════════════════════════════════════════════%b\n" "$C_GREEN$C_BOLD" "$C_RESET"
echo ""
log "Test bundle:"
log "  cd $STAGING_DIR && bash setup.sh"
