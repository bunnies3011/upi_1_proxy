"""Strict QR URL policy, public-IP check, bounded download, atomic PNG publish."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.core import http_client as http
from app.payments.upi_direct.errors import QrFetchError

_MAX_PNG_BYTES = 2 * 1024 * 1024
_MAX_HTML_BYTES = 256 * 1024
_MAX_DIM = 4096
_MAX_UPI_URI_LEN = 2048
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _is_valid_upi_uri(value: Any) -> bool:
    """Bounded, control-char-free `upi:` URI — reject oversized / garbage text."""
    if not isinstance(value, str):
        return False
    if not value.lower().startswith("upi:"):
        return False
    if len(value) > _MAX_UPI_URI_LEN:
        return False
    # No whitespace / control characters inside a real UPI intent URI.
    return not any(ord(ch) < 0x20 or ch.isspace() for ch in value)


def is_allowed_qr_png_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
        return False
    if parsed.port not in (None, 443):
        return False
    host = (parsed.hostname or "").lower()
    if host != "qr.stripe.com":
        return False
    path = parsed.path or ""
    return path.endswith(".png")


def is_allowed_hosted_instructions_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
        return False
    if parsed.port not in (None, 443):
        return False
    host = (parsed.hostname or "").lower()
    if host != "payments.stripe.com":
        return False
    return (parsed.path or "").startswith("/upi/instructions/")


async def resolve_public_ips(hostname: str) -> list[str]:
    """Resolve host without blocking the event loop; reject non-public addrs.

    Uses the loop resolver so a stuck DNS server cannot freeze every job or
    defeat the whole-flow `asyncio.timeout()` (a synchronous
    `socket.getaddrinfo` would block the single event loop thread).
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise QrFetchError(detail=f"dns_failed:{exc}") from exc
    ips: list[str] = []
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
            or not ip.is_global
        ):
            raise QrFetchError(detail=f"non_public_ip:{addr}")
        ips.append(addr)
    if not ips:
        raise QrFetchError(detail="no_resolved_ip")
    return ips


async def fetch_bounded_bytes(
    session: http.AsyncSession,
    url: str,
    *,
    max_bytes: int,
    proxy: str | None = None,
) -> bytes:
    """Fetch with redirects disabled and hard byte cap. No default full materialize."""
    # Pre-flight URL policy for known QR hosts
    if is_allowed_qr_png_url(url):
        host = "qr.stripe.com"
    elif is_allowed_hosted_instructions_url(url):
        host = "payments.stripe.com"
    else:
        raise QrFetchError(detail="url_not_allowlisted")

    await resolve_public_ips(host)

    # curl_cffi/httpx: stream=True when available; otherwise cap via content length
    kwargs: dict[str, Any] = {
        "timeout": 30.0,
        "allow_redirects": False,
    }
    if proxy:
        kwargs["proxy"] = proxy

    try:
        # Prefer stream path
        if hasattr(session, "stream"):
            async with session.stream("GET", url, **kwargs) as resp:  # type: ignore[attr-defined]
                if not (200 <= resp.status_code < 300):
                    raise QrFetchError(detail=f"http_{resp.status_code}")
                chunks: list[bytes] = []
                total = 0
                # curl_cffi 0.15 Response streams via `aiter_content()`
                # (there is no `aiter_bytes()` — that is the httpx spelling).
                async for chunk in resp.aiter_content():
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise QrFetchError(detail="body_too_large")
                    chunks.append(chunk)
                return b"".join(chunks)
        response = await session.get(url, **kwargs)
    except QrFetchError:
        raise
    except Exception as exc:
        raise QrFetchError(detail=f"transport:{exc.__class__.__name__}") from exc

    if not (200 <= response.status_code < 300):
        raise QrFetchError(detail=f"http_{response.status_code}")
    content = response.content if hasattr(response, "content") else b""
    if isinstance(content, str):
        content = content.encode("utf-8", errors="replace")
    if len(content) > max_bytes:
        raise QrFetchError(detail="body_too_large")
    return content


