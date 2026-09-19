"""Stripe ``js_checksum`` + ``rv_timestamp`` token generator (pure Python).

Port từ ``gpt_signup_hybrid/stripe_token.py`` (đã production-tested với UPI
flow). Sau khi migrate httpx → `curl_cffi.requests.AsyncSession` (bypass
Cloudflare TLS fingerprint), signature nhận `http_client` giữ nguyên, chỉ
đổi type annotation.

Thuật toán reverse-engineered (verified qua HAR thực):

    caesar_shift(s, n) = char-by-char (ord - 32 + n) % 95 + 32
    stripe_encode(s)   = url_encode(base64(xor5(s + pad_to_3(' '))))
    js_checksum        = caesar_shift(stripe_encode(JSON.stringify({id})), 11)
    rv_timestamp       = caesar_shift(stripe_encode(JSON.stringify({rvTs,rv,sv})), 11)

Constants (`rvTs/rv/sv`, `shift`) extract LIVE từ Stripe ``custom_checkout.js``
bundle qua pattern match — không hardcode obfuscation names.

Cache disk (giống UPI): theo SHA256 entry source, lifetime dài (bundle chỉ
đổi khi Stripe deploy build mới, thường vài tháng).

_Requirements: 3.3 — 3 token JS-runtime cho Stripe confirm._
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core import http_client as http


# ---------------------------------------------------------------------------
# Pure-Python primitives — verified 10/10 PASS parity với HAR thật
# ---------------------------------------------------------------------------


def caesar_shift(s: str, n: int) -> str:
    """JS: ``(ord - 32 + n) % 95 + 32`` char-by-char."""
    return "".join(chr((ord(c) - 32 + n) % 95 + 32) for c in s)


def stripe_encode(s: str) -> str:
    """Stripe module encoding: pad → XOR-5 → base64 → url-encode.

    Quirk: ``pad = 3 - len(s) % 3`` KHÔNG modulo lại → luôn pad 1..3 spaces,
    kể cả khi ``len(s) % 3 == 0`` (pad 3 spaces).
    """
    pad = 3 - len(s) % 3
    padded = s + " " * pad
    xored = bytes(5 ^ ord(c) for c in padded)
    return urllib.parse.quote(
        base64.b64encode(xored).decode("ascii"),
        safe="-_.!~*'()",
    )


def _js_stringify(obj: dict[str, Any]) -> str:
    """JS ``JSON.stringify`` default — keys insertion order, no whitespace."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# StripeTokenConfig dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StripeTokenConfig:
    """Config extract từ Stripe bundle — thay đổi mỗi build.

    Attributes:
        bundle_hash: SHA256 hash của entry source — invalidate cache khi đổi.
        shift: Caesar shift value (verified hiện tại = 11).
        rv_ts: constants module ``sK`` — version date, vd
            ``"2024-01-01 00:00:00 -0000"``.
        rv: constants module ``dG`` — build hash.
        sv: constants module ``QJ`` — build salt.
    """

    bundle_hash: str
    shift: int
    rv_ts: str
    rv: str
    sv: str

    def __repr__(self) -> str:
        return (
            f"StripeTokenConfig(bundle_hash={self.bundle_hash[:12]}…, "
            f"shift={self.shift}, rv_ts={self.rv_ts!r}, "
            f"rv={self.rv[:12]}…, sv={self.sv[:12]}…)"
        )


class StripeTokenExtractError(Exception):
    """Không extract được config từ bundle — Stripe có thể đã đổi obfuscation."""


# ---------------------------------------------------------------------------
# Pattern matchers — bám thuật toán, không bám tên/ID
# ---------------------------------------------------------------------------

_CAESAR_FN_RE = re.compile(
    r"\b[a-zA-Z_$][\w$]{0,3}\s*=\s*function\s*\(\s*"
    r"[a-zA-Z_$][\w$]{0,3}\s*,\s*"
    r"[a-zA-Z_$][\w$]{0,3}\s*\)\s*\{"
    r"[^{}]*?charCodeAt\([^)]*?\)\s*-\s*32\s*\+\s*[a-zA-Z_$][\w$]{0,3}\s*\)\s*%\s*95\s*\+\s*32"
    r"[^{}]*?\}"
)

