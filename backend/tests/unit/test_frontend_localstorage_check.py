"""Lint test — Frontend KHÔNG dùng `localStorage.setItem` cho runtime config.

Requirement 11.5: Frontend_App KHÔNG được dùng `localStorage` làm nguồn cấu
hình runtime; nguồn duy nhất là Settings Store (backend). Ngoại lệ có chủ
đích: persistent auth token cho header `X-Auth-Token` (Requirement 14.7) —
được lưu duy nhất qua key `IDEAL_QR_TOOL_AUTH_TOKEN` trong
`composables/useAuthToken.ts` (task 28.1).

Cơ chế check: scan mọi file `.ts` / `.vue` trong `frontend/src/`, dùng regex
đơn giản (không cần AST TypeScript) bắt call `localStorage.setItem(<key>,
<value>)`, phân loại theo argument đầu:

- Literal `"IDEAL_QR_TOOL_AUTH_TOKEN"` / `'IDEAL_QR_TOOL_AUTH_TOKEN'`
  → whitelist ở BẤT KỲ file nào (giá trị đúng ngoại lệ có chủ đích).
- Identifier `STORAGE_KEY` hoặc `AUTH_TOKEN_STORAGE_KEY`
  → CHỈ whitelist khi call nằm trong `composables/useAuthToken.ts` (nơi 2
  const này được định nghĩa và biết chắc trỏ tới key đúng).
- Mọi thứ khác (identifier khác, biểu thức, literal khác) → vi phạm.

Test PASS ⇔ danh sách vi phạm rỗng.

_Requirements: 11.5_
"""

from __future__ import annotations

import re
from pathlib import Path

# Từ file test này (backend/tests/unit/…), leo 3 cấp về `ideal_qr_tool/`.
_FRONTEND_SRC_DIR = Path(__file__).resolve().parents[3] / "frontend" / "src"

# Giá trị key duy nhất được phép lưu vào localStorage (Requirement 11.5 + 14.7).
_ALLOWED_KEY_LITERAL = "IDEAL_QR_TOOL_AUTH_TOKEN"

# Identifier const đã whitelist — hợp lệ chỉ trong file khai báo chúng.
_ALLOWED_IDENTIFIERS = frozenset({"STORAGE_KEY", "AUTH_TOKEN_STORAGE_KEY"})
_ALLOWED_IDENTIFIER_FILE = "composables/useAuthToken.ts"

# Bắt call `[window.]localStorage.setItem(<arg1>, ...)`.
# Capture group 1 = raw text của argument đầu; dừng ở dấu `,` hoặc `)` gần nhất
# — đủ dùng cho các key đơn giản (string literal / identifier); trường hợp
# phức tạp hơn không thuộc whitelist nên bị flag đúng.
_SETITEM_PATTERN = re.compile(
    r"\blocalStorage\s*\.\s*setItem\s*\(\s*([^,)]+?)\s*[,)]",
    re.DOTALL,
)

# Nhận dạng đúng literal "IDEAL_QR_TOOL_AUTH_TOKEN" (single hoặc double quote).
_LITERAL_AUTH_TOKEN_KEY = re.compile(
    rf"""^(?P<q>['"]){re.escape(_ALLOWED_KEY_LITERAL)}(?P=q)$"""
)


def _iter_frontend_source_files() -> list[Path]:
    """Tất cả file `.ts` và `.vue` trong `frontend/src/` (đệ quy)."""
    files: list[Path] = []
    for pattern in ("*.ts", "*.vue"):
        files.extend(_FRONTEND_SRC_DIR.rglob(pattern))
    return sorted(files)


def _relative_path(file: Path) -> str:
    """Path tương đối từ `frontend/src/`, dùng `/` để so sánh cross-platform."""
    return file.relative_to(_FRONTEND_SRC_DIR).as_posix()


def _classify_call(argument: str, relative_path: str) -> str | None:
    """Trả về mô tả vi phạm nếu call bị flag; `None` nếu thuộc whitelist."""
    stripped = argument.strip()

    # Whitelist 1 — literal đúng "IDEAL_QR_TOOL_AUTH_TOKEN": OK ở bất kỳ đâu.
    if _LITERAL_AUTH_TOKEN_KEY.match(stripped):
        return None

    # Whitelist 2 — identifier const, CHỈ hợp lệ trong file khai báo chúng.
    if stripped in _ALLOWED_IDENTIFIERS:
        if relative_path == _ALLOWED_IDENTIFIER_FILE:
            return None
        return (
            f"identifier `{stripped}` chỉ được phép trong "
            f"`{_ALLOWED_IDENTIFIER_FILE}`, không được dùng ở `{relative_path}`"
        )

    return (
        f"key argument `{stripped}` không thuộc whitelist "
        f"(chỉ chấp nhận literal \"{_ALLOWED_KEY_LITERAL}\" hoặc identifier "
        f"{sorted(_ALLOWED_IDENTIFIERS)} trong `{_ALLOWED_IDENTIFIER_FILE}`)"
    )


def test_frontend_localstorage_setitem_respects_runtime_config_whitelist() -> None:
    """Chỉ `useAuthToken.ts` được phép gọi `localStorage.setItem`, và chỉ
    với key thuộc whitelist (Requirement 11.5)."""
    assert _FRONTEND_SRC_DIR.is_dir(), (
        f"Không tìm thấy `{_FRONTEND_SRC_DIR}` — cấu trúc frontend đã đổi, "
        "cần cập nhật test path."
    )

    violations: list[str] = []

    for file in _iter_frontend_source_files():
        source = file.read_text(encoding="utf-8")
        relative_path = _relative_path(file)

        for match in _SETITEM_PATTERN.finditer(source):
            reason = _classify_call(match.group(1), relative_path)
            if reason is None:
                continue

            line_no = source.count("\n", 0, match.start()) + 1
            violations.append(f"{relative_path}:{line_no} — {reason}")

    assert violations == [], (
        "Frontend gọi `localStorage.setItem` cho key ngoài whitelist "
        "(vi phạm Requirement 11.5):\n  - " + "\n  - ".join(violations)
    )
