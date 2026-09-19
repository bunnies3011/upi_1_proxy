"""Parity test cho stripe_token — compare với UPI Rust reference values.

Verify các primitives (caesar_shift, stripe_encode, compute_*) khớp với
`rust_upi_bot/src/stripe_token.rs` để confirm thuật toán đã port chính xác.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.payments.ideal.stripe_token import (  # noqa: E402
    StripeTokenConfig,
    caesar_shift,
    compute_js_checksum,
    compute_rv_timestamp,
    stripe_encode,
)


def main() -> int:
    tests = [
        # Caesar shift
        (
            "caesar_shift('Hello', 11)",
            lambda: caesar_shift("Hello", 11),
            "Spwwz",
        ),
        (
            "caesar_shift('abc XYZ 123', 11)",
            lambda: caesar_shift("abc XYZ 123", 11),
            "lmn+cde+<=>",
        ),
        (
            "caesar_shift('!@#$%', 5)",
            lambda: caesar_shift("!@#$%", 5),
            "&E()*",
        ),
        # Stripe encode
        (
            "stripe_encode('test')",
            lambda: stripe_encode("test"),
            "cWB2cSUl",
        ),
        (
            'stripe_encode({"id":"abc"})',
            lambda: stripe_encode('{"id":"abc"}'),
            "fidsYSc%2FJ2RnZid4JSUl",
        ),
        (
            "stripe_encode('hello world!')",
            lambda: stripe_encode("hello world!"),
            "bWBpaWolcmp3aWEkJSUl",
        ),
        # js_checksum
        (
            "compute_js_checksum('test_ppage_id_abc', 11)",
            lambda: compute_js_checksum("test_ppage_id_abc", shift=11),
            "qto~d^n0=QU>QroyQlocavdxMlmRQleRoxU>rw",
        ),
        (
            "compute_js_checksum('pp_xyz_123', 11)",
            lambda: compute_js_checksum("pp_xyz_123", shift=11),
            "qto~d^n0=QU>a<by<Cq<z;Y&dypN`w",
        ),
        # rv_timestamp
        (
            "compute_rv_timestamp(cfg)",
            lambda: compute_rv_timestamp(
                StripeTokenConfig(
                    bundle_hash="dummy",
                    shift=11,
                    rv_ts="2024-01-01 00:00:00 -0000",
                    rv="e5ebd5e1e6abc123",
                    sv="3c7ef39815def456",
                )
            ),
            r"qto>n<Q=U&CyY&`>X^r<YNr<YN`<Y_C<Y_C<Y^`zY_`<Y^n{U>o&U&CydOMre=P#dO]rX=]yeu\>Ytn{U>e&U&CyYxd%dRX=[O;;XRQrd&P#X%o?U^`w",
        ),
    ]

    all_ok = True
    for name, fn, expected in tests:
        try:
            got = fn()
            ok = got == expected
            marker = "[PASS]" if ok else "[FAIL]"
            print(f"{marker} {name}", flush=True)
            if not ok:
                print(f"  expected: {expected!r}", flush=True)
                print(f"  got:      {got!r}", flush=True)
                all_ok = False
        except Exception as ex:  # noqa: BLE001
            print(f"[ERROR] {name} → {ex}", flush=True)
            all_ok = False

    print(f"\n[SUMMARY] {'ALL PASS' if all_ok else 'HAS FAILURES'}", flush=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