_JS_CHECKSUM_RE = re.compile(
    r"(?:return\s*|\b(?P<fn>[a-zA-Z_$][\w$]{0,3})\s*=)?"
    r"(?:\(\s*0\s*,\s*(?P<encmod>[a-zA-Z_$][\w$]{0,3})\s*\.\s*(?P<encfn>[a-zA-Z_$][\w$]{0,3})\s*\)|[a-zA-Z_$][\w$]{0,3})\s*\(\s*"
    r"(?:\(\s*0\s*,\s*[a-zA-Z_$][\w$]{0,3}\s*\.\s*[a-zA-Z_$][\w$]{0,3}\s*\)|[a-zA-Z_$][\w$]{0,3})"
    r"\s*\(\s*JSON\s*\.\s*stringify\s*\(\s*\{\s*id\s*:\s*[a-zA-Z_$][\w$]*\s*\}\s*\)\s*\)"
    r"\s*,\s*(?P<shift>[a-zA-Z_$][\w$]*|\d+)\s*\)"
)

_RV_TIMESTAMP_RE = re.compile(
    r"rv_timestamp\s*:\s*"
    r"(?:\(\s*0\s*,\s*[a-zA-Z_$][\w$]{0,3}\s*\.\s*[a-zA-Z_$][\w$]{0,3}\s*\)|[a-zA-Z_$][\w$]{0,3})"
    r"\s*\(\s*(?:\(\s*0\s*,\s*[a-zA-Z_$][\w$]{0,3}\s*\.\s*[a-zA-Z_$][\w$]{0,3}\s*\)|[a-zA-Z_$][\w$]{0,3})"
    r"\s*\(\s*JSON\s*\.\s*stringify\s*\(\s*\{(?P<keys>[^}]+)\}\s*\)\s*\)"
    r"\s*,\s*(?P<shift>[a-zA-Z_$.][\w$.]*|\d+)\s*\)"
)

_WEBPACK_REQUIRE_RE = re.compile(
    r"\b(?P<lhs>[a-zA-Z_$][\w$]{0,3})\s*=\s*[a-zA-Z_$][\w$]{0,3}\s*\(\s*(?P<id>\d+)\s*\)"
)


