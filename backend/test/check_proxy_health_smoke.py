"""Smoke check cho preflight probe + ProxyPool changes.

Chạy: python3 test/check_proxy_health_smoke.py

Verify:
    [1/8] Import proxy_health module không lỗi.
    [2/8] ProbeConfig default values đúng.
    [3/8] ProbeConfig.from_snapshot handle missing/invalid keys.
    [4/8] ProxyPool.force_dead set dead=True ngay.
    [5/8] ProxyPool.stats() trả (total, dead, leased_out).
    [6/8] ProxyPool.probe_config property có sẵn.
    [7/8] SettingsRepository whitelist gồm 7 keys probe mới.
    [8/8] acquire_live_proxy với config.enabled=False passthrough pool.acquire.

KHÔNG chạy probe thực (network) — chỉ check interface + logic offline.
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path

# Setup path để import app.* (chạy từ backend/ root)
_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
sys.path.insert(0, str(_BACKEND))


def _pass(tc: str, detail: str = "") -> None:
    print(f"[PASS] {tc} :: {detail}", flush=True)


def _fail(tc: str, detail: str) -> None:
    print(f"[FAIL] {tc} :: {detail}", flush=True)
    traceback.print_exc()


async def main() -> int:
    fails = 0

    # TC-01: Import
    try:
        from app.core.proxy_health import (  # noqa: F401
            ProbeConfig,
            acquire_live_proxy,
            probe_proxy,
            _classify_probe_exc,
            PROBE_REASON_AUTH,
            PROBE_REASON_IP,
            PROBE_REASON_OK,
        )
        _pass("TC-01 import", "proxy_health module imports OK")
    except Exception as exc:  # noqa: BLE001
        _fail("TC-01 import", str(exc))
        return 1  # không thể tiếp tục nếu import fail

    # TC-02: ProbeConfig default
    try:
        cfg = ProbeConfig()
        assert cfg.enabled is True, "enabled default phải True"
        assert cfg.endpoint == "https://api64.ipify.org"
        assert cfg.timeout_seconds == 6
        assert cfg.max_tries == 10
        assert cfg.sid_retry_per_line == 2
        assert cfg.concurrency == 5
        assert cfg.fallback_direct_on_exhausted is True
        _pass("TC-02 ProbeConfig defaults", "match spec")
    except Exception as exc:  # noqa: BLE001
        fails += 1
        _fail("TC-02 ProbeConfig defaults", str(exc))

    # TC-03: from_snapshot handle missing/invalid keys
    try:
        # Empty snapshot → all defaults
        cfg_empty = ProbeConfig.from_snapshot({})
        assert cfg_empty == ProbeConfig(), "empty snapshot phải trả default"

        # Snapshot với vài key set + vài key invalid
        cfg_partial = ProbeConfig.from_snapshot(
            {
                "proxy.probe_enabled": False,
                "proxy.probe_timeout_seconds": 10,
                # invalid type — phải fallback default
                "proxy.probe_max_tries": "not-a-number",
                "proxy.probe_endpoint": "",  # empty string → default
                # bool as int → phải fallback (bool != int semantics)
                "proxy.probe_sid_retry_per_line": True,
            }
        )
        assert cfg_partial.enabled is False
        assert cfg_partial.timeout_seconds == 10
        assert cfg_partial.max_tries == 10, "invalid string → default 10"
        assert cfg_partial.endpoint == "https://api64.ipify.org", "empty string → default"
        assert cfg_partial.sid_retry_per_line == 2, "bool True → default 2 (không phải 1)"
        _pass("TC-03 from_snapshot", "handle missing/invalid keys → default")
    except Exception as exc:  # noqa: BLE001
        fails += 1
        _fail("TC-03 from_snapshot", str(exc))

    # TC-04: ProxyPool.force_dead
    try:
        # Import trong scope để không blow up nếu proxy_pool có bug import
        from app.core.proxy_pool import ProxyPool

        class _FakeSettings:
            pass

        pool = ProxyPool(_FakeSettings())  # type: ignore[arg-type]
        pool.apply_settings(
            {
                "proxy.list": ["host1:8080", "host2:8080:user:pass"],
                "proxy.rotation_mode": "round_robin",
                "proxy.dead_threshold": 5,
            }
        )
        # Force dead line 1 — chỉ 1 lần, ngay lập tức là dead.
        pool.force_dead("host1:8080")
        total, dead, leased = pool.stats()
        assert total == 2 and dead == 1 and leased == 0, (
            f"expected total=2 dead=1 leased=0, got {(total, dead, leased)}"
        )
        _pass("TC-04 force_dead", "1 line force_dead → dead=1")
    except Exception as exc:  # noqa: BLE001
        fails += 1
        _fail("TC-04 force_dead", str(exc))

    # TC-05: mark_dead threshold vẫn hoạt động
    try:
        pool2 = ProxyPool(_FakeSettings())  # type: ignore[arg-type]
        pool2.apply_settings(
            {
                "proxy.list": ["p1:1", "p2:2"],
                "proxy.dead_threshold": 3,
            }
        )
        # 2 lần mark_dead — chưa đủ threshold=3
        pool2.mark_dead("p1:1")
        pool2.mark_dead("p1:1")
        _, dead2, _ = pool2.stats()
        assert dead2 == 0, f"chưa đủ threshold → dead=0, got {dead2}"
        # Lần thứ 3 → dead
        pool2.mark_dead("p1:1")
        _, dead3, _ = pool2.stats()
        assert dead3 == 1, f"đủ threshold → dead=1, got {dead3}"
        _pass("TC-05 mark_dead threshold", "hoạt động sau 3 lần fail")
    except Exception as exc:  # noqa: BLE001
        fails += 1
        _fail("TC-05 mark_dead threshold", str(exc))

    # TC-06: probe_config property
    try:
        pool3 = ProxyPool(_FakeSettings())  # type: ignore[arg-type]
        pool3.apply_settings(
            {
                "proxy.list": [],
                "proxy.probe_enabled": False,
                "proxy.probe_endpoint": "https://ipinfo.io/ip",
                "proxy.probe_max_tries": 7,
            }
        )
        cfg = pool3.probe_config
        assert cfg.enabled is False
        assert cfg.endpoint == "https://ipinfo.io/ip"
        assert cfg.max_tries == 7
        _pass("TC-06 probe_config property", "read từ snapshot đúng")
    except Exception as exc:  # noqa: BLE001
        fails += 1
        _fail("TC-06 probe_config property", str(exc))

    # TC-07: SettingsRepository whitelist gồm 7 keys mới
    try:
        from app.core.db import DbEngine
        from app.core.settings_store import SettingsRepository

        # Không cần init schema — chỉ check whitelist qua _resolve_constraint.
        engine = DbEngine(db_path=":memory:")
        repo = SettingsRepository(engine)
        expected_new_keys = [
            "proxy.probe_enabled",
            "proxy.probe_endpoint",
            "proxy.probe_timeout_seconds",
            "proxy.probe_max_tries",
            "proxy.probe_sid_retry_per_line",
            "proxy.probe_concurrency",
            "proxy.fallback_direct_on_exhausted",
        ]
        for key in expected_new_keys:
            constraint = repo.resolve_constraint(key)
            assert constraint is not None, f"whitelist thiếu {key}"
        _pass("TC-07 whitelist", f"7 keys mới đã đăng ký")
    except AttributeError as exc:
        # `resolve_constraint` có thể public tên khác — thử `_resolve_constraint`
        try:
            from app.core.db import DbEngine
            from app.core.settings_store import SettingsRepository

            engine = DbEngine(db_path=":memory:")
            repo = SettingsRepository(engine)
            for key in [
                "proxy.probe_enabled",
                "proxy.probe_endpoint",
                "proxy.probe_timeout_seconds",
                "proxy.probe_max_tries",
                "proxy.probe_sid_retry_per_line",
                "proxy.probe_concurrency",
                "proxy.fallback_direct_on_exhausted",
            ]:
                repo._resolve_constraint(key)  # noqa: SLF001
            _pass("TC-07 whitelist (via _resolve_constraint)", "7 keys mới đã đăng ký")
        except Exception as exc2:  # noqa: BLE001
            fails += 1
            _fail("TC-07 whitelist", f"{exc} / {exc2}")
    except Exception as exc:  # noqa: BLE001
        fails += 1
        _fail("TC-07 whitelist", str(exc))

    # TC-08: acquire_live_proxy config.enabled=False passthrough
    try:
        from app.core.proxy_health import acquire_live_proxy
        import logging

        pool4 = ProxyPool(_FakeSettings())  # type: ignore[arg-type]
        pool4.apply_settings(
            {
                "proxy.list": ["p1:1234"],
                "proxy.probe_enabled": False,  # DISABLE probe
            }
        )
        cfg_disabled = pool4.probe_config
        assert cfg_disabled.enabled is False
        logger = logging.getLogger("smoke")
        lease = await acquire_live_proxy(
            pool4,
            job_id="smoke-01",
            config=cfg_disabled,
            logger=logger,
        )
        # Không probe → chỉ pool.acquire() thuần → trả lease non-None với
        # materialized_url = template raw (chưa materialize SID).
        assert lease is not None, "config disabled → phải trả lease non-None"
        assert lease.materialized_url == "p1:1234"
        pool4.release(lease)
        _pass("TC-08 disabled passthrough", "config.enabled=False → pool.acquire()")
    except Exception as exc:  # noqa: BLE001
        fails += 1
        _fail("TC-08 disabled passthrough", str(exc))

    # TC-09: is_network_error classifier
    try:
        from app.core.proxy_health import is_network_error

        assert is_network_error("All connection attempts failed") is True
        assert is_network_error("ConnectTimeout: 3s") is True
        assert is_network_error("reason=network_error") is True
        assert is_network_error("Failed to connect to proxy") is True
        assert is_network_error("407 Proxy Authentication Required") is True
        # Business errors — KHÔNG mark
        assert is_network_error("reason=invalid_credential") is False
        assert is_network_error("mfa_required") is False
        assert is_network_error(None) is False
        assert is_network_error("") is False
        _pass("TC-09 is_network_error", "phân biệt network vs business đúng")
    except Exception as exc:  # noqa: BLE001
        fails += 1
        _fail("TC-09 is_network_error", str(exc))

    # TC-10: probe_pool_batch với pool rỗng → dict rỗng
    try:
        from app.core.proxy_health import probe_pool_batch
        import logging

        pool5 = ProxyPool(_FakeSettings())  # type: ignore[arg-type]
        pool5.apply_settings({"proxy.list": []})
        cfg = pool5.probe_config
        results = await probe_pool_batch(
            pool5, cfg, logger=logging.getLogger("smoke")
        )
        assert results == {}, f"pool rỗng phải trả dict rỗng, got {results}"
        _pass("TC-10 probe_pool_batch empty", "pool rỗng → dict rỗng")
    except Exception as exc:  # noqa: BLE001
        fails += 1
        _fail("TC-10 probe_pool_batch empty", str(exc))

    # TC-11: probe_pool_batch với config.enabled=False → dict rỗng
    try:
        pool6 = ProxyPool(_FakeSettings())  # type: ignore[arg-type]
        pool6.apply_settings(
            {"proxy.list": ["p1:1"], "proxy.probe_enabled": False}
        )
        cfg_off = pool6.probe_config
        assert cfg_off.enabled is False
        results = await probe_pool_batch(
            pool6, cfg_off, logger=logging.getLogger("smoke")
        )
        assert results == {}, "probe disabled → dict rỗng"
        _pass("TC-11 probe_pool_batch disabled", "config.enabled=False → skip")
    except Exception as exc:  # noqa: BLE001
        fails += 1
        _fail("TC-11 probe_pool_batch disabled", str(exc))

    # TC-12: probe_pool_batch với bad format → force_dead
    try:
        pool7 = ProxyPool(_FakeSettings())  # type: ignore[arg-type]
        # Line format rác (chỉ có host, không có port) → materialize_proxy raise
        pool7.apply_settings(
            {
                "proxy.list": ["invalid-format-no-port"],
                "proxy.probe_enabled": True,
                # Endpoint không quan trọng vì fail ở materialize
                "proxy.probe_timeout_seconds": 3,
            }
        )
        cfg = pool7.probe_config
        results = await probe_pool_batch(
            pool7, cfg, logger=logging.getLogger("smoke"), total_timeout_seconds=5.0
        )
        _, dead, _ = pool7.stats()
        assert dead == 1, f"bad format phải force_dead, got dead={dead}"
        assert results["invalid-format-no-port"] == (False, "auth")
        _pass("TC-12 probe_pool_batch bad_format", "force_dead cho line rác")
    except Exception as exc:  # noqa: BLE001
        fails += 1
        _fail("TC-12 probe_pool_batch bad_format", str(exc))

    print(f"\n=== SMOKE RESULT: {fails} fail(s) ===", flush=True)
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
