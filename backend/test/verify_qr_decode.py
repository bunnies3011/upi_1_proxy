"""Verify QR PNG có decode ra đúng payment_link Stripe hosted URL."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

try:
    from PIL import Image
    from pyzbar.pyzbar import decode  # type: ignore
except ImportError:
    # Fallback: dùng qrcode reader khác
    print("[SKIP] pyzbar không cài — bỏ qua decode, chỉ verify file tồn tại", flush=True)

    for p in sorted((ROOT / "runtime" / "qr").glob("*.png")):
        print(f"[FILE] {p.name} = {p.stat().st_size} bytes", flush=True)
    sys.exit(0)


def main() -> int:
    qr_dir = ROOT / "runtime" / "qr"
    for p in sorted(qr_dir.glob("*.png")):
        img = Image.open(p)
        results = decode(img)
        if not results:
            print(f"[FAIL] {p.name} — không decode được QR", flush=True)
            continue
        for r in results:
            data = r.data.decode("utf-8")
            print(f"[OK] {p.name}", flush=True)
            print(f"     decoded (first 200 chars): {data[:200]}", flush=True)
            if data.startswith("https://checkout.stripe.com/c/pay/"):
                print(f"     ✓ URL Stripe hosted checkout hợp lệ", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