def _balanced_brace(body: str, open_pos: int) -> int:
    depth = 0
    in_str = False
    ch_str = ""
    i = open_pos
    while i < len(body):
        c = body[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == ch_str:
                in_str = False
        else:
            if c in ("'", '"', "`"):
                in_str = True
                ch_str = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1


def _extract_webpack_module(body: str, mod_id: int) -> str:
    pattern = re.compile(rf"[\s,{{(]{mod_id}\s*:\s*", re.MULTILINE)
    for m in pattern.finditer(body):
        rest = body[m.end():m.end() + 200]
        sig = re.match(
            r"\s*(?:function\s*\([^)]*\)|\([^)]*\)\s*=>|[a-zA-Z_$][\w$]*\s*=>)\s*\{",
            rest,
        )
        if not sig:
            continue
        brace_open = m.end() + sig.end() - 1
        brace_close = _balanced_brace(body, brace_open)
        if brace_close < 0:
            continue
        return body[m.start():brace_close + 1]
    return ""


def _extract_constants_from_module(mod_body: str) -> dict[str, str]:
    export_map: dict[str, str] = {}
    for m in re.finditer(
        r"([a-zA-Z_$][\w$]{0,3})\s*:\s*function\s*\(\s*\)\s*\{\s*return\s+([a-zA-Z_$][\w$]{0,3})\s*\}",
        mod_body,
    ):
        export_map[m.group(1)] = m.group(2)
    var_values: dict[str, str] = {}
    for m in re.finditer(
        r'\b([a-zA-Z_$][\w$]{0,3})\s*=\s*(?:/\*[^*]*(?:\*(?!/)[^*]*)*\*/\s*)?"([^"]*)"',
        mod_body,
    ):
        var_values.setdefault(m.group(1), m.group(2))
    out: dict[str, str] = {}
    for export_name, local_name in export_map.items():
        if local_name in var_values:
            out[export_name] = var_values[local_name]
    return out


def extract_config(
    bundle_source: str,
    *,
    fallback_sources: list[str] | None = None,
) -> StripeTokenConfig:
    """Parse Stripe bundle → StripeTokenConfig. Fail-fast nếu không match."""
    bundle_hash = hashlib.sha256(bundle_source.encode("utf-8")).hexdigest()

    if not _CAESAR_FN_RE.search(bundle_source):
        raise StripeTokenExtractError(
            "Caesar shift function pattern không tìm thấy — "
            "Stripe có thể đã đổi thuật toán encode."
        )

    js_match = _JS_CHECKSUM_RE.search(bundle_source)
    if not js_match:
        raise StripeTokenExtractError(
            "js_checksum builder pattern không tìm thấy."
        )
    shift_raw = js_match.group("shift")
    if shift_raw.isdigit():
        shift = int(shift_raw)
    else:
        scope = bundle_source[max(0, js_match.start() - 2000):js_match.start()]
        matches = list(
            re.finditer(
                rf"(?:^|[\s,;]){re.escape(shift_raw)}\s*=\s*(\d+)", scope
            )
        )
        shift = int(matches[-1].group(1)) if matches else 11

    rv_match = _RV_TIMESTAMP_RE.search(bundle_source)
    if not rv_match:
        raise StripeTokenExtractError(
            "rv_timestamp builder pattern không tìm thấy."
        )
    rv_shift_raw = rv_match.group("shift")
    if rv_shift_raw.isdigit() and int(rv_shift_raw) != shift:
        raise StripeTokenExtractError(
            f"shift mismatch js_checksum={shift} vs rv_timestamp={rv_shift_raw}"
        )
    keys_literal = rv_match.group("keys")

    member_refs = re.findall(
        r"(\w+)\s*:\s*([a-zA-Z_$][\w$]*)\s*\.\s*([a-zA-Z_$][\w$]*)",
        keys_literal,
    )
    if len(member_refs) != 3:
        raise StripeTokenExtractError(
            f"rv_timestamp keys layout đã đổi — expect 3 refs, got {member_refs}"
        )

    rv_scope_start = max(0, rv_match.start() - 4000)
    rv_scope_end = min(len(bundle_source), rv_match.start() + 4000)
    rv_scope = bundle_source[rv_scope_start:rv_scope_end]
    constants_module_local = member_refs[0][1]

    constants_module_id: int | None = None
    for rm in _WEBPACK_REQUIRE_RE.finditer(rv_scope):
        if rm.group("lhs") == constants_module_local:
            constants_module_id = int(rm.group("id"))
            break
    if constants_module_id is None:
        raise StripeTokenExtractError(
            f"không resolve module ID cho local {constants_module_local!r}"
        )

    mod_body = _extract_webpack_module(bundle_source, constants_module_id)
    if not mod_body:
        for fb in fallback_sources or []:
            mod_body = _extract_webpack_module(fb, constants_module_id)
            if mod_body:
                break
    if not mod_body:
        raise StripeTokenExtractError(
            f"không tìm thấy body module {constants_module_id}"
        )

    constants = _extract_constants_from_module(mod_body)
    expected_keys = {ref[2] for ref in member_refs}
    missing = expected_keys - set(constants)
    if missing:
        raise StripeTokenExtractError(
            f"constants module {constants_module_id} thiếu keys {missing}"
        )

    key_to_member = {ref[0]: ref[2] for ref in member_refs}
    return StripeTokenConfig(
        bundle_hash=bundle_hash,
        shift=shift,
        rv_ts=constants[key_to_member["rvTs"]],
        rv=constants[key_to_member["rv"]],
        sv=constants[key_to_member["sv"]],
    )


def compute_js_checksum(ppage_id: str, *, shift: int = 11) -> str:
    """Compute js_checksum cho ppage_id (từ payment_pages/init.id)."""
    payload = _js_stringify({"id": ppage_id})
    return caesar_shift(stripe_encode(payload), shift)


def compute_rv_timestamp(config: StripeTokenConfig) -> str:
    """Compute rv_timestamp từ constants (đã extract live từ bundle)."""
    payload = _js_stringify(
        {"rvTs": config.rv_ts, "rv": config.rv, "sv": config.sv}
    )
    return caesar_shift(stripe_encode(payload), config.shift)


# ---------------------------------------------------------------------------
# Live bundle fetch (curl_cffi async, cache disk)
# ---------------------------------------------------------------------------

_STRIPE_JS_ENTRY = "https://js.stripe.com/v3/"
_CACHE_ROOT = Path("runtime/cache/stripe_bundles")


def _cache_path(key: str) -> Path:
    p = _CACHE_ROOT / key[:16]
    p.mkdir(parents=True, exist_ok=True)
    return p


async def fetch_bundles_live(
    http_client: http.AsyncSession,
    *,
    use_cache: bool = True,
) -> tuple[str, str]:
    """Fetch (custom_checkout, entry_stripe) live từ Stripe qua curl_cffi.

    Flow:
        1. GET ``https://js.stripe.com/v3/`` → entry source + set cookies.
        2. Parse webpack chunk map từ entry để resolve
           ``custom-checkout-<hash>.js`` fingerprinted URL.
        3. GET ``fingerprinted/js/custom-checkout-<hash>.js`` → bundle chính.

    Cache disk theo SHA256 entry source — bundle rarely changes (Stripe deploy
    build mới vài tháng 1 lần).

    Returns:
        (cc_src, entry_src) — cả 2 non-empty strings.

    Raises:
        StripeTokenExtractError: fetch/parse fail (bundle format changed,
        Cloudflare block, ...).
    """
    common_headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    entry: str = ""
    entry_hash: str = ""
    fetch_error: str = ""

    try:
        resp = await http_client.get(
            _STRIPE_JS_ENTRY,
            headers={**common_headers, "Referer": "https://chatgpt.com/"},
            timeout=30,
        )
        if resp.status_code == 200:
            entry = resp.text or ""
            entry_hash = hashlib.sha256(entry.encode("utf-8")).hexdigest()
        else:
            fetch_error = f"entry stripe.js HTTP {resp.status_code}"
    except http.HTTPError as exc:
        fetch_error = f"entry stripe.js transport: {type(exc).__name__}"

    # Cache-first path (khi fetch OK + hash match): trả cache ngay.
    if use_cache and entry_hash:
        cdir = _cache_path(entry_hash)
        cc_cache = cdir / "custom_checkout.js"
        entry_cache = cdir / "entry.js"
        if cc_cache.exists() and entry_cache.exists():
            return (
                cc_cache.read_text(encoding="utf-8"),
                entry_cache.read_text(encoding="utf-8"),
            )

    # Fetch fail path: nếu có bất kỳ cache dir nào trong _CACHE_ROOT → fallback
    # dùng bundle cache mới nhất. Stripe deploy bundle mới vài tháng 1 lần,
    # bundle cache vẫn hợp lệ để tính js_checksum/rv_timestamp trong khoảng
    # thời gian đó.
    if not entry and use_cache and _CACHE_ROOT.exists():
        cached_dirs = sorted(
            (d for d in _CACHE_ROOT.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        for cdir in cached_dirs:
            cc_cache = cdir / "custom_checkout.js"
            entry_cache = cdir / "entry.js"
            if cc_cache.exists() and entry_cache.exists():
                return (
                    cc_cache.read_text(encoding="utf-8"),
                    entry_cache.read_text(encoding="utf-8"),
                )

    if fetch_error:
        raise StripeTokenExtractError(fetch_error)

    # Parse webpack chunk maps.
    chunk_names: dict[int, str] = {}
    chunk_hashes: dict[int, str] = {}

    name_map_match = re.search(
        r'"fingerprinted/js/"[^}]*?\{([^}]+)\}', entry,
    )
    if name_map_match:
        for em in re.finditer(
            r'(\d+):"([a-z][a-zA-Z0-9_-]+)"', name_map_match.group(1)
        ):
            chunk_names[int(em.group(1))] = em.group(2)

    for m in re.finditer(r'\{(\d+:"[a-f0-9]{20,}",?){3,40}\}', entry):
        for em in re.finditer(r'(\d+):"([a-f0-9]{20,})"', m.group(0)):
            chunk_hashes[int(em.group(1))] = em.group(2)
        if chunk_hashes:
            break

    if not chunk_names or not chunk_hashes:
        raise StripeTokenExtractError(
            f"không parse được webpack chunk map "
            f"(names={len(chunk_names)}, hashes={len(chunk_hashes)})"
        )

    cc_id = next(
        (cid for cid, n in chunk_names.items() if n == "custom-checkout"), None
    )
    if cc_id is None:
        raise StripeTokenExtractError(
            f"không thấy chunk 'custom-checkout' trong map: "
            f"{list(chunk_names.values())}"
        )
    cc_hash = chunk_hashes.get(cc_id)
    if not cc_hash:
        raise StripeTokenExtractError(
            f"không có hash cho chunk {cc_id} (custom-checkout)"
        )
    cc_url = (
        f"https://js.stripe.com/v3/fingerprinted/js/"
        f"custom-checkout-{cc_hash}.js"
    )
    sub_headers = {
        **common_headers,
        "Referer": "https://js.stripe.com/v3/",
        "Sec-Fetch-Dest": "script",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "same-origin",
    }
    r_cc = await http_client.get(cc_url, headers=sub_headers, timeout=60)
    if r_cc.status_code != 200:
        raise StripeTokenExtractError(
            f"custom_checkout HTTP {r_cc.status_code}"
        )
    cc_src = r_cc.text or ""

    if use_cache:
        cdir = _cache_path(entry_hash)
        (cdir / "custom_checkout.js").write_text(cc_src, encoding="utf-8")
        (cdir / "entry.js").write_text(entry, encoding="utf-8")

    return cc_src, entry


async def extract_config_live(
    http_client: http.AsyncSession,
    *,
    use_cache: bool = True,
) -> StripeTokenConfig:
    """Fetch bundles live + extract config. Idempotent qua cache."""
    cc_src, entry_src = await fetch_bundles_live(
        http_client, use_cache=use_cache
    )
    return extract_config(cc_src, fallback_sources=[entry_src])


__all__ = [
    "StripeTokenConfig",
    "StripeTokenExtractError",
    "caesar_shift",
    "stripe_encode",
    "compute_js_checksum",
    "compute_rv_timestamp",
    "extract_config",
    "fetch_bundles_live",
    "extract_config_live",
]
