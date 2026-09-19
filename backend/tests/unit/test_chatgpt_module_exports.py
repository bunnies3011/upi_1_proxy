"""Public-surface + anti-circular-import contract for `payments/_chatgpt/`.

The shared ChatGPT module is extracted so a future UPI flow can reuse the
login + session-resolution surface WITHOUT importing `payments/ideal/`. Two
invariants are locked here:

1. The public surface (`SessionBundle`, `parse_account_line`,
   `ChatgptLoginClient`, `resolve_session`, `ChatgptLoginError`) is importable
   directly from `app.payments._chatgpt`.
2. `_chatgpt/` imports NOTHING from `app.payments.ideal` — only the reverse
   direction (`ideal → _chatgpt`) is allowed. Enforced by static AST scan so
   the check is deterministic and order-independent.
"""

from __future__ import annotations

import ast
import pathlib

import app.payments._chatgpt as chatgpt_pkg


def test_public_surface_is_importable() -> None:
    from app.payments._chatgpt import (  # noqa: F401 — import IS the assertion.
        ChatgptLoginClient,
        ChatgptLoginError,
        SessionBundle,
        parse_account_line,
        resolve_session,
    )

    # Basic shape checks so a broken re-export (e.g. re-exporting the wrong
    # object) fails loudly rather than silently.
    assert isinstance(ChatgptLoginClient, type)
    assert issubclass(ChatgptLoginError, Exception)
    assert isinstance(SessionBundle, type)
    assert callable(parse_account_line)
    assert callable(resolve_session)


def test_chatgpt_module_never_imports_ideal() -> None:
    """Anti-circular-import invariant: no module under `_chatgpt/` may import
    from `app.payments.ideal` (directly or via `from ... import`)."""
    package_dir = pathlib.Path(chatgpt_pkg.__file__).parent
    offenders: list[str] = []

    for py_file in sorted(package_dir.glob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "app.payments.ideal" or module.startswith(
                    "app.payments.ideal."
                ):
                    offenders.append(f"{py_file.name}: from {module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "app.payments.ideal" or alias.name.startswith(
                        "app.payments.ideal."
                    ):
                        offenders.append(f"{py_file.name}: import {alias.name}")

    assert not offenders, (
        "_chatgpt/ must not import from app.payments.ideal (anti-circular "
        "invariant). Offending imports:\n  " + "\n  ".join(offenders)
    )
