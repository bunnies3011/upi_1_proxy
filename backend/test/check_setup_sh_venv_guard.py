"""Xác nhận setup.sh có bước detect broken shebang cho .venv.

Kiểm tra:
  TC-01 shell syntax bằng `bash -n setup.sh` (parse mode, không thực thi).
  TC-02 chuỗi guard `PIP_SHEBANG` xuất hiện trong file.
  TC-03 branch recreate được set khi shebang trỏ path không tồn tại.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SETUP_SH = ROOT / "setup.sh"


def run(step: str, desc: str, ok: bool, detail: str = "") -> bool:
    tag = "[PASS]" if ok else "[FAIL]"
    suffix = f" :: {detail}" if detail else ""
    print(f"{tag} {step} — {desc}{suffix}", flush=True)
    return ok


def main() -> int:
    results: list[bool] = []

    # TC-01 bash parse
    proc = subprocess.run(
        ["bash", "-n", str(SETUP_SH)],
        capture_output=True,
        text=True,
    )
    results.append(run(
        "TC-01",
        "bash -n setup.sh",
        proc.returncode == 0,
        proc.stderr.strip(),
    ))

    text = SETUP_SH.read_text(encoding="utf-8")

    # TC-02 guard token
    results.append(run(
        "TC-02",
        "chứa guard PIP_SHEBANG",
        "PIP_SHEBANG" in text,
    ))

    # TC-03 recreate branch
    results.append(run(
        "TC-03",
        "recreate khi shebang cũ",
        'shebang trỏ path cũ' in text and 'recreate_venv=1' in text,
    ))

    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
