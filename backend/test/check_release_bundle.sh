#!/usr/bin/env bash
# Verify bundle release: khong co .vue, khong co sourcemap, khong co source frontend.
# Chay: bash backend/test/check_release_bundle.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
STAGING="$ROOT/release/ideal_qr_tool"

if [ ! -d "$STAGING" ]; then
  echo "[FAIL] staging chua ton tai: $STAGING (chay 'bash build.sh' truoc)"
  exit 1
fi

fail=0
check_pass() { echo "[PASS] $*"; }
check_fail() { echo "[FAIL] $*"; fail=1; }

# 1) Khong co .vue file
n_vue=$(find "$STAGING" -name '*.vue' 2>/dev/null | wc -l | tr -d ' ')
if [ "$n_vue" -eq 0 ]; then
  check_pass "TC-01 no .vue files in bundle"
else
  check_fail "TC-01 tim thay $n_vue .vue file: $(find "$STAGING" -name '*.vue')"
fi

# 2) Khong co sourcemap
n_map=$(find "$STAGING" -name '*.map' 2>/dev/null | wc -l | tr -d ' ')
if [ "$n_map" -eq 0 ]; then
  check_pass "TC-02 no .map (sourcemap) files"
else
  check_fail "TC-02 tim thay $n_map .map file"
fi

# 3) Khong co frontend/src/
if [ ! -d "$STAGING/frontend/src" ]; then
  check_pass "TC-03 no frontend/src/"
else
  check_fail "TC-03 frontend/src/ VAN CON!"
fi

# 4) Khong co frontend/node_modules
if [ ! -d "$STAGING/frontend/node_modules" ]; then
  check_pass "TC-04 no frontend/node_modules/"
else
  check_fail "TC-04 frontend/node_modules/ van con"
fi

# 5) Khong co frontend/package.json / vite.config.ts
if [ ! -f "$STAGING/frontend/package.json" ] && [ ! -f "$STAGING/frontend/vite.config.ts" ]; then
  check_pass "TC-05 no frontend config files (package.json / vite.config.ts)"
else
  check_fail "TC-05 config frontend van con"
fi

# 6) Frontend dist ton tai
if [ -f "$STAGING/frontend/dist/index.html" ]; then
  check_pass "TC-06 frontend/dist/index.html present"
else
  check_fail "TC-06 frontend/dist/index.html khong ton tai"
fi

# 7) Bundle JS minified.
# Metric dung cho esbuild output: bytes-per-line trung binh > 200 (esbuild
# tu chen newline sau ~500-1000 chars de tranh crash text tool, nhung van
# giu density rat cao). Source Vue nen build thi bytes/line thap (~30-80).
js_file=$(find "$STAGING/frontend/dist/assets" -name 'index-*.js' | head -1)
if [ -n "$js_file" ]; then
  bytes=$(wc -c < "$js_file" | tr -d ' ')
  lines=$(wc -l < "$js_file" | tr -d ' ')
  [ "$lines" -eq 0 ] && lines=1
  bpl=$((bytes / lines))
  if [ "$bpl" -gt 200 ]; then
    check_pass "TC-07 bundle JS minified (bytes/line=$bpl, size=${bytes}B)"
  else
    check_fail "TC-07 bundle JS co bytes/line=$bpl - co the khong minified?"
  fi
else
  check_fail "TC-07 khong tim thay index-*.js"
fi

# 8) Backend co pyproject.toml + app/
if [ -f "$STAGING/backend/pyproject.toml" ] && [ -d "$STAGING/backend/app" ]; then
  check_pass "TC-08 backend/{pyproject.toml, app/} present"
else
  check_fail "TC-08 backend structure thieu"
fi

# 9) Khong co __pycache__
n_pycache=$(find "$STAGING" -type d -name '__pycache__' 2>/dev/null | wc -l | tr -d ' ')
if [ "$n_pycache" -eq 0 ]; then
  check_pass "TC-09 no __pycache__ dirs"
else
  check_fail "TC-09 tim thay $n_pycache __pycache__ dir"
fi

# 10) Khong co backend/test/ hoac backend/tests/
if [ ! -d "$STAGING/backend/test" ] && [ ! -d "$STAGING/backend/tests" ]; then
  check_pass "TC-10 no backend/test/ or backend/tests/"
else
  check_fail "TC-10 backend/test hoac tests van con"
fi

# 11) Backend seed_settings.py co mat (setup.sh slim goi)
if [ -f "$STAGING/backend/seed_settings.py" ]; then
  check_pass "TC-11 backend/seed_settings.py present"
else
  check_fail "TC-11 backend/seed_settings.py THIEU - setup.sh se fail"
fi

# 12) Setup scripts release present + executable
if [ -x "$STAGING/setup.sh" ] && [ -f "$STAGING/setup.bat" ]; then
  check_pass "TC-12 setup.sh (executable) + setup.bat present"
else
  check_fail "TC-12 setup scripts thieu hoac khong executable"
fi

# 13) Archive files created
if [ -f "$ROOT/release/ideal_qr_tool.tar.gz" ]; then
  size=$(du -sh "$ROOT/release/ideal_qr_tool.tar.gz" | awk '{print $1}')
  check_pass "TC-13 tar.gz created ($size)"
else
  check_fail "TC-13 tar.gz missing"
fi

if [ -f "$ROOT/release/ideal_qr_tool.zip" ]; then
  size=$(du -sh "$ROOT/release/ideal_qr_tool.zip" | awk '{print $1}')
  check_pass "TC-14 zip created ($size)"
else
  check_fail "TC-14 zip missing"
fi

echo ""
if [ $fail -eq 0 ]; then
  echo "ALL PASS"
  exit 0
else
  echo "SOME CHECKS FAILED"
  exit 1
fi
