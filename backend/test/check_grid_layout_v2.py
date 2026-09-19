"""Verify grid layout v2: JOBS full-height cột phải, trái stack 3 (input/log/success)."""
from __future__ import annotations
import sys
from pathlib import Path

DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def main() -> int:
    css_files = list(DIST.glob("assets/index-*.css"))
    if not css_files:
        print("[FAIL] no dist CSS", flush=True)
        return 1
    css = " ".join(css_files[0].read_text().split())

    checks = [
        ('template-areas "input jobs"', '"input jobs"'),
        ('template-areas "log jobs"', '"log jobs"'),
        ('template-areas "success jobs"', '"success jobs"'),
        ('no more "error" area', '"success error"'),  # ngược lại — should NOT be found
        ('no more "log log"', '"log log"'),  # ngược lại — should NOT be found
    ]
    ok = 0
    for i, (label, pattern) in enumerate(checks):
        found = pattern in css
        # Cho checks negation
        if i >= 3:
            expect_found = False
            marker = "PASS" if not found else "FAIL"
            if not found:
                ok += 1
        else:
            marker = "PASS" if found else "FAIL"
            if found:
                ok += 1
        print(f"  [{marker}] {label}", flush=True)

    # ErrorOutputPanel deleted
    err_file = DIST.parent / "src" / "components" / "ErrorOutputPanel.vue"
    if err_file.exists():
        print(f"  [FAIL] ErrorOutputPanel.vue still exists", flush=True)
    else:
        print(f"  [PASS] ErrorOutputPanel.vue deleted", flush=True)
        ok += 1

    print(f"\n[verify] {ok}/{len(checks)+1}", flush=True)
    return 0 if ok >= len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
