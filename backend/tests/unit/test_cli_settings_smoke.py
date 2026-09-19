"""Task 43.1: smoke test CLI `settings` sub-command trên DB tạm.

Requirement 15.2 (`settings get/set/dump`) + Requirement 15.7 (exit code
`0` success, `1` config/validation error). Test cô lập DB qua env
`IDEAL_QR_TOOL_DB_PATH=<tmp_path>/test.db` (Requirement 15.3) để KHÔNG đụng
runtime DB của Backend_Service.

3 test case, MỖI case là 1 subprocess RIÊNG (KHÔNG chain command để giữ
đúng semantics smoke-test và tránh state ngầm giữa sub-command):

1. ``settings set ui.input_draft abc`` → exit 0, sau đó
   ``settings get ui.input_draft`` → stdout chứa ``abc``.
2. ``settings set ideal.max_concurrent not-a-number`` → exit 1, stderr
   chứa ``ideal.max_concurrent`` (parse int fail — xem
   ``settings_cmd._parse_value`` phần constraint ``int``).
3. ``settings dump`` với DB rỗng → exit 0, stdout parse được JSON hợp lệ
   (``--format json`` để có JSON deterministic, empty dict ``{}``).

Tại sao subprocess-per-case (không gộp):
- Mỗi command CLI mở/đóng ``DbEngine`` riêng — chạy tuần tự trong
  cùng process pytest sẽ dính lifecycle asyncio loop giữa các test.
- Fail-Fast: nếu case 2 làm treo (regression argparse), case 3 vẫn phải
  chạy được và fail độc lập.

Tại sao ``timeout=10`` explicit:
- Nếu CLI stuck (ví dụ regression khiến ``DbEngine.close()`` không trả),
  ``subprocess.TimeoutExpired`` sẽ raise sau 10s và test fail với message
  rõ ràng thay vì treo pytest worker vô hạn (Project rule "chạy test
  đừng để stuck").

Tại sao dùng ``sys.executable`` thay vì literal ``.venv/bin/python``:
- ``sys.executable`` là interpreter hiện đang chạy pytest — đúng venv,
  đúng site-packages, portable giữa CI/dev/máy khác nhau. Literal path
  ``.venv/bin/python`` gãy khi pytest được gọi qua path khác.

Tại sao cwd = ``backend/``:
- ``python -m app.cli`` cần ``app`` package hiện diện trong ``sys.path``.
  Package ``app`` nằm ở ``backend/app/``, nên chạy từ ``backend/`` để
  Python auto-thêm CWD vào ``sys.path[0]``.

_Requirements: 15.2, 15.7_
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


# Timeout cứng cho MỖI subprocess CLI — 10s dư thừa cho settings CRUD
# (bootstrap + 1 SQL query + close < 2s trên máy dev bình thường), đủ
# margin cho CI chậm nhưng KHÔNG treo pytest nếu regression.
_SUBPROCESS_TIMEOUT_SEC = 10.0


def _backend_root() -> Path:
    """Trả về ``backend/`` — cwd để ``python -m app.cli`` tìm được ``app`` pkg.

    File hiện tại nằm ở ``backend/tests/unit/test_cli_settings_smoke.py``
    → ``.parent`` là ``unit/``, ``.parent`` là ``tests/``, ``.parent`` nữa
    là ``backend/``.
    """
    return Path(__file__).resolve().parent.parent.parent


def _run_cli(
    args: list[str],
    *,
    db_path: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Chạy 1 subprocess ``python -m app.cli <args>`` với DB tạm cô lập.

    Args:
        args: Argv sau ``app.cli`` — ví dụ
            ``["settings", "set", "ui.input_draft", "abc"]``.
        db_path: Đường dẫn file SQLite tạm (thường ``tmp_path / "test.db"``);
            được truyền qua env ``IDEAL_QR_TOOL_DB_PATH`` để CLI resolve
            (Requirement 15.3). Bootstrap sẽ ``mkdir parents=True`` cho
            ``db_path.parent`` nên KHÔNG cần tạo trước.
        extra_env: Env bổ sung (override / thêm) — merge lên ``os.environ``.

    Returns:
        ``CompletedProcess`` với ``stdout``/``stderr`` là ``str`` (decoded UTF-8).
        Test tự assert ``returncode``/nội dung.

    Raises:
        pytest.fail: Khi CLI vượt ``_SUBPROCESS_TIMEOUT_SEC`` — báo lỗi rõ
            ràng "CLI stuck" kèm command để dev debug, thay vì để pytest
            treo (Project rule).
    """
    env = dict(os.environ)
    env["IDEAL_QR_TOOL_DB_PATH"] = str(db_path)
    # Prevent Python từ mở __pycache__ ở tmp_path — smoke test không cần
    # cache và tránh side-effect ghi file vào tmp_path do worker khác.
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    if extra_env:
        env.update(extra_env)

    cmd = [sys.executable, "-m", "app.cli", *args]
    try:
        return subprocess.run(  # noqa: S603 — cmd đã kiểm soát, không có shell=True
            cmd,
            cwd=str(_backend_root()),
            env=env,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired as ex:
        pytest.fail(
            "CLI stuck: subprocess vượt "
            f"{_SUBPROCESS_TIMEOUT_SEC}s timeout — "
            f"cmd={cmd!r}, cwd={_backend_root()!r}, "
            f"partial_stdout={ex.stdout!r}, partial_stderr={ex.stderr!r}"
        )


def _format_debug(result: subprocess.CompletedProcess[str]) -> str:
    """Format subprocess result thành block dễ đọc cho assert message."""
    return (
        f"return_code={result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_settings_set_then_get_ui_input_draft_roundtrip(tmp_path: Path) -> None:
    """Case 1 (Requirement 15.2, 15.7): set → get round-trip trên DB tạm.

    Kỳ vọng:
    - ``settings set ui.input_draft abc`` → exit 0.
    - ``settings get ui.input_draft`` (chạy subprocess RIÊNG) → exit 0,
      stdout chứa literal ``abc`` (không assert exact format vì
      TextFormatter render ``ui.input_draft = "abc"`` với JSON-quoted
      value — chỉ cần substring match để test không phụ thuộc format
      exact).
    """
    db_path = tmp_path / "test.db"

    set_result = _run_cli(
        ["settings", "set", "ui.input_draft", "abc"],
        db_path=db_path,
    )
    assert set_result.returncode == 0, (
        "settings set ui.input_draft phải exit 0:\n"
        f"{_format_debug(set_result)}"
    )

    # Verify DB file được tạo sau set → chứng minh CLI đã persist thật.
    assert db_path.exists(), (
        "SQLite file phải được tạo sau settings set:\n"
        f"{_format_debug(set_result)}"
    )

    get_result = _run_cli(
        ["settings", "get", "ui.input_draft"],
        db_path=db_path,
    )
    assert get_result.returncode == 0, (
        "settings get ui.input_draft phải exit 0:\n"
        f"{_format_debug(get_result)}"
    )
    assert "abc" in get_result.stdout, (
        "stdout của settings get phải chứa giá trị vừa set ('abc'):\n"
        f"{_format_debug(get_result)}"
    )


def test_settings_set_invalid_int_exit_1_and_reports_key(tmp_path: Path) -> None:
    """Case 2 (Requirement 15.2, 15.7): parse int fail → exit 1 + key trong stderr.

    ``ideal.max_concurrent`` có TypeConstraint ``int`` (whitelist —
    Requirement 11.7). Truyền value ``not-a-number`` khiến
    ``settings_cmd._parse_value`` gọi ``int()`` raise ``ValueError`` →
    handler in message ``"Không parse được value cho 'ideal.max_concurrent'
    (type=int): ..."`` ra stderr và return exit code 1.
    """
    db_path = tmp_path / "test.db"

    result = _run_cli(
        ["settings", "set", "ideal.max_concurrent", "not-a-number"],
        db_path=db_path,
    )

    assert result.returncode == 1, (
        "Parse int fail phải trả exit code 1 (config error, "
        "Requirement 15.7):\n"
        f"{_format_debug(result)}"
    )
    assert "ideal.max_concurrent" in result.stderr, (
        "stderr phải chứa key 'ideal.max_concurrent' để dev locate lỗi "
        "(Requirement 15.2):\n"
        f"{_format_debug(result)}"
    )


def test_settings_dump_fresh_db_returns_valid_json(tmp_path: Path) -> None:
    """Case 3 (Requirement 15.2, 15.7): dump DB fresh → exit 0 + JSON hợp lệ.

    Dùng ``--format json`` để đảm bảo output là JSON deterministic có thể
    parse chặt. Trên DB tạm CHƯA có key user-set, ``bootstrap_services``
    vẫn seed default cho namespace ``ideal.*`` (Task 5 + Task 20.3 —
    ``register_ideal_namespace`` register default value cho các key có
    ``TypeConstraint.default`` non-None, ví dụ ``ideal.known_issuers``).
    Đây là behavior đúng nên test KHÔNG assert dict rỗng — chỉ verify:

    - exit 0 (Requirement 15.7).
    - stdout parse được JSON (Requirement 15.2 — ``json`` format phải là
      1 JSON object độc lập, không mixed prose).
    - Kết quả parse là dict (map key → value theo hợp đồng
      ``JsonFormatter.format_settings_map``).
    """
    db_path = tmp_path / "test.db"

    result = _run_cli(
        ["--format", "json", "settings", "dump"],
        db_path=db_path,
    )

    assert result.returncode == 0, (
        f"settings dump phải exit 0 trên DB tạm:\n{_format_debug(result)}"
    )

    stdout = result.stdout.strip()
    assert stdout, (
        "stdout không được rỗng — JsonFormatter tối thiểu phải in '{}':\n"
        f"{_format_debug(result)}"
    )

    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as ex:
        pytest.fail(
            "stdout của settings dump phải là JSON hợp lệ "
            f"(Requirement 15.2): {ex}\n{_format_debug(result)}"
        )

    assert isinstance(parsed, dict), (
        "Dump phải là JSON object (dict) — key → value map:\n"
        f"{_format_debug(result)}"
    )
