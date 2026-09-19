"""Unit test cơ bản cho `core/redaction.py` — verify vài case cụ thể.

Property test đầy đủ (Property 5, dùng hypothesis) thuộc task 4.2 —
KHÔNG viết ở đây.
"""

from __future__ import annotations

from app.core.redaction import redact_dict, redact_message

_MASK = "***REDACTED***"


def test_redact_dict_masks_top_level_sensitive_fields() -> None:
    payload = {
        "email": "user@example.com",
        "password": "s3cret",
        "access_token": "tok-abc",
    }

    result = redact_dict(payload)

    assert result["email"] == "user@example.com"
    assert result["password"] == _MASK
    assert result["access_token"] == _MASK


def test_redact_dict_does_not_mutate_input() -> None:
    payload = {"cookie": "raw-cookie-value"}

    result = redact_dict(payload)

    assert payload["cookie"] == "raw-cookie-value"
    assert result["cookie"] == _MASK


def test_redact_dict_matches_case_insensitive_and_namespaced_key() -> None:
    payload = {"Password": "s3cret", "web.auth_token": "raw-token"}

    result = redact_dict(payload)

    assert result["Password"] == _MASK
    assert result["web.auth_token"] == _MASK


def test_redact_dict_recurses_into_nested_dict_and_list() -> None:
    payload = {
        "account": {"email": "a@b.com", "totp_secret": "123456"},
        "sessions": [
            {"cookie": "c1"},
            {"cookie": "c2"},
        ],
    }

    result = redact_dict(payload)

    assert result["account"]["email"] == "a@b.com"
    assert result["account"]["totp_secret"] == _MASK
    assert result["sessions"][0]["cookie"] == _MASK
    assert result["sessions"][1]["cookie"] == _MASK


def test_redact_message_replaces_known_secret_at_any_position() -> None:
    message = "login failed for token=tok-abc123 after retry"

    result = redact_message(message, ["tok-abc123"])

    assert "tok-abc123" not in result
    assert _MASK in result


def test_redact_message_replaces_all_occurrences() -> None:
    message = "sig=xyz initiate with sig=xyz again"

    result = redact_message(message, ["xyz"])

    assert "xyz" not in result
    assert result.count(_MASK) == 2


def test_redact_message_skips_empty_secret_without_matching_whole_message() -> None:
    message = "some log line without secrets"

    result = redact_message(message, ["", "not-present-secret"])

    assert result == message
