"""Decode 6 QR PNG để verify nội dung là URL `tx.ideal.nl/2/{tx_id}?sig=...`."""
from __future__ import annotations
import sys
from pathlib import Path
from PIL import Image
import zxingcpp

QR_DIR = Path(__file__).resolve().parents[1] / "runtime" / "qr"


def main() -> int:
    files = sorted(QR_DIR.glob("*.png"))
    print(f"[e2e-verify] {len(files)} QR files\n", flush=True)
    ok = 0
    for f in files:
        img = Image.open(f)
        rs = zxingcpp.read_barcodes(img)
        if not rs:
            print(f"  [FAIL DECODE] {f.name}", flush=True)
            continue
        url = rs[0].text
        expected_prefix = "https://tx.ideal.nl/2/"
        expected_sig = "?sig="
        pass_prefix = url.startswith(expected_prefix)
        has_sig = expected_sig in url
        if pass_prefix and has_sig:
            ok += 1
            print(f"  [PASS] {f.name[:20]} → {url[:100]}...", flush=True)
        else:
            print(f"  [FAIL FORMAT] {f.name} → {url[:200]}", flush=True)
    print(f"\n[verify] {ok}/{len(files)} PASS", flush=True)
    return 0 if ok == len(files) else 1


if __name__ == "__main__":
    sys.exit(main())
