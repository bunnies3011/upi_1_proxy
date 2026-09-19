#!/usr/bin/env sh
# TypeScript / Vue typecheck cho các file vừa sửa. In tiến trình từng bước.
set -e

cd "$(dirname "$0")/../frontend"

echo "[1/2] Running vue-tsc (typecheck only)..."
# --noEmit vì chỉ cần verify type, không cần build output.
if [ ! -f node_modules/.bin/vue-tsc ]; then
  echo "  [FAIL] vue-tsc not installed — chạy npm install trong frontend/"
  exit 1
fi
./node_modules/.bin/vue-tsc --noEmit
echo "  [PASS] typecheck OK"

echo "[2/2] Done."
