"""Setup Settings (proxy + issuer + device profile) rồi ghi accounts_e2e.txt.

Sau khi chạy script này, chạy `python -m app.cli run-batch runtime/accounts_e2e.txt`
để test flow với proxy + accounts thật.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.bootstrap import bootstrap_services  # noqa: E402


DEVICE_PROFILE_NL = {
    "language": "nl-NL",
    "timeZone": "Europe/Amsterdam",
    "screenWidth": 1920,
    "screenHeight": 1080,
    "screenAvailableWidth": 1920,
    "screenAvailableHeight": 1055,
    "colorDepth": 24,
}


# host:port:user:pass → http://user:pass@host:port
_PROXIES_RAW = [
    "103.209.60.122:8097:vcM2ybPK6c69611:rwNbiaMl11",
    "103.138.109.162:8226:UqYUQ7GE6c69611:aL8MhMk811",
    "103.145.253.8:8309:O9uuudBW6c69611:cTcFbRyM11",
    "139.99.36.55:8501:7qbdYMjc6c69611:7PdTqwXY11",
    "103.207.36.217:8620:7KiPV2VC6c69611:c7X85S7U11",
]


def _to_http_proxy(raw: str) -> str:
    parts = raw.split(":")
    if len(parts) != 4:
        raise ValueError(f"proxy sai format host:port:user:pass — {raw!r}")
    host, port, user, pw = parts
    return f"http://{user}:{pw}@{host}:{port}"


_ACCOUNTS = [
    "roves_vestige2i+uf7a7n@icloud.com|Anczz123456789@|7IVU46EENTYEQ4RS6UEH7PODSQHTLJJZ",
    "zealous-flint49+e4l26v@icloud.com|Anczz123456789@|JGRCEBRM7CFUKKL2NRHP2EE2DZGWFUV2",
    "spikier_gaze_54+b3ejl01@icloud.com|Anczz123456789@|CPN4HIYKEKCTFACTI7OYUWYNMXQEKFSG",
    "roves_vestige2i+h5l0kw0@icloud.com|Anczz123456789@|7U77ARGBEGZDYUXCQ3OSWVQZOLV6CVTB",
    "zealous-flint49+cxrmk1@icloud.com|Anczz123456789@|B5O37BHBLTN74QHZGVR5O5SSGTRTRWUW",
    "spikier_gaze_54+ud9sc5b@icloud.com|Anczz123456789@|LQTDPUNCOWCPKUK6Z2EBS6GQPMY5RWWA",
    "roves_vestige2i+16jzd@icloud.com|Anczz123456789@|UK2AXZP5APOGMPKR4CNEPCWUIXRK7MKD",
    "zealous-flint49+1ilo5@icloud.com|Anczz123456789@|5AFS4ZE4DDGKKY5QC2QXHC56LTAL7OBA",
    "spikier_gaze_54+ttuwfas@icloud.com|Anczz123456789@|F7QKCGB4FTOGJMO3DEOBHLMLR7AQ5T7H",
    "roves_vestige2i+pr1ja@icloud.com|Anczz123456789@|4B5NVKUS7UEJFF7W52MPR5E6MAH2M5SX",
    "zealous-flint49+1kcd2y3@icloud.com|Anczz123456789@|N6MMRAAN3L6UAFXJZ6F57GJ7LEVP24OO",
    "spikier_gaze_54+at4zw@icloud.com|Anczz123456789@|ARI4REDWQMVP24SCATZZTWWWROMMBYJX",
]


async def main() -> int:
    services = await bootstrap_services(
        db_path=ROOT / "runtime" / "e2e_test.db",
        bind_host="127.0.0.1",
        session_cache_dir=ROOT / "runtime" / "session_cache",
        qr_output_dir=ROOT / "runtime" / "qr",
        validate_startup=False,
    )
    try:
        # 1) ideal.default_issuer
        print("[setup] ideal.default_issuer = RABONL2U", flush=True)
        await services.settings.set("ideal.default_issuer", "RABONL2U")

        # 2) ideal.device_profiles
        print("[setup] ideal.device_profiles = [1 NL desktop profile]", flush=True)
        await services.settings.set("ideal.device_profiles", [DEVICE_PROFILE_NL])

        # 3) proxy.list — convert 5 raw proxy line → http://user:pass@host:port
        proxies = [_to_http_proxy(p) for p in _PROXIES_RAW]
        print(f"[setup] proxy.list = {len(proxies)} proxy", flush=True)
        for i, p in enumerate(proxies):
            # Không log user:pass — chỉ host:port
            host_port = p.split("@", 1)[1] if "@" in p else p
            print(f"[setup]   [{i+1}] http://***@{host_port}", flush=True)
        await services.settings.set("proxy.list", proxies)

        # 4) proxy.rotation_mode = round_robin (mặc định) — 5 proxy đủ để 12
        # job phân bổ đều
        await services.settings.set("proxy.rotation_mode", "round_robin")

        # 5) ideal.max_concurrent = 3 — không dập quá nhiều song song, tránh
        # bị ChatGPT rate limit thêm sau lần blocked lần trước
        await services.settings.set("ideal.max_concurrent", 3)

        # 6) Verify
        di = await services.settings.get("ideal.default_issuer")
        dp = await services.settings.get("ideal.device_profiles")
        pl = await services.settings.get("proxy.list")
        rm = await services.settings.get("proxy.rotation_mode")
        mc = await services.settings.get("ideal.max_concurrent")
        print("[verify] ideal.default_issuer  =", di, flush=True)
        print("[verify] ideal.device_profiles =", len(dp), "items", flush=True)
        print("[verify] proxy.list            =", len(pl), "proxy", flush=True)
        print("[verify] proxy.rotation_mode   =", rm, flush=True)
        print("[verify] ideal.max_concurrent  =", mc, flush=True)

        # 7) Ghi accounts_e2e.txt
        accounts_file = ROOT / "runtime" / "accounts_e2e.txt"
        accounts_file.write_text("\n".join(_ACCOUNTS) + "\n", encoding="utf-8")
        print(f"[write] {accounts_file} ({len(_ACCOUNTS)} accounts)", flush=True)

    finally:
        await services.db_engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
