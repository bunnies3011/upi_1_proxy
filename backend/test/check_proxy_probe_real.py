"""Verify probe_proxy với REAL network — chạy khi cần diagnose proxy chết.

Usage:
    # Test 1 proxy cụ thể
    python3 test/check_proxy_probe_real.py http://user:pass@1.2.3.4:8080

    # Test nhiều proxy (mỗi dòng 1 proxy trong stdin)
    cat proxies.txt | python3 test/check_proxy_probe_real.py

Output cho mỗi proxy:
    [PASS] host:port :: reason=ok  latency=1.23s
    [FAIL] host:port :: reason=auth (DNS/407)
    [FAIL] host:port :: reason=ip (timeout/reset)

Dùng để confirm classifier phân biệt đúng "auth" vs "ip" trước khi tin
tưởng logic mark_dead. KHÔNG PHẢI test suite chính thức — smoke reality
check khi user báo bug về proxy.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
sys.path.insert(0, str(_BACKEND))

from app.core.proxy_format import materialize_proxy, mask_proxy
from app.core.proxy_health import probe_proxy, is_network_error


async def _probe_one(raw_line: str, endpoint: str, timeout: float) -> None:
    """Probe 1 proxy → in kết quả 1 dòng. Format rác → in FAIL rõ."""
    try:
        url = materialize_proxy(raw_line)
    except ValueError as exc:
        print(f"[FAIL] {mask_proxy(raw_line)} :: bad format ({exc})", flush=True)
        return

    start = time.monotonic()
    ok, reason = await probe_proxy(url, endpoint=endpoint, timeout=timeout)
    elapsed = time.monotonic() - start
    label = "PASS" if ok else "FAIL"
    print(
        f"[{label}] {mask_proxy(url)} :: reason={reason}  latency={elapsed:.2f}s",
        flush=True,
    )


async def main() -> int:
    endpoint = "https://api64.ipify.org"
    timeout = 6.0

    # Args CLI > stdin (script agnostic)
    raw_lines: list[str] = []
    if len(sys.argv) > 1:
        raw_lines = [a for a in sys.argv[1:] if a.strip()]
    else:
        if sys.stdin.isatty():
            print(
                "Usage: python3 test/check_proxy_probe_real.py <proxy>...\n"
                "       cat proxies.txt | python3 test/check_proxy_probe_real.py",
                file=sys.stderr,
            )
            return 2
        raw_lines = [line.strip() for line in sys.stdin if line.strip()]

    if not raw_lines:
        print("Không có proxy để probe.", file=sys.stderr)
        return 2

    print(
        f"Probe {len(raw_lines)} proxy tới {endpoint} (timeout={timeout}s)...",
        flush=True,
    )
    print("-" * 70, flush=True)

    # Probe song song 4 con cùng lúc (giới hạn để không spam network)
    sem = asyncio.Semaphore(4)

    async def _gated(raw: str) -> None:
        async with sem:
            await _probe_one(raw, endpoint, timeout)

    await asyncio.gather(*(_gated(r) for r in raw_lines))

    # Bonus: check `is_network_error` với 1 vài message thực
    print("-" * 70, flush=True)
    print("Classifier check (is_network_error):", flush=True)
    _samples = [
        ("All connection attempts failed", True),
        ("Đăng nhập thất bại: reason=network_error", True),
        ("Đăng nhập thất bại: reason=invalid_credential", False),
        ("ConnectTimeout: ...", True),
        ("some business error", False),
    ]
    for msg, expected in _samples:
        got = is_network_error(msg)
        tag = "PASS" if got == expected else "FAIL"
        print(
            f"  [{tag}] {msg!r} → is_network_error={got} (expected={expected})",
            flush=True,
        )

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
