"""A/B proxy pool helpers for UPI Direct.

Keep raw lines and every materialized URL available for redaction registration,
never for logs.
"""

from __future__ import annotations

import random
import re
from typing import Sequence
from urllib.parse import unquote, urlparse

from app.core.proxy_format import materialize_proxy
from app.core.settings_store import SettingsRepository
from app.payments.upi_direct.models import ProxyPoolSelection, ResolvedProxyPools

_SETTING_PROXY_CHECKOUT = "upi_direct.proxy_checkout"
_SETTING_PROXY_PROMOTION = "upi_direct.proxy_promotion"
_PROXY_USERINFO_RE = re.compile(r"://([^:/@]+):([^@/]+)@")


def _as_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


async def resolve_proxy_pools(
    settings: SettingsRepository,
    *,
    checkout_key: str = _SETTING_PROXY_CHECKOUT,
    promotion_key: str = _SETTING_PROXY_PROMOTION,
) -> ResolvedProxyPools:
    """Load A (checkout) and B (promotion) proxy line lists from settings."""
    checkout = _as_str_list(await settings.get(checkout_key))
    promotion = _as_str_list(await settings.get(promotion_key))
    return ResolvedProxyPools(checkout_lines=checkout, promotion_lines=promotion)


def pick_and_materialize(lines: Sequence[str]) -> ProxyPoolSelection | None:
    """Pick one non-empty line at random and materialize it. Empty pool → None."""
    candidates = [line.strip() for line in lines if line and line.strip()]
    if not candidates:
        return None
    raw = random.choice(candidates)
    return ProxyPoolSelection(raw_line=raw, materialized_url=materialize_proxy(raw))


def proxy_secret_values(lines: Sequence[str]) -> list[str]:
    """Extract credential fragments from raw lines and materialized URLs for redaction."""
    secrets: list[str] = []
    for line in lines:
        if not line:
            continue
        secrets.append(line)
        try:
            materialized = materialize_proxy(line)
        except ValueError:
            materialized = ""
        if materialized:
            secrets.append(materialized)
            secrets.extend(_secrets_from_url(materialized))
        secrets.extend(_secrets_from_raw_line(line))
    # Dedup while preserving order; drop empties.
    seen: set[str] = set()
    out: list[str] = []
    for secret in secrets:
        if secret and secret not in seen:
            seen.add(secret)
            out.append(secret)
    return out


def _secrets_from_url(url: str) -> list[str]:
    found: list[str] = []
    match = _PROXY_USERINFO_RE.search(url)
    if match:
        user = unquote(match.group(1))
        password = unquote(match.group(2))
        if password:
            found.append(password)
        if user and password:
            found.append(f"{user}:{password}")
        if user:
            found.append(user)
    try:
        parsed = urlparse(url)
        if parsed.password:
            found.append(unquote(parsed.password))
        if parsed.username and parsed.password:
            found.append(f"{unquote(parsed.username)}:{unquote(parsed.password)}")
    except Exception:
        pass
    return found


def _secrets_from_raw_line(line: str) -> list[str]:
    """Colon-form `host:port:user:pass` and `user:pass@host:port` fragments."""
    found: list[str] = []
    if "@" in line and "://" not in line:
        cred, _, _host = line.rpartition("@")
        if ":" in cred:
            user, _, password = cred.partition(":")
            if password:
                found.append(password)
                found.append(f"{user}:{password}")
        return found
    parts = line.split(":")
    if len(parts) >= 4 and "://" not in line:
        user, password = parts[2], parts[3]
        if password:
            found.append(password)
            found.append(f"{user}:{password}")
    return found


__all__ = [
    "resolve_proxy_pools",
    "pick_and_materialize",
    "proxy_secret_values",
]
