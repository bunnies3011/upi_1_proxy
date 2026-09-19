"""Shared account/session models for the ChatGPT module (`payments/_chatgpt/`).

These types are consumed by BOTH the iDEAL flow and (later) the UPI flow, so
they live in the shared module. `ChatgptAccount` is the single parsed-account
model — iDEAL re-exports it as `IdealParsedAccount` for backward compatibility.

Payment_Module_Boundary: imports only `app.core.payment_flow` (base marker +
`AccountLineError`). It imports NOTHING from `app.payments.ideal`.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.payment_flow import AccountLineError, ParsedAccount


@dataclass(frozen=True)
class ChatgptAccount(ParsedAccount):
    """Parsed result of one valid account line (Requirement 1.1).

    Accepts two input formats:
        1. ``email|password|totp_secret`` — ``password`` non-empty,
           ``totp_secret`` optional (empty → account without MFA); sets
           ``email``/``password``/``totp_secret``, ``access_token = None``.
        2. ``email|access_token`` — sets ``email``/``access_token``,
           ``password = totp_secret = None``.

    Pure data holder — no parsing in ``__init__``. See ``parse_account_line``.
    """

    email: str
    password: str | None
    totp_secret: str | None
    access_token: str | None


def _looks_like_email(candidate: str) -> bool:
    """Minimal email format check (Requirement 1.1): has ``@`` with non-empty
    local and domain parts. Not full RFC 5322 — the backend only uses email as
    an identifier (it never sends mail); Stripe/ChatGPT validate further.
    """
    if not candidate or "@" not in candidate:
        return False
    local, _, domain = candidate.partition("@")
    return bool(local) and bool(domain) and "@" not in domain


def parse_account_line(raw_line: str) -> "ChatgptAccount | AccountLineError":
    """Parse one raw account line per Requirement 1.1.

    Returns `AccountLineError` (does NOT raise) for any format error — the job
    manager aggregates the list of skipped lines with reasons (Requirement 8.2).

    Leading/trailing whitespace of each part is stripped; whitespace INSIDE the
    email/token/password is preserved (do not silently mutate user input).

    Args:
        raw_line: Raw line (not yet trimmed). Empty/whitespace-only → error.
    """
    if raw_line is None:
        return AccountLineError(line="", reason="empty_line")

    stripped = raw_line.strip()
    if not stripped:
        return AccountLineError(line=raw_line, reason="empty_line")

    parts = [segment.strip() for segment in stripped.split("|")]

    if len(parts) == 3:
        email, password, totp_secret = parts
        if not _looks_like_email(email):
            return AccountLineError(line=raw_line, reason="invalid_email")
        if not password:
            return AccountLineError(
                line=raw_line, reason="password_required_for_3_part_format"
            )
        # totp_secret may be empty (R1.1) — account without MFA.
        return ChatgptAccount(
            raw_line=raw_line,
            email=email,
            password=password,
            totp_secret=totp_secret if totp_secret else None,
            access_token=None,
        )

    if len(parts) == 2:
        email, access_token = parts
        if not _looks_like_email(email):
            return AccountLineError(line=raw_line, reason="invalid_email")
        if not access_token:
            return AccountLineError(
                line=raw_line, reason="access_token_required_for_2_part_format"
            )
        return ChatgptAccount(
            raw_line=raw_line,
            email=email,
            password=None,
            totp_secret=None,
            access_token=access_token,
        )

    return AccountLineError(
        line=raw_line,
        reason=f"invalid_field_count_expected_2_or_3_got_{len(parts)}",
    )


@dataclass(frozen=True)
class SessionBundle:
    """Result of the pure-HTTP login step (Requirement 1.3) — cached in
    `AccountSessionCache` (Requirement 10) for reuse.

    Attributes:
        email: identifier of the logged-in account.
        access_token: Bearer token for subsequent requests.
        cookies: cookie-jar snapshot after login.
    """

    email: str
    access_token: str
    cookies: dict[str, str]


__all__ = [
    "ChatgptAccount",
    "SessionBundle",
    "parse_account_line",
]
