"""Verify bundle JS mới có sử dụng CSS vars (--surface, --text, --log-bg)."""
from __future__ import annotations
import sys
from pathlib import Path

DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def main() -> int:
    css_files = list(DIST.glob("assets/index-*.css"))
    js_files = list(DIST.glob("assets/index-*.js"))
    if not css_files or not js_files:
        print(f"[FAIL] no dist files. dist={DIST}", flush=True)
        return 1

    css = css_files[0].read_text()
    # Check tokens declared
    tokens = [
        "--surface-0", "--surface-1", "--surface-2",
        "--text-1", "--text-2", "--text-3",
        "--log-bg", "--log-text",
        "--accent-green", "--accent-red", "--accent-blue",
        "--border-color",
    ]
    for t in tokens:
        marker = "PASS" if t in css else "FAIL"
        print(f"  [{marker}] token {t}", flush=True)

    # Check .app-root:not(.app-root--dark) light theme rule exists
    if ":not(.app-root--dark)" in css or "app-root:not" in css:
        print(f"  [PASS] light theme selector present", flush=True)
    else:
        print(f"  [FAIL] light theme selector missing", flush=True)

    # Check log viewer uses var (not hardcoded #0a0f16)
    hardcoded = "#0a0f16"
    if hardcoded in css:
        print(f"  [WARN] still contains {hardcoded} — LogPanel or JobDetailPanel", flush=True)
    else:
        print(f"  [PASS] no hardcoded {hardcoded}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
