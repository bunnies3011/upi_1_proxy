"""Verify 3 endpoint mới cho task 5: rerun, check-plan, hard delete.

Chỉ kiểm tra endpoint contract (route đúng, status code, body shape),
KHÔNG cần account thật.
"""
import sys
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


def tc_health():
    r = httpx.get(f"{BASE}/api/jobs", timeout=5)
    assert r.status_code == 200, f"status={r.status_code}"


def tc_rerun_404():
    r = httpx.post(f"{BASE}/api/jobs/nonexistent-xyz/rerun", timeout=5)
    assert r.status_code == 404, f"status={r.status_code}"
    body = r.json()
    detail = body.get("detail") or body
    assert detail.get("error_code") == "job_not_found", f"body={body}"


def tc_check_plan_404():
    r = httpx.post(f"{BASE}/api/jobs/nonexistent-xyz/check-plan", timeout=5)
    assert r.status_code == 404, f"status={r.status_code}"
    body = r.json()
    assert body.get("error_code") == "job_not_found", f"body={body}"


def tc_delete_remove_404():
    r = httpx.delete(f"{BASE}/api/jobs/nonexistent-xyz/remove", timeout=5)
    assert r.status_code == 404, f"status={r.status_code}"
    body = r.json()
    assert body.get("error_code") == "job_not_found", f"body={body}"


def tc_full_lifecycle():
    """Submit 1 job giả → rerun (fail vì handler chưa register?) → delete."""
    # Submit account_line invalid để có 1 job pending (nếu payment_method
    # không register handler → 400). Nhưng flow chuẩn là ideal registered.
    # Test này chỉ verify endpoint route dùng job_id đã có.
    r = httpx.post(
        f"{BASE}/api/jobs",
        json={
            "payment_method": "ideal",
            "lines": ["invalid@test.com|dummy_password_123|"],
        },
        timeout=10,
    )
    assert r.status_code == 200, f"submit status={r.status_code} body={r.text[:200]}"
    body = r.json()
    created = body.get("created_job_ids", [])
    if not created:
        skipped = body.get("skipped", [])
        log("SKIP", f"submit created=0, skipped={skipped}")
        return
    job_id = created[0]
    log("INFO", f"created job_id={job_id[:12]}...")

    # Wait a beat để job vào state
    time.sleep(0.3)

    # Test rerun
    r = httpx.post(f"{BASE}/api/jobs/{job_id}/rerun", timeout=5)
    assert r.status_code == 200, f"rerun status={r.status_code} body={r.text[:200]}"
    rerun_body = r.json()
    assert "created_job_ids" in rerun_body, f"body={rerun_body}"
    log("INFO", f"rerun created={len(rerun_body['created_job_ids'])} skipped={len(rerun_body.get('skipped', []))}")

    # Test check-plan (job vừa tạo chưa login → error=no_session_cached)
    r = httpx.post(f"{BASE}/api/jobs/{job_id}/check-plan", timeout=15)
    assert r.status_code == 200, f"check-plan status={r.status_code} body={r.text[:200]}"
    plan_body = r.json()
    assert "plan" in plan_body, f"body={plan_body}"
    log("INFO", f"check-plan plan={plan_body.get('plan')} error={plan_body.get('error')}")

    # Test hard delete
    r = httpx.delete(f"{BASE}/api/jobs/{job_id}/remove", timeout=5)
    assert r.status_code == 200, f"delete status={r.status_code}"
    del_body = r.json()
    assert del_body.get("deleted") is True, f"body={del_body}"
    log("INFO", f"deleted job_id={job_id[:12]}...")

    # Verify không còn trong list
    r = httpx.get(f"{BASE}/api/jobs", timeout=5)
    assert r.status_code == 200
    jobs = r.json()
    for j in jobs:
        if j["job_id"] == job_id:
            raise AssertionError(f"job_id={job_id} vẫn còn sau delete")


def main() -> int:
    log("START", "check task5 endpoints")
    passes = 0
    fails = 0

    for name, func in [
        ("TC-01: health /api/jobs", tc_health),
        ("TC-02: rerun 404", tc_rerun_404),
        ("TC-03: check-plan 404", tc_check_plan_404),
        ("TC-04: delete/remove 404", tc_delete_remove_404),
        ("TC-05: full lifecycle", tc_full_lifecycle),
    ]:
        if check_endpoint(name, func):
            passes += 1
        else:
            fails += 1

    log("SUMMARY", f"{passes} PASS / {fails} FAIL")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
