#!/usr/bin/env python3
"""Smoke test proxy_format — cover 5 format + placeholder {SID} + mask.

Verify canonical logic port từ `gpt_signup_hybrid/web/proxy_format.py`.

Chạy: ./.venv/bin/python3 test/smoke_proxy_normalize.py từ backend/.
In [PASS]/[FAIL] mỗi case ngay khi xong.
"""
from __future__ import annotations

import asyncio
import re
import sys
import traceback
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.proxy_format import (  # noqa: E402
    gen_sid,
    has_template,
    mask_proxy,
    materialize_proxy,
    materialize_template,
    sanitize_proxy_text,
)
from app.core.proxy_pool import ProxyPool  # noqa: E402


class _FakeSettings:
    async def list(self) -> dict:
        return {}


# ---------------------------------------------------------------------------
# materialize_proxy — 5 format
# ---------------------------------------------------------------------------


def _test_materialize() -> int:
    failed = 0

    cases: list[tuple[str, str, str]] = [
        # (name, input, expected)
        (
            "TC-01 host:port:user:pass",
            "103.209.60.122:8097:vcM2ybPK6c69611:rwNbiaMl11",
            "http://vcM2ybPK6c69611:rwNbiaMl11@103.209.60.122:8097",
        ),
        (
            "TC-02 host:port",
            "1.2.3.4:8080",
            "http://1.2.3.4:8080",
        ),
        (
            "TC-03 host:port:user",
            "1.2.3.4:8080:onlyuser",
            "http://onlyuser@1.2.3.4:8080",
        ),
        (
            "TC-04 URL form http://user:pass@host:port",
            "http://user:pass@1.2.3.4:8080",
            "http://user:pass@1.2.3.4:8080",
        ),
        (
            "TC-05 URL form socks5://",
            "socks5://user:pass@1.2.3.4:1080",
            "socks5://user:pass@1.2.3.4:1080",
        ),
        (
            "TC-06 credential-at form user:pass@host:port",
            "user:p@ss@1.2.3.4:8080",
            "http://user:p@ss@1.2.3.4:8080",
        ),
        (
            "TC-07 URL-encode password chứa @",
            "1.2.3.4:8080:user:p@w:!",
            # `p@w:!` URL-encoded → `p%40w%3A%21`
            "http://user:p%40w%3A%21@1.2.3.4:8080",
        ),
    ]

    for name, raw, expected in cases:
        try:
            got = materialize_proxy(raw)
            assert got == expected, f"want {expected!r}, got {got!r}"
            print(f"[PASS] {name}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {name} — {type(exc).__name__}: {exc}", flush=True)
            failed += 1

    # Error cases
    tc = "TC-08 empty line → ValueError"
    try:
        try:
            materialize_proxy("   ")
        except ValueError:
            print(f"[PASS] {tc}", flush=True)
        else:
            print(f"[FAIL] {tc} — không raise", flush=True)
            failed += 1
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {tc} — {type(exc).__name__}: {exc}", flush=True)
        failed += 1

    tc = "TC-09 single-part rác → ValueError"
    try:
        try:
            materialize_proxy("just-1-word")
        except ValueError:
            print(f"[PASS] {tc}", flush=True)
        else:
            print(f"[FAIL] {tc} — không raise", flush=True)
            failed += 1
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {tc} — {type(exc).__name__}: {exc}", flush=True)
        failed += 1

    return failed


# ---------------------------------------------------------------------------
# {SID} placeholder — case-insensitive + cùng SID cho user + pass
# ---------------------------------------------------------------------------


def _test_sid_template() -> int:
    failed = 0

    tc = "TC-10 has_template case-insensitive"
    try:
        assert has_template("user-{SID}:pass") is True
        assert has_template("user-{sid}:pass") is True
        assert has_template("user-{Sid}:pass") is True
        assert has_template("user:pass") is False
        print(f"[PASS] {tc}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {tc} — {type(exc).__name__}: {exc}", flush=True)
        failed += 1

    tc = "TC-11 gen_sid → [a-z0-9]{n}"
    try:
        sid = gen_sid(12)
        assert len(sid) == 12
        assert re.fullmatch(r"[a-z0-9]{12}", sid) is not None
        print(f"[PASS] {tc}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {tc} — {type(exc).__name__}: {exc}", flush=True)
        failed += 1

    tc = "TC-12 materialize thay CÙNG 1 SID cho user + pass"
    try:
        url = materialize_proxy("1.2.3.4:8080:user-{SID}:pass-{sid}", sid_len=6)
        # url ≈ http://user-<sid>:pass-<sid>@1.2.3.4:8080
        m = re.fullmatch(
            r"http://user-([a-z0-9]{6}):pass-([a-z0-9]{6})@1\.2\.3\.4:8080",
            url,
        )
        assert m is not None, f"URL không khớp shape: {url!r}"
        assert m.group(1) == m.group(2), f"SID user vs pass khác nhau: {url!r}"
        print(f"[PASS] {tc} — SID={m.group(1)}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {tc} — {type(exc).__name__}: {exc}", flush=True)
        failed += 1

    tc = "TC-13 2 lần materialize → 2 SID khác nhau"
    try:
        u1 = materialize_proxy("1.2.3.4:8080:user-{SID}:pw")
        u2 = materialize_proxy("1.2.3.4:8080:user-{SID}:pw")
        # Xác suất trùng ≈ 36^-8 ~ 3.5e-13; nếu trùng thực sự thì test này
        # coi như flaky nhưng thực tế chấp nhận được.
        assert u1 != u2, f"2 lần gen phải khác SID, cùng={u1}"
        print(f"[PASS] {tc}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {tc} — {type(exc).__name__}: {exc}", flush=True)
        failed += 1

    tc = "TC-14 materialize_template legacy — thay SID caller cung cấp"
    try:
        out = materialize_template("user-{SID}:pw@1.2.3.4:8080", "abc123")
        assert out == "user-abc123:pw@1.2.3.4:8080"
        print(f"[PASS] {tc}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {tc} — {type(exc).__name__}: {exc}", flush=True)
        failed += 1

    return failed


