"""check_sse_live — smoke test realtime SSE END-TO-END qua HTTP thật.

Mục đích: xác nhận user có bug "phải reload mới thấy data" là do đâu:
  - BE không broadcast → không có event nào chảy trong stream.
  - BE broadcast nhưng chậm → latency > 2s.
  - BE broadcast OK → bug nằm ở FE (proxy / parse / dispatch).

Chiến lược:
  1. Start uvicorn thật ở port 8901 (tránh xung đột 8989 dev).
  2. Mở SSE stream `GET /api/events/stream` bằng curl_cffi (streaming).
  3. Submit 1 job POST `/api/jobs` với 1 line account giả (sẽ fail nhanh
     ở step check credential — đủ để thấy job_status pending → running
     → error trong <2s).
  4. Log realtime từng event đến (đi kèm mốc thời gian & delta ms) —
     giúp phát hiện buffer / delay.
  5. Kill uvicorn khi xong (không leak process).

Chạy: `.venv/bin/python test/check_sse_live.py`
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
BACKEND_DIR = Path(__file__).resolve().parent.parent
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8901
SERVER_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"
BOOT_WAIT_SEC = 15.0
EVENT_WAIT_SEC = 8.0


def _log(msg: str) -> None:
    """In log kèm timestamp ms, flush ngay để realtime."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _wait_port_open(host: str, port: int, timeout: float) -> bool:
    """Poll TCP port đến khi bind hoặc timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _start_uvicorn() -> subprocess.Popen:
    """Start uvicorn với PYTHONUNBUFFERED để log stdout real-time."""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["IDEAL_QR_TOOL_BIND_HOST"] = SERVER_HOST
    # DB path riêng để không đụng data thật của user.
    env["IDEAL_QR_TOOL_DB_PATH"] = str(
        BACKEND_DIR / "runtime" / "check_sse_live.db"
    )
    _log(f"start uvicorn @ {SERVER_URL}, DB={env['IDEAL_QR_TOOL_DB_PATH']}")
    proc = subprocess.Popen(
        [
            str(BACKEND_DIR / ".venv/bin/uvicorn"),
            "app.main:app",
            "--host", SERVER_HOST,
            "--port", str(SERVER_PORT),
            "--log-level", "warning",
        ],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    if not _wait_port_open(SERVER_HOST, SERVER_PORT, BOOT_WAIT_SEC):
        proc.kill()
        out = proc.stdout.read() if proc.stdout else ""
        raise RuntimeError(f"uvicorn không bind port trong {BOOT_WAIT_SEC}s\n{out}")
    _log("uvicorn READY")
    return proc


def _stop_uvicorn(proc: subprocess.Popen) -> None:
    _log("stopping uvicorn…")
    try:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)
    _log("uvicorn stopped")


def _sample_account_line() -> str:
    """Line account fake — chỉ để trigger flow, KHÔNG cần login OK.

    Format `email|password|totp` khớp `mask_account_line` — flow sẽ fail
    sớm ở bước check credential/login (error status), đủ để test SSE
    phát ít nhất 2 event (pending → error) trong vài giây.

    Nonce theo `time.monotonic_ns()` để tránh dedup `job_already_exists`
    giữa các lần run script.
    """
    nonce = time.monotonic_ns()
    return f"check_sse_{nonce}@example.com|dummy_password|JBSWY3DPEHPK3PXP"


def _parse_sse_frame(raw: str) -> tuple[str, dict] | None:
    """Parse 1 frame `event: X\\ndata: Y\\n\\n`. Trả None nếu là keepalive."""
    lines = raw.split("\n")
    event_type = "message"
    data_lines: list[str] = []
    for l in lines:
        if not l or l.startswith(":"):
            continue
        if l.startswith("event:"):
            event_type = l[len("event:"):].lstrip()
        elif l.startswith("data:"):
            data_lines.append(l[len("data:"):].lstrip())
    if not data_lines:
        return None
    payload = json.loads("\n".join(data_lines))
    return event_type, payload


def _run_scenario() -> int:
    """Chạy scenario chính. Trả 0 nếu OK, 1 nếu thấy vấn đề realtime.

    Chạy SSE stream + submit trong CÙNG 1 luồng chính bằng cách:
      - Mở stream context manager (giữ mở suốt quá trình).
      - Trước khi vào loop đọc, submit job qua httpx.Client riêng.
      - Đọc stream tối đa `EVENT_WAIT_SEC` giây kể từ mốc submit.
    """
    stream_client = httpx.Client(timeout=httpx.Timeout(30.0, read=None))
    _log(f"open SSE stream {SERVER_URL}/api/events/stream")
    t_stream_open = time.monotonic()
    with stream_client.stream(
        "GET",
        f"{SERVER_URL}/api/events/stream",
        headers={"Accept": "text/event-stream"},
    ) as resp:
        if resp.status_code != 200:
            _log(f"[FAIL] SSE stream trả HTTP {resp.status_code}")
            stream_client.close()
            return 1
        _log(
            f"[PASS] SSE stream connected in "
            f"{(time.monotonic()-t_stream_open)*1000:.0f}ms"
        )

        # Submit 1 job qua client riêng — SSE stream vẫn giữ mở.
        with httpx.Client(timeout=10.0) as api:
            s = api.get(f"{SERVER_URL}/api/settings/ideal.default_issuer")
            if s.status_code == 404 or not (s.json() or {}).get("value"):
                _log("seed default_issuer=INGB0RUS (test convention)")
                api.put(
                    f"{SERVER_URL}/api/settings/ideal.default_issuer",
                    json={"value": "INGB0RUS"},
                )

            t_submit = time.monotonic()
            _log("POST /api/jobs (1 line fake account)")
            r = api.post(
                f"{SERVER_URL}/api/jobs",
                json={"lines": [_sample_account_line()], "start": True},
            )
            if r.status_code != 200:
                _log(f"[FAIL] submit job trả HTTP {r.status_code}: {r.text[:200]}")
                stream_client.close()
                return 1
            created = r.json().get("created_job_ids") or []
            _log(
                f"[PASS] job created ids={created} in "
                f"{(time.monotonic()-t_submit)*1000:.0f}ms"
            )
            if not created:
                _log(f"[FAIL] skipped: {r.json()}")
                stream_client.close()
                return 1
            job_id = created[0]

        _log(f"đọc SSE stream tối đa {EVENT_WAIT_SEC}s…")
        got_status_event = False
        got_log_event = False
        got_status_first_at: float | None = None
        got_log_first_at: float | None = None
        buffer = ""
        deadline = time.monotonic() + EVENT_WAIT_SEC
        event_count = 0
        for chunk in resp.iter_raw():
            now = time.monotonic()
            if now > deadline:
                _log(f"⏱️  hết {EVENT_WAIT_SEC}s → dừng đọc")
                break
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                parsed = _parse_sse_frame(frame + "\n\n")
                if parsed is None:
                    _log(
                        f"  · keepalive/comment "
                        f"({(now-t_submit)*1000:.0f}ms sau submit)"
                    )
                    continue
                event_type, payload = parsed
                event_count += 1
                delta_ms = (now - t_submit) * 1000
                job_id_evt = str(payload.get("job_id", "?"))[:8]
                _log(
                    f"[EVT-{event_count:02d}] +{delta_ms:>6.0f}ms  "
                    f"type={event_type:<15} job={job_id_evt}  "
                    f"status={payload.get('status','—')}"
                )
                if payload.get("job_id") == job_id:
                    if event_type == "job_status" and not got_status_event:
                        got_status_event = True
                        got_status_first_at = now
                    if event_type == "job_log" and not got_log_event:
                        got_log_event = True
                        got_log_first_at = now
                if got_status_event and got_log_event:
                    _log("→ đã nhận đủ status + log cho job vừa submit")
                    break
            if got_status_event and got_log_event:
                break

    stream_client.close()

    # 4. Verdict
    print("", flush=True)
    _log("=" * 60)
    if got_status_event:
        assert got_status_first_at is not None
        latency = (got_status_first_at - t_submit) * 1000
        marker = "PASS" if latency < 2000 else "SLOW"
        _log(f"[{marker}] first job_status trong +{latency:.0f}ms (budget 2000ms)")
    else:
        _log("[FAIL] KHÔNG nhận được job_status nào cho job vừa submit trong "
             f"{EVENT_WAIT_SEC}s — SSE stream không realtime")
    if got_log_event:
        assert got_log_first_at is not None
        latency = (got_log_first_at - t_submit) * 1000
        marker = "PASS" if latency < 2000 else "SLOW"
        _log(f"[{marker}] first job_log trong +{latency:.0f}ms (budget 2000ms)")
    else:
        _log("[WARN] không nhận được job_log — có thể job fail trước khi log flow start")

    _log(f"tổng {event_count} event nhận trong stream")
    return 0 if got_status_event else 1


def main() -> int:
    proc = _start_uvicorn()
    try:
        return _run_scenario()
    finally:
        _stop_uvicorn(proc)


if __name__ == "__main__":
    sys.exit(main())
