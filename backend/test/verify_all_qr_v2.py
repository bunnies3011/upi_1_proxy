"""Verify QR PNG bằng zxing-cpp (ZXing port C++, chuẩn công nghiệp — mà
mọi phone camera dùng bên dưới). Nếu ZXing decode được → phone quét được.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image
import zxingcpp

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    qr_dir = ROOT / "runtime" / "qr"
    all_ok = True
    files = sorted(qr_dir.glob("*.png"))
    print(f"Total QR files: {len(files)}", flush=True)

    for p in files:
        img = Image.open(p)
        results = zxingcpp.read_barcodes(img)
        if not results:
            print(f"[FAIL] {p.name}  —  no QR detected", flush=True)
            all_ok = False
            continue
        data = results[0].text
        ok = data.startswith("https://checkout.stripe.com/c/pay/cs_live_")
        marker = "[PASS]" if ok else "[FAIL]"
        cs = data.split("cs_live_")[1][:20] if "cs_live_" in data else "N/A"
        print(f"{marker} {p.name}  →  cs_live_{cs}...  ({len(data)} chars, {results[0].format})", flush=True)
        if not ok:
            all_ok = False

    print(f"\n[SUMMARY] {'ALL PASS (phone quét được)' if all_ok else 'HAS FAILURES'}", flush=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
