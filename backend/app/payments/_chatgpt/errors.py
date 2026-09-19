"""Login error type for the shared ChatGPT module (`payments/_chatgpt/`).

`ChatgptLoginError` is the base raised by the login/session surface. It is a
plain `Exception` on purpose: `_chatgpt/` must import NOTHING from
`payments/ideal/` (anti-circular-import invariant), so it cannot inherit the
iDEAL-specific `IdealFlowError`. To keep the flow contract unchanged it carries
the same public attributes the flow boundary reads — `error_code="login_failed"`
and `step="login"` — so `IdealFlowHandler.run()` maps a login failure to
`JobResult(status=ERROR, error_code="login_failed")` exactly as before.

`LoginError` is a name alias so the moved login modules can keep their existing
`raise LoginError(...)` call sites verbatim. Note this is a DIFFERENT class from
`app.payments.ideal.errors.LoginError` (which remains an `IdealFlowError`
subclass for the iDEAL error hierarchy / its tests); both expose the same
`reason`/`error_code`/`step` surface, and both are caught at the flow boundary.
"""

from __future__ import annotations


class ChatgptLoginError(Exception):
    """ChatGPT pure-HTTP login failed (Requirement 1.4).

    Attributes:
        reason: one of ``invalid_credential``, ``mfa_required``,
            ``account_locked``, ``network_error``.
        error_code: stable code exposed via API/log — always ``login_failed``.
        step: flow step for realtime logging — always ``login``.
    """

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason: str = reason
        self.error_code: str = "login_failed"
        self.step: str = "login"
        super().__init__(message or f"Login failed: reason={reason}")


#: Alias kept so the moved login modules keep `raise LoginError(...)` verbatim.
LoginError = ChatgptLoginError


__all__ = ["ChatgptLoginError", "LoginError"]
