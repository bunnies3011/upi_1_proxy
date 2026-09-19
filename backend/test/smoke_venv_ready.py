"""Smoke: .venv shebang khớp path hiện tại và các entry-point chạy được."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

VENV = Path(__file__).resolve().parents[1] / ".venv"
BIN = VENV / "bin"


def step(tc: str, desc: str, ok: bool, detail: str = "") -> bool:
    tag = "[PASS]" if ok else "[FAIL]"
    print(f"{tag} {tc} — {desc} :: {detail}" if detail else f"{tag} {tc} — {desc}", flush=True)
    return ok


def head(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[0]


def main() -> int:
    results: list[bool] = []

    # TC-01 shebang pip khớp .venv/bin/python
    pip_head = head(BIN / "pip")
    expected = f"#!{BIN / 'python'}"
    results.append(step(
        "TC-01",
        "shebang pip khớp .venv/bin/python",
        pip_head == expected or pip_head.startswith(f"#!{BIN}"),
        f"got={pip_head}",
    ))

    # TC-02 uvicorn chạy được (import + --version)
    proc = subprocess.run(
        [str(BIN / "uvicorn"), "--version"],
        capture_output=True,
        text=True,
    )
    results.append(step(
        "TC-02",
        "uvicorn --version",
        proc.returncode == 0,
        (proc.stdout or proc.stderr).strip(),
    ))

    # TC-03 import chính app.bootstrap được (đủ deps)
    proc = subprocess.run(
        [str(BIN / "python"), "-c", "import app.bootstrap; print('bootstrap OK')"],
        capture_output=True,
        text=True,
        cwd=str(VENV.parent),
    )
    results.append(step(
        "TC-03",
        "import app.bootstrap",
        proc.returncode == 0,
        (proc.stdout or proc.stderr).strip(),
    ))

    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