# ---------------------------------------------------------------------------
# mask_proxy + sanitize_proxy_text
# ---------------------------------------------------------------------------


def _test_mask() -> int:
    failed = 0

    cases: list[tuple[str, str | None, str]] = [
        ("TC-15 None → 'direct'", None, "direct"),
        ("TC-16 empty → 'direct'", "", "direct"),
        (
            "TC-17 URL form có credential",
            "http://user:pass@1.2.3.4:8080",
            "http://***@1.2.3.4:8080",
        ),
        (
            "TC-18 URL form no-auth",
            "http://1.2.3.4:8080",
            "http://1.2.3.4:8080",
        ),
        (
            "TC-19 credential-at",
            "user:pass@1.2.3.4:8080",
            "***@1.2.3.4:8080",
        ),
        (
            "TC-20 colon-form 4 phần",
            "1.2.3.4:8080:user:pass",
            "***@1.2.3.4:8080",
        ),
        (
            "TC-21 colon-form 2 phần no-auth",
            "1.2.3.4:8080",
            "1.2.3.4:8080",
        ),
    ]
    for name, raw, expected in cases:
        try:
            got = mask_proxy(raw)
            assert got == expected, f"want {expected!r}, got {got!r}"
            print(f"[PASS] {name}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {name} — {type(exc).__name__}: {exc}", flush=True)
            failed += 1

    tc = "TC-22 sanitize_proxy_text — strip creds trong text bất kỳ"
    try:
        got = sanitize_proxy_text(
            "connect failed: http://user:pass@1.2.3.4:8080/foo (timeout)"
        )
        assert got == "connect failed: http://***@1.2.3.4:8080/foo (timeout)", got
        print(f"[PASS] {tc}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {tc} — {type(exc).__name__}: {exc}", flush=True)
        failed += 1

    return failed


# ---------------------------------------------------------------------------
# ProxyPool integration — apply_settings raw + acquire → materialized URL
# ---------------------------------------------------------------------------


async def _test_pool_integration() -> int:
    """Pool trả raw line, consumer gọi `materialize_proxy` khi cần URL cụ thể.

    Pattern port từ `gpt_signup_hybrid/web/proxy_pool.py.pick()` — pool là
    dumb container (rotate/lease/dead), không hiểu format proxy.
    """
    failed = 0
    pool = ProxyPool(_FakeSettings())  # type: ignore[arg-type]

    pool.apply_settings({
        "proxy.list": [
            "103.209.60.122:8097:vcM2ybPK6c69611:rwNbiaMl11",
            "http://direct:url@2.3.4.5:9000",
            "3.4.5.6:8080",
            "1.1.1.1:8080:user-{SID}:pass",
        ],
        "proxy.rotation_mode": "round_robin",
        "proxy.max_leases_per_proxy": 1,
        "proxy.dead_threshold": 3,
    })

    tc = "TC-23 pool trả raw line + consumer materialize"
    try:
        leases = []
        for i in range(4):
            lease = await pool.acquire(f"job-{i}")
            assert lease is not None, f"lease {i} phải != None"
            leases.append(lease)

        # Pool giữ nguyên raw line (không materialize sẵn).
        raws = [l.materialized_url for l in leases]
        assert (
            "103.209.60.122:8097:vcM2ybPK6c69611:rwNbiaMl11" in raws
        ), f"pool phải giữ raw colon-form: {raws}"
        assert (
            "1.1.1.1:8080:user-{SID}:pass" in raws
        ), f"pool phải giữ template `{{SID}}`: {raws}"

        # Consumer (flow.py) gọi materialize → mỗi lần cho URL httpx-ready.
        urls = [materialize_proxy(l.materialized_url) for l in leases]
        for url in urls:
            assert "://" in url, f"URL phải có scheme sau materialize: {url!r}"
            assert "{sid}" not in url.lower(), f"placeholder chưa thay: {url!r}"

        # 4 raw line phân biệt → 4 URL phân biệt (SID gen ngẫu nhiên).
        assert len(set(urls)) == 4

        print(f"[PASS] {tc}", flush=True)
        for u in urls:
            print(f"        · {u}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {tc} — {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        failed += 1

    return failed


async def _main() -> int:
    failed = 0
    failed += _test_materialize()
    failed += _test_sid_template()
    failed += _test_mask()
    failed += await _test_pool_integration()
    print(f"\n== Done: fail={failed} ==", flush=True)
    return failed


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
