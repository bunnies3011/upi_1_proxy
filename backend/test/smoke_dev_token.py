"""Smoke test: verify VITE dev-token injection + backend auth flow.

Bước 1: GET /src/main.ts qua Vite dev → phải có chuỗi 'VITE_DEV_AUTH_TOKEN'
        (Vite chưa thay thế ở transform layer, chỉ thay khi bundle client — dev
        chạy qua transform inline nên chuỗi vẫn thấy được trong source).

Bước 2: Vite thay `import.meta.env.VITE_DEV_AUTH_TOKEN` thành literal string
        trong transformed module — request `/@id/__x00__/plugin-vue:...` hoặc
        `/src/main.ts` với accept text/javascript sẽ có `"dev-local-token"`.

Bước 3: GET /api/settings (proxy 5173 → 8989) với X-Auth-Token header phải 200.

Bước 4: GET /api/settings không có header → 401 (fail-closed).

Chạy: `python3 test/smoke_dev_token.py` từ ideal_qr_tool/backend.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request

VITE_ROOT = "http://localhost:5173"
DEV_TOKEN = "dev-local-token"


def _fetch(url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")


def _pass(tc: str, detail: str) -> None:
    print(f"[PASS] {tc} — {detail}", flush=True)


def _fail(tc: str, detail: str) -> None:
    print(f"[FAIL] {tc} — {detail}", flush=True)


def main() -> int:
    exit_code = 0

    # TC-01: Vite serve /src/main.ts phải có chuỗi VITE_DEV_AUTH_TOKEN đã inline
    tc = "TC-01 vite-transform"
    print(f"[RUN ] {tc} — GET /src/main.ts", flush=True)
    status, body = _fetch(f"{VITE_ROOT}/src/main.ts")
    if status != 200:
        _fail(tc, f"HTTP {status}")
        exit_code = 1
    elif DEV_TOKEN in body:
        _pass(tc, f"main.ts contains {DEV_TOKEN!r} (Vite đã inline env)")
    else:
        _fail(
            tc,
            "main.ts KHÔNG có literal token đã inline — VITE_DEV_AUTH_TOKEN "
            "chưa export đúng, kiểm tra dev.sh",
        )
        exit_code = 1

    # TC-02: /api/settings proxy qua Vite → backend, có header → 200
    tc = "TC-02 backend-auth-ok"
    print(f"[RUN ] {tc} — GET /api/settings với X-Auth-Token", flush=True)
    status, body = _fetch(
        f"{VITE_ROOT}/api/settings",
        headers={"X-Auth-Token": DEV_TOKEN},
    )
    if status == 200:
        _pass(tc, f"HTTP 200, body length={len(body)}")
    else:
        _fail(tc, f"HTTP {status}, body={body[:200]!r}")
        exit_code = 1

    # TC-03: /api/settings không có header → 401
    tc = "TC-03 backend-auth-reject"
    print(f"[RUN ] {tc} — GET /api/settings KHÔNG có X-Auth-Token", flush=True)
    status, _ = _fetch(f"{VITE_ROOT}/api/settings")
    if status == 401:
        _pass(tc, "HTTP 401 fail-closed đúng")
    else:
        _fail(tc, f"HTTP {status} (mong đợi 401)")
        exit_code = 1

    print(f"\n[DONE] exit_code={exit_code}", flush=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
