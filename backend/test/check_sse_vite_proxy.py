"""check_sse_vite_proxy — verify Vite dev server proxy có buffer SSE không.

Bối cảnh: `dev.sh` cho phép chạy song song `npm run dev` (Vite :5173) proxy
`/api/*` sang uvicorn :8989. Nếu Vite dev proxy buffer SSE response → FE
không nhận event realtime → user phải reload thấy data mới.

Chiến lược:
  1. Start uvicorn thật port 8989.
  2. Start Vite dev (`npm run dev`) port 5173.
  3. Đo latency 1 event `job_status` qua CẢ 2 origin:
       - Direct: http://127.0.0.1:8989/api/events/stream
       - Vite:   http://127.0.0.1:5173/api/events/stream
  4. Nếu latency Vite > latency direct + 500ms → nghi buffer.
  5. Kill cả 2 process khi xong (tránh leak).

Chạy: `.venv/bin/python test/check_sse_vite_proxy.py`
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

BACKEND_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BACKEND_DIR.parent / "frontend"
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8901  # tránh xung đột 8989 nếu user đang dev
VITE_PORT = 5910  # tránh xung đột 5173
BOOT_WAIT_SEC = 30.0


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _wait_port_open(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _start_uvicorn() -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["IDEAL_QR_TOOL_BIND_HOST"] = BACKEND_HOST
    env["IDEAL_QR_TOOL_DB_PATH"] = str(
        BACKEND_DIR / "runtime" / "check_vite_proxy.db"
    )
    _log(f"[boot] uvicorn @ {BACKEND_HOST}:{BACKEND_PORT}")
    proc = subprocess.Popen(
        [
            str(BACKEND_DIR / ".venv/bin/uvicorn"),
            "app.main:app",
            "--host", BACKEND_HOST,
            "--port", str(BACKEND_PORT),
            "--log-level", "warning",
        ],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    if not _wait_port_open(BACKEND_HOST, BACKEND_PORT, BOOT_WAIT_SEC):
        proc.kill()
        raise RuntimeError(f"uvicorn không bind trong {BOOT_WAIT_SEC}s")
    _log("[boot] uvicorn READY")
    return proc


def _start_vite() -> subprocess.Popen:
    """Start Vite dev với BACKEND_PORT override để proxy đúng backend test."""
    env = os.environ.copy()
    env["VITE_BACKEND_PORT"] = str(BACKEND_PORT)
    _log(f"[boot] vite dev @ {BACKEND_HOST}:{VITE_PORT} (proxy → :{BACKEND_PORT})")
    proc = subprocess.Popen(
        ["npx", "vite", "--port", str(VITE_PORT), "--host", BACKEND_HOST],
        cwd=str(FRONTEND_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    if not _wait_port_open(BACKEND_HOST, VITE_PORT, BOOT_WAIT_SEC):
        proc.kill()
        raise RuntimeError(f"vite không bind trong {BOOT_WAIT_SEC}s")
    _log("[boot] vite READY")
    return proc


def _stop(proc: subprocess.Popen, name: str) -> None:
    _log(f"[stop] {name}…")
    try:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
    _log(f"[stop] {name} STOPPED")


def _parse_sse_frame(raw: str) -> tuple[str, dict] | None:
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
    return event_type, json.loads("\n".join(data_lines))


def _measure_first_event_latency(base_url: str, label: str) -> float | None:
    """Mở SSE stream, submit job, đo mốc thời gian tới event `job_status` đầu.

    Trả về latency (ms) hoặc None nếu KHÔNG nhận event trong 8 giây (nghi
    buffer nghiêm trọng).
    """
    _log(f"[{label}] connect SSE {base_url}/api/events/stream")
    stream_client = httpx.Client(timeout=httpx.Timeout(30.0, read=None))
    latency_ms: float | None = None
    try:
        with stream_client.stream(
            "GET",
            f"{base_url}/api/events/stream",
            headers={"Accept": "text/event-stream"},
        ) as resp:
            if resp.status_code != 200:
                _log(f"[{label}] FAIL HTTP {resp.status_code}")
                return None
            # Submit job qua CÙNG base_url (Vite proxy /api → backend).
            nonce = time.monotonic_ns()
            line = f"vite_test_{nonce}@example.com|dummy|JBSWY3DPEHPK3PXP"
            with httpx.Client(timeout=10.0) as api:
                api.put(
                    f"{base_url}/api/settings/ideal.default_issuer",
                    json={"value": "INGB0RUS"},
                )
                t_submit = time.monotonic()
                r = api.post(
                    f"{base_url}/api/jobs",
                    json={"lines": [line], "start": True},
                )
                if r.status_code != 200:
                    _log(f"[{label}] FAIL POST /api/jobs {r.status_code}: {r.text[:200]}")
                    return None
                created = r.json().get("created_job_ids") or []
                if not created:
                    _log(f"[{label}] FAIL skipped: {r.json()}")
                    return None
                job_id = created[0]
                _log(f"[{label}] submitted job {job_id[:8]}")

            # Đọc stream cho tới khi thấy job_status của job vừa tạo, hoặc timeout.
            deadline = time.monotonic() + 8.0
            buffer = ""
            for chunk in resp.iter_raw():
                if time.monotonic() > deadline:
                    _log(f"[{label}] TIMEOUT 8s — không thấy event")
                    return None
                buffer += chunk.decode("utf-8", errors="replace")
                while "\n\n" in buffer:
                    frame, buffer = buffer.split("\n\n", 1)
                    parsed = _parse_sse_frame(frame + "\n\n")
                    if parsed is None:
                        continue
                    event_type, payload = parsed
                    if (
                        event_type == "job_status"
                        and payload.get("job_id") == job_id
                    ):
                        latency_ms = (time.monotonic() - t_submit) * 1000
                        _log(f"[{label}] first job_status +{latency_ms:.0f}ms")
                        return latency_ms
    finally:
        stream_client.close()
    return latency_ms


def main() -> int:
    if not (FRONTEND_DIR / "node_modules").is_dir():
        _log("[skip] frontend/node_modules chưa cài — bỏ qua Vite proxy test")
        _log("        chạy: cd frontend && npm install")
        return 0

    uvicorn_proc = _start_uvicorn()
    vite_proc: subprocess.Popen | None = None
    try:
        vite_proc = _start_vite()

        # Đo direct trước — để backend khởi động ổn định, giảm nhiễu.
        _log("=" * 60)
        direct_ms = _measure_first_event_latency(
            f"http://{BACKEND_HOST}:{BACKEND_PORT}", "DIRECT"
        )
        # Đo qua Vite proxy.
        _log("=" * 60)
        vite_ms = _measure_first_event_latency(
            f"http://{BACKEND_HOST}:{VITE_PORT}", "VITE  "
        )
        _log("=" * 60)

        if direct_ms is None:
            _log("[FAIL] DIRECT không nhận event → backend có vấn đề")
            return 1
        if vite_ms is None:
            _log("[FAIL] VITE  không nhận event trong 8s → PROXY BUFFER SSE")
            _log("       ĐÂY chính là nguyên nhân UI 'không realtime' trong dev.")
            return 1

        diff = vite_ms - direct_ms
        _log(f"[SUMMARY] direct={direct_ms:.0f}ms  vite={vite_ms:.0f}ms  Δ={diff:+.0f}ms")
        if diff > 500:
            _log(f"[FAIL] Vite proxy chậm hơn direct >500ms → CÓ BUFFER")
            return 1
        _log("[PASS] Vite proxy pass-through OK, không phải nguồn bug realtime.")
        return 0
    finally:
        if vite_proc is not None:
            _stop(vite_proc, "vite")
        _stop(uvicorn_proc, "uvicorn")


if __name__ == "__main__":
    sys.exit(main())
