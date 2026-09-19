"""Source quarantine: no runtime/test import of external gpt-upi-tool source."""

from __future__ import annotations

import ast
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[2]
_UPI_DIRECT = _BACKEND / "app" / "payments" / "upi_direct"
_SOURCE_MARKERS = (
    "gpt-upi-tool-master",
    "gpt_upi_tool",
    "pay_upi_http",
    "upi_runner",
)


def test_upi_direct_package_has_no_source_tree_references() -> None:
    for path in _UPI_DIRECT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for marker in _SOURCE_MARKERS:
            assert marker not in text, f"{path} contains source marker {marker}"


def test_upi_direct_package_has_no_live_stripe_keys() -> None:
    """Package source must not embed live Stripe secrets (not this test file)."""
    forbidden = ("sk_live_", "pk_live_", "rk_live_")
    for path in _UPI_DIRECT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{path} contains {token}"


def test_no_ideal_import_ast() -> None:
    for path in _UPI_DIRECT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("app.payments.ideal")
