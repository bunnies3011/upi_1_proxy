"""Parse AST + import các module iDEAL flow đã sửa để check syntax + circular imports."""
from __future__ import annotations
import ast
import sys
import traceback
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
FILES = [
    BACKEND / "app" / "payments" / "ideal" / "flow.py",
    BACKEND / "app" / "payments" / "ideal" / "models.py",
    BACKEND / "app" / "payments" / "ideal" / "stripe_client.py",
    BACKEND / "app" / "payments" / "ideal" / "transaction_client.py",
    BACKEND / "app" / "payments" / "ideal" / "errors.py",
    BACKEND / "app" / "payments" / "ideal" / "chatgpt_client.py",
]


def main() -> int:
    print("=== [1] AST syntax check ===", flush=True)
    ok = 0
    for f in FILES:
        try:
            ast.parse(f.read_text())
            print(f"  [PASS] {f.relative_to(BACKEND)}", flush=True)
            ok += 1
        except SyntaxError as e:
            print(f"  [FAIL] {f.relative_to(BACKEND)}: {e}", flush=True)

    if ok != len(FILES):
        return 1

    print("\n=== [2] Import check ===", flush=True)
    sys.path.insert(0, str(BACKEND))
    try:
        from app.payments.ideal.flow import IdealFlowHandler  # noqa
        from app.payments.ideal.models import IdealTransactionInitiate, parse_ideal_transaction_initiate  # noqa
        from app.payments.ideal.stripe_client import StripeClient  # noqa
        from app.payments.ideal.transaction_client import TransactionClient  # noqa
        from app.payments.ideal.errors import IdealFlowError  # noqa
        print("  [PASS] Import OK", flush=True)
    except Exception as e:
        print(f"  [FAIL] Import error: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        return 1

    print("\n=== [3] Verify parse_ideal_transaction_initiate với qrCodeUrl ===", flush=True)
    payload = {
        "view": "INITIAL_VIEW",
        "amount": 1,
        "creditorName": "OpenAI Ireland Limited",
        "supportedIssuers": [
            {
                "id": "INGBNL2A",
                "deeplinkType": "STATIC",
                "deeplink": "https://ideal.ing.nl/xxx",
                "availabilityStatus": "operational",
            }
        ],
        "qrCodeUrl": "https://tx.ideal.nl/2/APR6Z4ZZ?sig=CGBCQEII",
        "payloadUri": "https%3A%2F%2Ftx.ideal.nl%2F2%2FAPR6Z4ZZ",
    }
    ini = parse_ideal_transaction_initiate(payload)
    print(f"  qrCodeUrl parsed: {ini.qrCodeUrl!r}", flush=True)
    print(f"  payloadUri parsed: {ini.payloadUri!r}", flush=True)
    print(f"  supportedIssuers: {len(ini.supportedIssuers)}", flush=True)
    assert ini.qrCodeUrl == "https://tx.ideal.nl/2/APR6Z4ZZ?sig=CGBCQEII"
    assert ini.payloadUri == "https%3A%2F%2Ftx.ideal.nl%2F2%2FAPR6Z4ZZ"
    print("  [PASS] parse_ideal_transaction_initiate ready.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
