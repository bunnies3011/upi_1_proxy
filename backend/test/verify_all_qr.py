"""Verify tất cả QR PNG decode ra đúng Stripe hosted checkout URL."""

from __future__ import annotations

import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    qr_dir = ROOT / "runtime" / "qr"
    all_ok = True
    files = sorted(qr_dir.glob("*.png"))
    print(f"Total QR files: {len(files)}", flush=True)

    detector = cv2.QRCodeDetector()
    for p in files:
        img = cv2.imread(str(p))
        data, _, _ = detector.detectAndDecode(img)
        ok = data.startswith("https://checkout.stripe.com/c/pay/cs_live_")
        marker = "[PASS]" if ok else "[FAIL]"
        cs = data.split("cs_live_")[1][:20] if "cs_live_" in data else "N/A"
        print(f"{marker} {p.name}  →  cs_live_{cs}...  ({len(data)} chars)", flush=True)
        if not ok:
            all_ok = False

    print(f"\n[SUMMARY] {'ALL PASS' if all_ok else 'HAS FAILURES'}", flush=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
