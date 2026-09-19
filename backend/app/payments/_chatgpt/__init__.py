"""Shared ChatGPT login + session-resolution module.

Extracted from `payments/ideal/` so both the iDEAL flow and the UPI flow can
reuse ChatGPT authentication without importing each other. This package imports
NOTHING from `payments/ideal/` — the dependency direction is strictly
`ideal → _chatgpt` (anti-circular-import invariant, gated by an import smoke
test).

Public surface:
    - `SessionBundle`, `ChatgptAccount`, `parse_account_line` (models)
    - `ChatgptLoginError` / `LoginError` (errors)
    - `ChatgptLoginClient` (login/revalidate/hydrate/reset_openai_cookies)
    - `resolve_session` (cache → login → direct-token free function)
"""

from __future__ import annotations

from app.payments._chatgpt.errors import ChatgptLoginError, LoginError
from app.payments._chatgpt.login_client import ChatgptLoginClient
from app.payments._chatgpt.models import (
    ChatgptAccount,
    SessionBundle,
    parse_account_line,
)
from app.payments._chatgpt.plan_status import check_plan_status
from app.payments._chatgpt.session import resolve_session

__all__ = [
    "ChatgptAccount",
    "ChatgptLoginClient",
    "ChatgptLoginError",
    "LoginError",
    "SessionBundle",
    "check_plan_status",
    "parse_account_line",
    "resolve_session",
]
