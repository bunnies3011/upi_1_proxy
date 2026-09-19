#!/usr/bin/env bash
# Runner pytest unit test suite với progress realtime + per-file timeout 60s.
# Đọc log dần qua get_process_output.
set -u
cd "$(dirname "$0")/.."

TOTAL=0
PASSED=0
FAILED=0
FAILED_FILES=()

# Enumerate unit test files (sorted deterministic).
mapfile -t FILES < <(find tests/unit -maxdepth 1 -name 'test_*.py' | sort)

echo "=========================================="
echo "PYTEST UNIT SUITE — ${#FILES[@]} test files"
echo "=========================================="

for f in "${FILES[@]}"; do
    TOTAL=$((TOTAL + 1))
    rel="${f#tests/unit/}"
    printf '[%d/%d] %-70s ' "$TOTAL" "${#FILES[@]}" "$rel"

    # Per-file timeout 60s (dùng bash builtin `timeout`).
    if output=$(timeout 60 .venv/bin/python -m pytest "$f" --tb=short -q -p no:cacheprovider 2>&1); then
        # Trích dòng summary cuối
        summary=$(echo "$output" | tail -3 | grep -E '(passed|failed|error)' | head -1)
        echo "[PASS] $summary"
        PASSED=$((PASSED + 1))
    else
        rc=$?
        summary=$(echo "$output" | tail -20 | grep -E '(FAILED|ERROR|passed|failed)' | head -3 | tr '\n' ' | ')
        echo "[FAIL rc=$rc] $summary"
        FAILED=$((FAILED + 1))
        FAILED_FILES+=("$rel")
        # Dump chi tiết failure vào stderr để đọc lại
        echo "---- FAIL DETAIL: $rel ----" >&2
        echo "$output" | tail -50 >&2
        echo "---- END FAIL DETAIL ----" >&2
    fi
done

echo ""
echo "=========================================="
echo "SUMMARY: total=$TOTAL passed=$PASSED failed=$FAILED"
if [ ${#FAILED_FILES[@]} -gt 0 ]; then
    echo "FAILED FILES:"
    for ff in "${FAILED_FILES[@]}"; do
        echo "  - $ff"
    done
    exit 1
fi
echo "=========================================="
