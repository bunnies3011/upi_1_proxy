"""Architecture check — Core/API/main boundary vs Payment module (Task 34.1).

AST-scan tĩnh (KHÔNG execute module) để enforce Payment_Module_Boundary
(Requirement 13.1–13.7, 9.8):

1. `app/core/**/*.py` — KHÔNG được import bất kỳ symbol nào thuộc
   `app.payments.*` (bao gồm cả `app.payments.ideal.*`) hoặc relative
   `..payments.*` / `.payments.*`. `core/` phải payment-agnostic để có thể
   dùng lại cho payment method khác (R13.2, R13.3, R9.8).

2. `app/main.py` — KHÔNG được import `app.payments.*`. SAU refactor task 38.2
   (theo docstring `app/main.py`), `main.py` đã ủy quyền toàn bộ 2 điểm chạm
   payment module cho `app/bootstrap.py` (R13.7). `main.py` chỉ điều phối tầng
   HTTP.

3. `app/api/**/*.py` — KHÔNG được import `app.payments.*`. Router chỉ tương
   tác với payment logic qua interface generic `JobManager`, KHÔNG biết payment
   method cụ thể (R13.5, R13.6).

4. `app/bootstrap.py` — ĐIỂM TÍCH HỢP DUY NHẤT của payment module (R13.7):
   PHẢI có ít nhất 1 import `app.payments.ideal` (positive assertion — nếu
   ai vô tình xoá import ở đây và chuyển sang chỗ khác, test sẽ fail luôn cả
   ràng buộc "duy nhất" ở scope 1/2/3 lẫn ràng buộc "phải tồn tại ở đây").

Parse thuần bằng `ast` module stdlib — không import target module, tránh
side-effect và không phụ thuộc runtime state khi thu thập vi phạm.

_Requirements: 9.8, 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7_
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Anchor path — file này ở `backend/tests/unit/`, parents[2] = `backend/`.
# ---------------------------------------------------------------------------
_BACKEND_ROOT: Path = Path(__file__).resolve().parents[2]
_APP_DIR: Path = _BACKEND_ROOT / "app"
_CORE_DIR: Path = _APP_DIR / "core"
_API_DIR: Path = _APP_DIR / "api"
_MAIN_FILE: Path = _APP_DIR / "main.py"
_BOOTSTRAP_FILE: Path = _APP_DIR / "bootstrap.py"


# ---------------------------------------------------------------------------
# Helper — phân loại target import.
# ---------------------------------------------------------------------------

def _is_payments_target(module: str | None, level: int) -> bool:
    """True nếu 1 target import trỏ vào package `payments.*`.

    Bao trùm:
    - absolute: `app.payments`, `app.payments.*`, `payments`, `payments.*`
      (bao gồm cả `payments.ideal.*` — task 34.1 nhắc tên này rõ).
    - relative (`level > 0`) mà first segment là `payments`
      (ví dụ `from ..payments.ideal import x`).

    `from . import x` (level>0, module=None) KHÔNG trỏ trực tiếp tới
    `payments.*` nên trả False — case này sẽ được scope-check khác bắt nếu cần.
    """
    if module is None:
        return False

    first_segment = module.split(".", 1)[0]

    if level > 0:
        # Relative import: chỉ cần đụng vào `payments` là vi phạm.
        return first_segment == "payments"

    # Absolute import.
    if module == "app.payments" or module.startswith("app.payments."):
        return True
    if module == "payments" or module.startswith("payments."):
        return True
    return False


def _display_from_target(module: str | None, level: int) -> str:
    """Chuẩn hoá tên target import để hiển thị trong thông báo lỗi."""
    return ("." * level) + (module or "")


def _collect_payment_imports(py_file: Path) -> list[str]:
    """Parse 1 file `.py`, trả về list vi phạm dạng
    `<relative_path>:<line> import <target>` (relative theo backend root để
    thông báo lỗi ngắn, không lệ thuộc CWD của người chạy test).
    """
    source = py_file.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(py_file))

    rel_path = py_file.relative_to(_BACKEND_ROOT)
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            # `import a.b.c` — mỗi alias là 1 module absolute, level=0.
            for alias in node.names:
                if _is_payments_target(alias.name, level=0):
                    violations.append(
                        f"{rel_path}:{node.lineno} import {alias.name}"
                    )
        elif isinstance(node, ast.ImportFrom):
            if _is_payments_target(node.module, level=node.level):
                target = _display_from_target(node.module, node.level)
                violations.append(
                    f"{rel_path}:{node.lineno} from {target}"
                )

    return violations


def _list_py_files(base: Path) -> list[Path]:
    """Đệ quy tất cả file `.py` dưới `base`, sort ổn định. `rglob("*.py")`
    tự loại `.pyc`/`__pycache__/*.pyc` (chỉ match `.py` extension).
    """
    return sorted(base.rglob("*.py"))


# ---------------------------------------------------------------------------
# Sanity: đường dẫn tồn tại.
# ---------------------------------------------------------------------------

def test_paths_exist() -> None:
    """Anchor path phải tồn tại — nếu không, test còn lại vô nghĩa (fail-fast)."""
    assert _APP_DIR.is_dir(), f"APP_DIR missing: {_APP_DIR}"
    assert _CORE_DIR.is_dir(), f"CORE_DIR missing: {_CORE_DIR}"
    assert _API_DIR.is_dir(), f"API_DIR missing: {_API_DIR}"
    assert _MAIN_FILE.is_file(), f"main.py missing: {_MAIN_FILE}"
    assert _BOOTSTRAP_FILE.is_file(), f"bootstrap.py missing: {_BOOTSTRAP_FILE}"


# ---------------------------------------------------------------------------
# Scope 1: `app/core/**` — KHÔNG import `payments.*`.
# ---------------------------------------------------------------------------

def test_core_has_no_payments_imports() -> None:
    """`app/core/**/*.py` KHÔNG import gì từ `payments.*` (R13.1–13.7, R9.8)."""
    py_files = _list_py_files(_CORE_DIR)
    assert py_files, f"Không có file .py nào trong {_CORE_DIR}"

    violations: list[str] = []
    for py_file in py_files:
        violations.extend(_collect_payment_imports(py_file))

    assert violations == [], (
        "Vi phạm Payment_Module_Boundary — `app/core/` KHÔNG được import "
        "`payments.*` (R13.1–13.3, R9.8):\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# Scope 2: `app/main.py` — KHÔNG import `payments.*` (R13.7 — bootstrap.py
# là điểm tích hợp duy nhất).
# ---------------------------------------------------------------------------

def test_main_has_no_payments_imports() -> None:
    """`app/main.py` KHÔNG được import `payments.*` — SAU refactor task 38.2
    (docstring `app/main.py`), điểm chạm được rút về `app/bootstrap.py` (R13.7).
    """
    violations = _collect_payment_imports(_MAIN_FILE)
    assert violations == [], (
        "Vi phạm Payment_Module_Boundary — `app/main.py` KHÔNG được import "
        "`payments.*` (R13.7 — dùng `app/bootstrap.py`):\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# Scope 3: `app/api/**` — KHÔNG import `payments.*` (R13.5, R13.6).
# ---------------------------------------------------------------------------

def test_api_layer_has_no_payments_imports() -> None:
    """`app/api/**/*.py` KHÔNG được import `payments.*` — router chỉ tương tác
    payment logic qua `JobManager` generic (R13.5, R13.6).
    """
    py_files = _list_py_files(_API_DIR)
    assert py_files, f"Không có file .py nào trong {_API_DIR}"

    violations: list[str] = []
    for py_file in py_files:
        violations.extend(_collect_payment_imports(py_file))

    assert violations == [], (
        "Vi phạm Payment_Module_Boundary — `app/api/` KHÔNG được import "
        "`payments.*` (R13.5, R13.6):\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# Scope 4: `app/bootstrap.py` — LÀ điểm tích hợp DUY NHẤT (R13.7). Positive
# assertion: PHẢI có ít nhất 1 import `app.payments.ideal` để bảo vệ ngữ
# nghĩa "duy nhất" của scope này. Nếu ai vô tình chuyển import sang chỗ
# khác, test scope 1/2/3 sẽ báo vi phạm; nếu ai xoá luôn thì test này báo
# thiếu điểm tích hợp.
# ---------------------------------------------------------------------------

def test_bootstrap_is_the_sole_payment_integration_point() -> None:
    """`app/bootstrap.py` PHẢI import `app.payments.ideal` (R13.7)."""
    imports = _collect_payment_imports(_BOOTSTRAP_FILE)
    # `imports` chứa các dòng vi phạm nếu ta apply cùng rule, nhưng ở đây
    # KHÔNG phải vi phạm — ngược lại, phải TỒN TẠI. Filter lấy các dòng
    # trỏ tới `app.payments.ideal` cụ thể (R13.7 dùng registry `ideal`).
    ideal_imports = [line for line in imports if "app.payments.ideal" in line]

    assert ideal_imports, (
        "Vi phạm R13.7 — `app/bootstrap.py` PHẢI import `app.payments.ideal` "
        "làm điểm tích hợp duy nhất (registry `register_ideal_namespace` / "
        "`register_ideal_handler`). Hiện không tìm thấy import nào.\n"
        f"Tất cả import payments.* trong bootstrap.py: {imports or '<none>'}"
    )


# ---------------------------------------------------------------------------
# Parametrize summary — 1 test tổng hợp để nếu chạy verbose sẽ liệt kê từng
# file/scope cụ thể, giúp CI log dễ đọc. Không thay thế các test trên
# (mỗi scope 1 test riêng vẫn cần để pytest báo failure tách bạch).
# ---------------------------------------------------------------------------

def _forbidden_scopes() -> list[tuple[str, list[Path]]]:
    return [
        ("app/core/", _list_py_files(_CORE_DIR)),
        ("app/main.py", [_MAIN_FILE]),
        ("app/api/", _list_py_files(_API_DIR)),
    ]


@pytest.mark.parametrize(
    ("scope_label", "py_file"),
    [
        (label, py_file)
        for label, files in _forbidden_scopes()
        for py_file in files
    ],
    ids=lambda x: str(x) if isinstance(x, Path) else x,
)
def test_no_payments_import_per_file(scope_label: str, py_file: Path) -> None:
    """Per-file check để CI báo lỗi ở chính xác file vi phạm (không gộp)."""
    violations = _collect_payment_imports(py_file)
    assert violations == [], (
        f"[{scope_label}] Vi phạm Payment_Module_Boundary trong "
        f"{py_file.relative_to(_BACKEND_ROOT)}:\n" + "\n".join(violations)
    )
