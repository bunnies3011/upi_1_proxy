"""Verify:
1. SSE `job_log` event có `ts` field (fix log realtime)
2. Endpoint POST /api/jobs/stop-all
3. Endpoint POST /api/jobs/rerun-failed
4. Endpoint DELETE /api/jobs/clear?filter=...
"""
import json
import sys
import threading
import time

import httpx


BASE = "http://127.0.0.1:8989"


def log(tag: str, msg: str) -> None:
    print(f"[{tag}] {msg}", flush=True)


def check_endpoint(name: str, func) -> bool:
    log("RUN", name)
    try:
        func()
        log("PASS", name)
        return True
    except AssertionError as e:
        log("FAIL", f"{name} :: assertion: {e}")
        return False
    except Exception as e:
        log("FAIL", f"{name} :: {type(e).__name__}: {e}")
        return False


def tc_stop_all_empty():
    r = httpx.post(f"{BASE}/api/jobs/stop-all", timeout=5)
    assert r.status_code == 200, f"status={r.status_code}"
    body = r.json()
    assert "stopped_count" in body, f"body={body}"


def tc_rerun_failed_empty():
    r = httpx.post(f"{BASE}/api/jobs/rerun-failed", timeout=5)
    assert r.status_code == 200, f"status={r.status_code}"
    body = r.json()
    assert "created_job_ids" in body, f"body={body}"


def tc_clear_invalid_filter():
    r = httpx.delete(f"{BASE}/api/jobs/clear?filter=xxx", timeout=5)
    assert r.status_code == 400, f"status={r.status_code} body={r.text[:200]}"
    body = r.json()
    detail = body.get("detail") or body
    assert detail.get("error_code") == "invalid_filter", f"body={body}"


def tc_clear_all_empty():
    r = httpx.delete(f"{BASE}/api/jobs/clear?filter=all", timeout=5)
    assert r.status_code == 200
    body = r.json()
    assert "deleted_count" in body


def tc_sse_log_has_ts():
    """Submit 1 job → SSE stream nhận `job_log` event với `ts` hợp lệ."""
    # Open SSE stream
    events: list[dict] = []
    stop_evt = threading.Event()

    def reader():
        with httpx.stream(
            "GET",
            f"{BASE}/api/events/stream",
            timeout=httpx.Timeout(60.0, connect=5.0, read=None),
        ) as r:
            r.raise_for_status()
            current_event = "message"
            for line in r.iter_lines():
                if stop_evt.is_set():
                    break
                if line.startswith("event: "):
                    current_event = line[7:].strip()
                elif line.startswith("data: "):
                    try:
                        data = json.loads(line[6:])
                        if current_event == "job_log":
                            events.append({"event": "job_log", "data": data})
                        elif current_event == "job_status":
                            events.append({"event": "job_status", "data": data})
                    except Exception:
                        pass
                if len(events) >= 3:
                    # Enough log events collected
                    break

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(0.5)  # Wait for stream connect

    # Submit 1 job for parsing error → tạo pending → runner start → sinh log
    r = httpx.post(
        f"{BASE}/api/jobs",
        json={
            "payment_method": "ideal",
            "lines": ["bogus@test.com|dummy_pass|"],
        },
        timeout=5,
    )
    assert r.status_code == 200
    body = r.json()
    created = body.get("created_job_ids", [])
    assert created, f"no job created: {body}"
    job_id = created[0]
    log("INFO", f"submitted job_id={job_id[:12]}...")

    # Wait for at least 1 log event
    for _ in range(15):
        time.sleep(0.5)
        if any(e["event"] == "job_log" and e["data"].get("job_id") == job_id for e in events):
            break

    stop_evt.set()

    # Clean up
    try:
        httpx.delete(f"{BASE}/api/jobs/{job_id}/remove", timeout=3)
    except Exception:
        pass

    log_events = [e["data"] for e in events if e["event"] == "job_log" and e["data"].get("job_id") == job_id]
    log("INFO", f"received {len(log_events)} job_log event(s) for {job_id[:12]}")
    assert len(log_events) > 0, "no job_log SSE event received"

    # Check ts field
    for i, ev in enumerate(log_events[:3]):
        assert "ts" in ev, f"log_event[{i}] thiếu ts: {ev}"
        assert isinstance(ev["ts"], (int, float)), f"ts phải là number: {ev['ts']}"
        assert ev["ts"] > 1e9, f"ts phải là epoch seconds (>1e9): {ev['ts']}"
        assert "message" in ev, f"log_event[{i}] thiếu message: {ev}"
        log("INFO", f"log[{i}] ts={ev['ts']:.2f} msg={ev['message'][:80]}")


def main() -> int:
    log("START", "check log realtime + bulk endpoints")
    passes = 0
    fails = 0

    for name, func in [
        ("TC-01: /stop-all empty", tc_stop_all_empty),
        ("TC-02: /rerun-failed empty", tc_rerun_failed_empty),
        ("TC-03: /clear invalid filter → 400", tc_clear_invalid_filter),
        ("TC-04: /clear all empty", tc_clear_all_empty),
        ("TC-05: SSE job_log có ts field", tc_sse_log_has_ts),
    ]:
        if check_endpoint(name, func):
            passes += 1
        else:
            fails += 1

    log("SUMMARY", f"{passes} PASS / {fails} FAIL")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