def validate_and_reencode_png(raw: bytes) -> bytes:
    """Decode with Pillow, enforce bounds, re-encode PNG."""
    if not raw.startswith(_PNG_MAGIC):
        raise QrFetchError(detail="not_png_magic")
    try:
        from PIL import Image, ImageFile

        ImageFile.LOAD_TRUNCATED_IMAGES = False
        # Decompression bomb protection
        Image.MAX_IMAGE_PIXELS = _MAX_DIM * _MAX_DIM
        with Image.open(BytesIO(raw)) as img:
            img.verify()
        with Image.open(BytesIO(raw)) as img:
            w, h = img.size
            if w > _MAX_DIM or h > _MAX_DIM:
                raise QrFetchError(detail=f"dimensions_too_large:{w}x{h}")
            if img.mode not in ("RGB", "RGBA", "L", "P"):
                img = img.convert("RGB")
            elif img.mode == "P":
                img = img.convert("RGBA")
            out = BytesIO()
            img.save(out, format="PNG", optimize=True)
            return out.getvalue()
    except QrFetchError:
        raise
    except Exception as exc:
        raise QrFetchError(detail=f"png_decode:{exc.__class__.__name__}") from exc


def extract_upi_uri_from_hosted_html(html: bytes, *, max_depth: int = 8) -> str | None:
    """Parse hosted instructions HTML for mobile_auth_url / upi: URI only."""
    if len(html) > _MAX_HTML_BYTES:
        raise QrFetchError(detail="html_too_large")
    text = html.decode("utf-8", errors="replace")
    # Bounded search for data-message / mobile_auth_url / upi:
    import json
    import re

    meta = re.search(
        r'<meta[^>]+id=["\']payload["\'][^>]+data-message=["\']([^"\']+)["\']',
        text,
        re.I,
    )
    if meta:
        import base64

        raw_b64 = meta.group(1)
        try:
            pad = "=" * (-len(raw_b64) % 4)
            decoded = base64.urlsafe_b64decode(raw_b64 + pad)
            payload = json.loads(decoded.decode("utf-8"))
        except Exception:
            payload = None
        if isinstance(payload, dict):
            uri = _find_upi_uri(payload, depth=0, max_depth=max_depth)
            if uri and _is_valid_upi_uri(uri):
                return uri
    # Fallback: direct upi: in HTML — still bounded/validated, never raw text.
    m = re.search(r"(upi:[^\s\"'<>]{1,%d})" % _MAX_UPI_URI_LEN, text, re.I)
    if m and _is_valid_upi_uri(m.group(1)):
        return m.group(1)
    return None


def _find_upi_uri(obj: Any, *, depth: int, max_depth: int) -> str | None:
    if depth > max_depth:
        return None
    if _is_valid_upi_uri(obj):
        return obj  # type: ignore[return-value]
    if isinstance(obj, dict):
        for key in ("mobile_auth_url", "upi_intent_url", "url", "uri"):
            v = obj.get(key)
            if _is_valid_upi_uri(v):
                return v
        for v in obj.values():
            found = _find_upi_uri(v, depth=depth + 1, max_depth=max_depth)
            if found:
                return found
    if isinstance(obj, list):
        for v in obj:
            found = _find_upi_uri(v, depth=depth + 1, max_depth=max_depth)
            if found:
                return found
    return None


def extract_intent_state_from_hosted_html(html: bytes) -> str | None:
    """Parse hosted instructions HTML to inspect Stripe's payment intent_state."""
    if len(html) > _MAX_HTML_BYTES:
        return None
    text = html.decode("utf-8", errors="replace")
    import base64
    import json
    import re

    meta = re.search(
        r'<meta[^>]+id=["\']payload["\'][^>]+data-message=["\']([^"\']+)["\']',
        text,
        re.I,
    )
    if not meta:
        return None
    raw_b64 = meta.group(1)
    try:
        pad = "=" * (-len(raw_b64) % 4)
        decoded = base64.urlsafe_b64decode(raw_b64 + pad)
        payload = json.loads(decoded.decode("utf-8"))
    except Exception:
        return None

    if isinstance(payload, dict):
        state = payload.get("intent_state")
        if isinstance(state, str):
            return state.strip().lower()
    return None


def atomic_write_png(path: Path, png_bytes: bytes) -> Path:
    """Write via same-dir temp + os.replace; clean temp on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(png_bytes)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        return path
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


__all__ = [
    "is_allowed_qr_png_url",
    "is_allowed_hosted_instructions_url",
    "resolve_public_ips",
    "fetch_bounded_bytes",
    "validate_and_reencode_png",
    "extract_upi_uri_from_hosted_html",
    "extract_intent_state_from_hosted_html",
    "atomic_write_png",
    "_MAX_PNG_BYTES",
    "_MAX_HTML_BYTES",
]

