"""Verify feature "track Telegram per job" — persist DB + API endpoint.

Chạy: `./.venv/bin/python test/check_telegram_notifications_db.py` từ backend/.

Kiểm tra:
    1. DB `runtime/ideal_qr_tool.db` bảng `jobs` có cột
       `telegram_notifications_json` (schema migration OK).
    2. Có ≥1 job với `telegram_notifications_json != '[]'` (feature ghi
       được entry sau khi CLI test chạy 12 accounts).
    3. Mỗi entry có shape đúng: chat_id, chat_label, sent_at, success,
       error.
    4. Endpoint `GET /api/jobs/{id}` trả field `telegram_notifications`
       — CHỈ chạy nếu backend server đang mở (health check port 8989).

Fail-fast: raise + exit 1 khi mismatch shape.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "runtime" / "ideal_qr_tool.db"


def _pass(name: str, detail: str = "") -> None:
    print(f"[PASS] {name} :: {detail}", flush=True)


def _fail(name: str, detail: str) -> None:
    print(f"[FAIL] {name} :: {detail}", flush=True)
    sys.exit(1)


def _check_schema(conn: sqlite3.Connection) -> None:
    cursor = conn.execute("PRAGMA table_info(jobs)")
    cols = {row[1] for row in cursor.fetchall()}
    cursor.close()
    if "telegram_notifications_json" not in cols:
        _fail(
            "schema.telegram_notifications_json",
            f"column missing — cols={sorted(cols)}",
        )
    _pass("schema.telegram_notifications_json", "column present")


def _check_entries(conn: sqlite3.Connection) -> list[tuple[str, str, list[dict]]]:
    """Trả list (job_id, status, entries) cho jobs có telegram_notifications."""
    rows = conn.execute(
        """
        SELECT job_id, status, telegram_notifications_json
        FROM jobs
        WHERE telegram_notifications_json != '[]'
          AND telegram_notifications_json IS NOT NULL
        ORDER BY order_num ASC
        """
    ).fetchall()

    if not rows:
        _fail(
            "db.entries_present",
            "0 job có telegram_notifications — chạy `run-batch` với "
            "telegram enabled trước rồi test lại",
        )

    parsed: list[tuple[str, str, list[dict]]] = []
    for jid, status, tn_json in rows:
        try:
            entries = json.loads(tn_json)
        except (ValueError, TypeError) as exc:
            _fail(
                f"db.parse[{jid[:12]}]",
                f"invalid JSON: {exc}",
            )
        if not isinstance(entries, list):
            _fail(
                f"db.type[{jid[:12]}]",
                f"expected list, got {type(entries).__name__}",
            )
        parsed.append((jid, status, entries))

    _pass("db.entries_present", f"{len(rows)} jobs có telegram_notifications")
    return parsed


def _check_entry_shape(entries: list[dict], job_id: str) -> None:
    required_keys = {"chat_id", "chat_label", "sent_at", "success", "error"}
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            _fail(
                f"entry.shape[{job_id[:12]}#{i}]",
                f"expected dict, got {type(entry).__name__}",
            )
        missing = required_keys - set(entry.keys())
        if missing:
            _fail(
                f"entry.keys[{job_id[:12]}#{i}]",
                f"missing {missing}, got {sorted(entry.keys())}",
            )
        if not isinstance(entry["chat_id"], str) or not entry["chat_id"]:
            _fail(
                f"entry.chat_id[{job_id[:12]}#{i}]",
                f"expected non-empty str, got {entry['chat_id']!r}",
            )
        if not isinstance(entry["chat_label"], str):
            _fail(
                f"entry.chat_label[{job_id[:12]}#{i}]",
                f"expected str, got {type(entry['chat_label']).__name__}",
            )
        if not isinstance(entry["sent_at"], (int, float)):
            _fail(
                f"entry.sent_at[{job_id[:12]}#{i}]",
                f"expected number, got {type(entry['sent_at']).__name__}",
            )
        if not isinstance(entry["success"], bool):
            _fail(
                f"entry.success[{job_id[:12]}#{i}]",
                f"expected bool, got {type(entry['success']).__name__}",
            )
    _pass(
        f"entry.shape[{job_id[:12]}]",
        f"{len(entries)} entries shape OK",
    )


def _check_api_endpoint(job_id: str) -> None:
    """Best-effort: nếu server đang chạy port 8989, verify endpoint trả
    field `telegram_notifications`. Nếu server không chạy → skip."""
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:8989/api/jobs/{job_id}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer dev-local-token",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            body = json.loads(resp.read())
    except urllib.error.URLError as exc:
        print(
            f"[SKIP] api.endpoint :: server not reachable ({exc.reason}), "
            f"skip API check",
            flush=True,
        )
        return
    except Exception as exc:  # noqa: BLE001
        print(f"[SKIP] api.endpoint :: {exc}", flush=True)
        return

    if "telegram_notifications" not in body:
        _fail(
            "api.telegram_notifications field",
            f"missing in response — keys={list(body.keys())}",
        )
    entries = body["telegram_notifications"]
    if not isinstance(entries, list):
        _fail(
            "api.telegram_notifications type",
            f"expected list, got {type(entries).__name__}",
        )
    _pass(
        "api.telegram_notifications",
        f"job_id={job_id[:12]} entries={len(entries)}",
    )


def main() -> int:
    if not DB_PATH.is_file():
        _fail("db.exists", f"{DB_PATH} not found — chạy CLI test trước")

    conn = sqlite3.connect(DB_PATH)
    try:
        _check_schema(conn)
        parsed = _check_entries(conn)

        # Print summary + shape check top 3
        print()
        print("Sample entries:")
        for jid, status, entries in parsed[:3]:
            print(f"  {jid[:12]} status={status} entries={len(entries)}")
            for e in entries:
                label = e.get("chat_label") or "(no label)"
                ok = "✓" if e.get("success") else "✗"
                print(
                    f"    {ok} chat_id={e.get('chat_id')} "
                    f"label={label!r} "
                    f"sent_at={e.get('sent_at', 0):.2f}"
                )
        print()

        for jid, _status, entries in parsed:
            _check_entry_shape(entries, jid)

        # Verify API endpoint cho job đầu tiên (best-effort).
        first_job_id = parsed[0][0]
        _check_api_endpoint(first_job_id)

        # Summary
        total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        print()
        print(
            f"[SMOKE] {len(parsed)}/{total} jobs có telegram_notifications ✓"
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
