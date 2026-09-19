"""Property test cho `ChatgptClient.classify_login_error` (Property 2).

*Với mọi* response lỗi đăng nhập giả lập thuộc 1 trong 4 nhóm nguyên nhân
(`invalid_credential`, `mfa_required`, `account_locked`, `network_error`),
hàm phân loại luôn trả về đúng nhóm — không có sai/thiếu.

Priority (deterministic, theo docstring implementation):
    1. Transport-level exception (Timeout/Network/Transport) → `network_error`.
    2. `response=None` → `network_error`.
    3. Body chứa keyword MFA → `mfa_required`.
    4. Status 403 + keyword locked → `account_locked`.
    5. Status ∈ {401, 403} + keyword invalid_credential → `invalid_credential`.
    6. Fallback → `network_error`.

**Validates: Requirements 1.4**
"""
from __future__ import annotations

from curl_cffi.requests.exceptions import ConnectionError as _CffiConnectionError
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core import http_client as http
from app.payments.ideal.chatgpt_client import (
    ChatgptClient,
    LOGIN_ERROR_ACCOUNT_LOCKED,
    LOGIN_ERROR_INVALID_CREDENTIAL,
    LOGIN_ERROR_MFA_REQUIRED,
    LOGIN_ERROR_NETWORK,
)
from tests.support.fake_http import FakeResponse

_MFA_KEYWORDS: tuple[str, ...] = (
    "mfa_required", "totp_required", "two_factor_required",
)
_LOCKED_KEYWORDS: tuple[str, ...] = ("account_locked", "banned", "disabled")
_INVALID_CRED_KEYWORDS: tuple[str, ...] = (
    "invalid_credential", "wrong password", "password_incorrect",
)
_ALL_LOGIN_KEYWORDS_LOWER: tuple[str, ...] = (
    "mfa_required", "totp_required", "two_factor_required", "mfa", "otp_required",
    "account_locked", "account_disabled", "account_suspended", "user_disabled",
    "banned", "locked", "disabled",
    "invalid_credential", "invalidcredentials", "wrong password",
    "incorrect password", "password_incorrect", "email_or_password_incorrect",
)


def _has_no_login_keyword(body: str) -> bool:
    lowered = body.lower()
    return not any(kw in lowered for kw in _ALL_LOGIN_KEYWORDS_LOWER)


_PBT_SETTINGS = settings(deadline=None, max_examples=50)


@_PBT_SETTINGS
@given(
    exc_type=st.sampled_from(
        [http.TimeoutException, http.NetworkError, _CffiConnectionError]
    )
)
def test_transport_error_always_network(exc_type: type[Exception]) -> None:
    """*Với mọi* Timeout / NetworkError / ConnectionError → `LOGIN_ERROR_NETWORK`.

    **Validates: Requirements 1.4**
    """
    exc = exc_type("simulated transport failure")
    result = ChatgptClient.classify_login_error(response=None, exception=exc)
    assert result == LOGIN_ERROR_NETWORK


def test_no_response_no_exception_network() -> None:
    """`classify_login_error(None, None)` → `LOGIN_ERROR_NETWORK`.

    **Validates: Requirements 1.4**
    """
    assert ChatgptClient.classify_login_error(None, None) == LOGIN_ERROR_NETWORK


@_PBT_SETTINGS
@given(keyword=st.sampled_from(_MFA_KEYWORDS))
def test_mfa_keyword_in_body_returns_mfa_required(keyword: str) -> None:
    """MFA keyword body + status 401 → `MFA_REQUIRED`.

    **Validates: Requirements 1.4**
    """
    response = FakeResponse(status_code=401, text=f"error: {keyword}")
    result = ChatgptClient.classify_login_error(response=response, exception=None)
    assert result == LOGIN_ERROR_MFA_REQUIRED


@_PBT_SETTINGS
@given(keyword=st.sampled_from(_LOCKED_KEYWORDS))
def test_locked_keyword_403_returns_account_locked(keyword: str) -> None:
    """Locked keyword + status 403 → `ACCOUNT_LOCKED`.

    **Validates: Requirements 1.4**
    """
    response = FakeResponse(status_code=403, text=f"user is {keyword}")
    result = ChatgptClient.classify_login_error(response=response, exception=None)
    assert result == LOGIN_ERROR_ACCOUNT_LOCKED


@_PBT_SETTINGS
@given(keyword=st.sampled_from(_INVALID_CRED_KEYWORDS))
def test_invalid_credential_keyword_401_returns_invalid_credential(
    keyword: str,
) -> None:
    """Invalid_credential keyword + status 401 → `INVALID_CREDENTIAL`.

    **Validates: Requirements 1.4**
    """
    response = FakeResponse(status_code=401, text=f"login: {keyword}")
    result = ChatgptClient.classify_login_error(response=response, exception=None)
    assert result == LOGIN_ERROR_INVALID_CREDENTIAL


@_PBT_SETTINGS
@given(
    body=st.text(
        alphabet="0123456789_- ",
        min_size=0,
        max_size=80,
    ).filter(_has_no_login_keyword)
)
def test_unknown_response_returns_network(body: str) -> None:
    """Status 500 + body không match keyword → fallback `LOGIN_ERROR_NETWORK`.

    **Validates: Requirements 1.4**
    """
    response = FakeResponse(status_code=500, text=body)
    result = ChatgptClient.classify_login_error(response=response, exception=None)
    assert result == LOGIN_ERROR_NETWORK
