"""Property test cho `parse_account_line` — nhận diện đúng 2 định dạng
(Property 1, task 15.2).

**Property 1: Parse account line nhận diện đúng 2 định dạng**

Dùng `hypothesis` sinh dòng hợp lệ theo cả 2 định dạng được R1.1 chấp nhận:

    1. `email|password|totp_secret` — `totp_secret` có thể rỗng (account
       không bật MFA); parser SHALL trả `IdealParsedAccount` với
       `email`/`password` = giá trị nhập, `totp_secret` = nhập nếu non-empty
       ngược lại `None`, `access_token = None`.
    2. `email|access_token` — parser SHALL trả `IdealParsedAccount` với
       `email`/`access_token` = giá trị nhập, `password = totp_secret = None`.

Bổ sung 1 property phủ định (invalid format): dòng thô có 0 hoặc >=3 ký
tự phân tách `|` (tức không thuộc cả 2 định dạng hợp lệ) → parser SHALL
trả `AccountLineError` (KHÔNG raise) để `Job_Manager` tổng hợp danh sách
skip theo Requirement 8.2.

**Validates: Requirements 1.1**

Chỉ import trực tiếp `parse_account_line`/`IdealParsedAccount` từ
`app.payments.ideal.models` (source of truth) và `AccountLineError` từ
`app.core.payment_flow` (base type dùng chung cho mọi payment method) —
không đi qua re-export `chatgpt_client` để tránh phụ thuộc chuyển tiếp
không cần thiết trong property test.
"""

from __future__ import annotations

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app.core.payment_flow import AccountLineError
from app.payments.ideal.models import IdealParsedAccount, parse_account_line

# ---------------------------------------------------------------------------
# Shared strategies
#
# - Email: `st.emails()` sinh email hợp lệ RFC. Loại thêm bất kỳ giá trị nào
#   chứa `|` (ký tự phân tách của định dạng account line) hoặc whitespace ở
#   biên (parser `.strip()` từng phần — nếu strip biến email thành chuỗi
#   khác, assertion `result.email == email` sẽ sai lệch với input gốc).
# - Password/token/totp: text non-empty, không chứa `|`, không toàn
#   whitespace, không có whitespace ở biên (cùng lý do strip như trên).
# ---------------------------------------------------------------------------


def _clean_segment(value: str) -> bool:
    """Segment hợp lệ: không rỗng sau strip, không chứa `|`, không thay
    đổi bởi strip (để so sánh input == output không lệch)."""
    return bool(value) and "|" not in value and value == value.strip()


_email_strategy = st.emails().filter(_clean_segment)
_password_strategy = st.text(min_size=1, max_size=50).filter(_clean_segment)
_token_strategy = st.text(min_size=1, max_size=50).filter(_clean_segment)

# TOTP base32: rỗng (account không bật MFA) HOẶC alphabet base32 chuẩn.
_TOTP_BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
_totp_strategy = st.one_of(
    st.just(""),
    st.text(min_size=1, max_size=32, alphabet=_TOTP_BASE32_ALPHABET),
)


# ---------------------------------------------------------------------------
# Test 1 — 2-part format: `email|access_token`
# ---------------------------------------------------------------------------


@given(email=_email_strategy, token=_token_strategy)
@settings(max_examples=50)
def test_parse_account_line_accepts_2_part_email_token(
    email: str, token: str
) -> None:
    """R1.1 định dạng 2 phần (`email|access_token`) — parser trả
    `IdealParsedAccount` với `email`/`access_token` = input, `password` và
    `totp_secret` = `None`."""
    raw = f"{email}|{token}"

    result = parse_account_line(raw)

    assert isinstance(result, IdealParsedAccount), (
        f"expected IdealParsedAccount, got {type(result).__name__}: {result!r}"
    )
    assert result.raw_line == raw
    assert result.email == email
    assert result.access_token == token
    assert result.password is None
    assert result.totp_secret is None


# ---------------------------------------------------------------------------
# Test 2 — 3-part format: `email|password|totp_secret` (totp có thể rỗng)
# ---------------------------------------------------------------------------


@given(
    email=_email_strategy,
    password=_password_strategy,
    totp=_totp_strategy,
)
@settings(max_examples=50)
def test_parse_account_line_accepts_3_part_email_password_totp(
    email: str, password: str, totp: str
) -> None:
    """R1.1 định dạng 3 phần (`email|password|totp_secret`) — parser trả
    `IdealParsedAccount` với `email`/`password` = input, `totp_secret` =
    input nếu non-empty (base32), ngược lại `None` (account không bật MFA);
    `access_token` = `None`."""
    raw = f"{email}|{password}|{totp}"

    result = parse_account_line(raw)

    assert isinstance(result, IdealParsedAccount), (
        f"expected IdealParsedAccount, got {type(result).__name__}: {result!r}"
    )
    assert result.raw_line == raw
    assert result.email == email
    assert result.password == password
    # totp rỗng → `None`; non-empty → giữ nguyên chuỗi input.
    assert result.totp_secret == (totp if totp else None)
    assert result.access_token is None


# ---------------------------------------------------------------------------
# Test 3 — Invalid format: không đúng cả 2 định dạng → `AccountLineError`
# ---------------------------------------------------------------------------


@given(raw=st.text(min_size=0, max_size=200))
@settings(max_examples=50)
def test_parse_account_line_rejects_invalid_pipe_count(raw: str) -> None:
    """R1.1: dòng thô không khớp cả 2 định dạng hợp lệ → parser trả
    `AccountLineError` (KHÔNG raise). Filter về 2 nhóm chắc chắn invalid:
    số ký tự phân tách `|` = 0 (thiếu separator, 1 part) hoặc >= 3 (>=4
    parts, dư separator). Cả 2 nhóm này parser luôn dẫn tới nhánh
    `invalid_field_count_*` hoặc `empty_line` — không thể trả
    `IdealParsedAccount`."""
    pipe_count = raw.count("|")
    assume(pipe_count == 0 or pipe_count >= 3)

    result = parse_account_line(raw)

    assert isinstance(result, AccountLineError), (
        f"expected AccountLineError, got {type(result).__name__}: {result!r}"
    )
    assert result.reason, "AccountLineError.reason phải non-empty để log định danh nguyên nhân"
