"""Verify grid template-areas mới `input jobs / log log / success error`."""
from __future__ import annotations
import sys
from pathlib import Path

DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def main() -> int:
    css_files = list(DIST.glob("assets/index-*.css"))
    if not css_files:
        print("[FAIL] no dist CSS", flush=True)
        return 1
    css = css_files[0].read_text()

    # Vite/PostCSS minify có thể strip whitespace và newlines trong CSS →
    # normalize thành 1 dòng để regex.
    css_normal = " ".join(css.split())

    checks = [
        ('template-areas "input jobs"', '"input jobs"'),
        ('template-areas "log log"', '"log log"'),
        ('template-areas "success error"', '"success error"'),
    ]
    ok = 0
    for label, pattern in checks:
        # Cho phép biến thể (' hoặc ", space bên trong)
        found = pattern in css_normal or pattern.replace('"', "'") in css_normal
        marker = "PASS" if found else "FAIL"
        print(f"  [{marker}] {label}", flush=True)
        if found:
            ok += 1

    if ok == len(checks):
        print(f"\n[PASS] layout 2/1/2 grid confirmed ({ok}/{len(checks)})", flush=True)
        return 0
    print(f"\n[FAIL] layout check {ok}/{len(checks)}", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
