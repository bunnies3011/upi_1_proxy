"""Property test cho `StripeClient.follow_redirect` — **Property 13**.

**Property 13: Follow redirect chỉ chấp nhận đúng 302 + Location hợp lệ**

*Với mọi* cặp `(status, has_location)` giả lập ở response redirect từ
Stripe (`GET redirect_to_url`), `StripeClient.follow_redirect(url)`:

- **CHỈ** trả `(encoded_tx_url, sig)` khi:
    - `response.status_code == 302`
    - Có header `Location`
    - Location đúng format `https://pay.ideal.nl/transactions/{encoded_tx_url}?sig={sig}`
      (path bắt đầu bằng `/transactions/`, `encoded_tx_url` non-empty, query
      param `sig` non-empty).
  Cả `encoded_tx_url` và `sig` được truyền nguyên vẹn từ Location header
  ra output (R4.7, R4.9).

- Raise `RedirectValidationError` (R4.8, Fail_Fast_Policy) khi:
    - `status_code != 302`, HOẶC
    - `status_code == 302` nhưng thiếu header `Location`, HOẶC
    - `status_code == 302` có Location nhưng SAI format
      (path không bắt đầu bằng `/transactions/`).

  Đồng thời `exc.http_status`/`exc.has_location` phải phản ánh chính xác
  trạng thái quan sát được ở response — KHÔNG suy diễn thêm.

Sử dụng `httpx.MockTransport` bắt request `follow_redirect` tại wire-level
để phát response cứng theo scenario. `_FakeSettings` async `get(key)` trả
`None` cho mọi key → `_read_request_timeout_seconds` fallback default nội
bộ (MockTransport phản hồi ngay, timeout không bị chạm).

**Validates: Requirements 4.7, 4.8**
"""

from __future__ import annotations

import asyncio
import logging
import string
from typing import Any

import pytest

from tests.support.fake_http import FakeAsyncSession, FakeResponse
from hypothesis import given, settings
from hypothesis import strategies as st

from app.payments.ideal.errors import RedirectValidationError
from app.payments.ideal.stripe_client import StripeClient

# ---------------------------------------------------------------------------
# Fake Settings — duck-typed thay `SettingsRepository`.
#
# `follow_redirect()` chỉ đọc `ideal.stripe_request_timeout_seconds`. Trả
# `None` cho mọi key → `_read_request_timeout_seconds()` fallback về default
# nội bộ (30s) — MockTransport emit response ngay, timeout không bị chạm.
# ---------------------------------------------------------------------------


class _FakeSettings:
    """Trả `None` cho mọi key — buộc `_read_*` dùng default nội bộ."""

    async def get(self, key: str) -> Any | None:  # noqa: ARG002 — key unused
        return None


# ---------------------------------------------------------------------------
# Alphabet constraints cho hypothesis — chọn tập ký tự KHÔNG cần
# percent-encode để URL và `parse_qs` decode nguyên vẹn giá trị. Nếu chèn
# `?`/`&`/`#`/space/... vào tx hay sig, urllib sẽ diễn giải khác → property
# về "truyền nguyên vẹn" khó biểu đạt. Ràng buộc alphabet chính là smart
# generator constrain-to-input-space.
# ---------------------------------------------------------------------------

#: `encoded_tx_url` chấp nhận alphanumeric — an toàn cho path segment,
#: không đụng ký tự đặc biệt cần escape trong URL.
_TX_ALPHABET: str = string.ascii_letters + string.digits

#: `sig` chấp nhận alphanumeric + `-`/`_` — ký tự URL-safe theo RFC3986
#: unreserved, `parse_qs` decode nguyên vẹn (không thay đổi giá trị).
_SIG_ALPHABET: str = "abc123-_"


# ---------------------------------------------------------------------------
# Handler factory — mỗi scenario dùng closure phát 1 response duy nhất vì
# `follow_redirect` KHÔNG retry (R4.7 — pure-HTTP single-shot).
# ---------------------------------------------------------------------------


def _make_valid_302_handler(tx: str, sig: str):
    """Handler phát response 302 với Location đúng format iDEAL.

    Location dạng `https://pay.ideal.nl/transactions/{tx}?sig={sig}`.
    """

    def _handler(call):  # noqa: ANN001
        location = f"https://pay.ideal.nl/transactions/{tx}?sig={sig}"
        # New semantics: allow_redirects=True → response.url = final URL.
        return FakeResponse(200, headers={"location": location}, url=location)

    return _handler


def _make_status_with_location_handler(status: int):
    """Handler phát response với `status` bất kỳ + Location hợp lệ.

    Dùng để kiểm chứng: chỉ cần status khác 302, `follow_redirect` phải
    Fail_Fast bất kể Location có hợp lệ hay không (R4.8).
    """

    def _handler(call):  # noqa: ANN001
        return FakeResponse(
            status,
            headers={"location": "https://pay.ideal.nl/transactions/abc?sig=xyz"},
        )

    return _handler


def _make_302_no_location_handler():
    """Handler phát response 302 nhưng KHÔNG có header `Location` (R4.8)."""

    def _handler(call):  # noqa: ANN001
        return FakeResponse(302)

    return _handler


def _make_302_bad_location_handler():
    """Handler phát response 302 với Location SAI format (không `/transactions/`)."""

    def _handler(call):  # noqa: ANN001
        # New semantics: return 200 with final url != /transactions/ path.
        return FakeResponse(200, url="https://x.example/foo/bar")

    return _handler


# ---------------------------------------------------------------------------
# Helper — chạy `follow_redirect` với 1 handler và trả `(tx, sig)` hoặc
# propagate exception. Async client tạo với `follow_redirects=False` (mặc
# định của httpx) để chắc chắn không auto-follow — chưa kể `follow_redirect`
# cũng override `follow_redirects=False` per-call.
# ---------------------------------------------------------------------------


def _run_follow_redirect(handler) -> tuple[str, str]:
    """Bọc `asyncio.run` cho gọn — trả `(encoded_tx_url, sig)` nếu thành công.

    Không nuốt exception: caller dùng `pytest.raises` ở test raise-path.
    """

    async def _inner() -> tuple[str, str]:
        fake_session = FakeAsyncSession(default_handler=handler)
        # follow_redirect tạo inline `create_async_client(...)` — monkey-patch
        # nó để trả cùng session (test-local, no global side-effect).
        import app.core.http_client as _hc
        orig = _hc.create_async_client
        _hc.create_async_client = lambda **kw: fake_session  # type: ignore[assignment]
        try:
            client = StripeClient(
                http_client=fake_session,
                settings=_FakeSettings(),  # duck-typed thay SettingsRepository
                logger=logging.getLogger(
                    "test.stripe_client.follow_redirect_property"
                ),
            )
            return await client.follow_redirect("https://x.example/y")
        finally:
            _hc.create_async_client = orig  # type: ignore[assignment]

    return asyncio.run(_inner())


# ---------------------------------------------------------------------------
# Test 1 — happy path: 302 + Location đúng format → return (tx, sig) nguyên vẹn.
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(
    tx=st.text(alphabet=_TX_ALPHABET, min_size=1, max_size=30),
    sig=st.text(alphabet=_SIG_ALPHABET, min_size=1, max_size=50),
)
def test_valid_302_with_location_returns_tx_and_sig(tx: str, sig: str) -> None:
    """*Với mọi* `(tx, sig)` non-empty (alphabet URL-safe), response 302
    có Location `https://pay.ideal.nl/transactions/{tx}?sig={sig}` khiến
    `follow_redirect` trả `(encoded_tx_url, sig_out)` với:

        - `encoded_tx_url == tx` (path segment sau `/transactions/` giữ nguyên).
        - `sig_out == sig` (query param `sig` decode nguyên vẹn).

    Property này bám R4.7 (path prefix + query param bắt buộc) và R4.9
    (truyền `sig` nguyên vẹn, không parse/biến đổi nội dung).

    **Validates: Requirements 4.7**
    """
    handler = _make_valid_302_handler(tx, sig)
    encoded_tx_url, sig_out = _run_follow_redirect(handler)

    assert encoded_tx_url == tx, (
        f"encoded_tx_url kỳ vọng = {tx!r} nhưng nhận {encoded_tx_url!r} — "
        "`follow_redirect` không truyền nguyên vẹn segment `/transactions/{tx}` "
        "từ Location header (R4.7)."
    )
    assert sig_out == sig, (
        f"sig kỳ vọng = {sig!r} nhưng nhận {sig_out!r} — "
        "`follow_redirect` không truyền nguyên vẹn query param `sig` từ "
        "Location header (R4.7, R4.9)."
    )


# ---------------------------------------------------------------------------
# Test 2 — property: mọi status ≠ 302 (có Location hợp lệ) đều Fail_Fast.
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(
    # Sau khi migrate sang `allow_redirects=True`, status 2xx đi qua parse
    # `response.url` (không phải header Location) — nhánh raise
    # RedirectValidationError với http_status khác. Test này chỉ cover nhánh
    # non-2xx (Fail_Fast_Policy R4.8).
    status=st.integers(min_value=300, max_value=599).filter(lambda s: s != 302),
)
def test_non_302_status_raises(status: int) -> None:
    """*Với mọi* HTTP status ∈ [200, 599] khác 302, `follow_redirect` LUÔN
    raise `RedirectValidationError` với `exc.http_status == status`, kể cả
    khi response có Location hợp lệ.

    Location hợp lệ được đặt cố tình để đảm bảo hành vi Fail_Fast dựa
    trên **duy nhất** status code (R4.8) — không cứu vãn bằng cách xét
    Location dù có format đẹp.

    **Validates: Requirements 4.8**
    """
    handler = _make_status_with_location_handler(status)

    with pytest.raises(RedirectValidationError) as exc_info:
        _run_follow_redirect(handler)

    assert exc_info.value.http_status == status, (
        f"RedirectValidationError.http_status kỳ vọng = {status} nhưng nhận "
        f"{exc_info.value.http_status!r} — `follow_redirect` không propagate "
        "đúng HTTP status của response (R4.8)."
    )


# ---------------------------------------------------------------------------
# Test 3 — status 302 nhưng thiếu Location → Fail_Fast + has_location=False.
# ---------------------------------------------------------------------------


def test_302_missing_location_raises() -> None:
    """Response 302 KHÔNG có header `Location` → `follow_redirect` raise
    `RedirectValidationError` với `exc.has_location is False` và
    `exc.http_status == 302`.

    Đây là nhánh Fail_Fast_Policy đặc trưng của R4.8: có 302 nhưng thiếu
    Location là bất thường của Stripe — không được suy diễn thay thế.

    **Validates: Requirements 4.8**
    """
    handler = _make_302_no_location_handler()

    with pytest.raises(RedirectValidationError) as exc_info:
        _run_follow_redirect(handler)

    assert exc_info.value.http_status == 302, (
        "RedirectValidationError.http_status kỳ vọng = 302 nhưng nhận "
        f"{exc_info.value.http_status!r} — status code không được propagate "
        "đúng khi thiếu Location (R4.8)."
    )
    assert exc_info.value.has_location is False, (
        "RedirectValidationError.has_location kỳ vọng = False khi response "
        f"KHÔNG có header Location, nhưng nhận {exc_info.value.has_location!r} "
        "(R4.8)."
    )


# ---------------------------------------------------------------------------
# Test 4 — status 302 + Location SAI format → Fail_Fast (path không /transactions/).
# ---------------------------------------------------------------------------


def test_302_invalid_location_format_raises() -> None:
    """Response 302 có Location nhưng path KHÔNG bắt đầu bằng
    `/transactions/` (ví dụ `/foo/bar`) → `follow_redirect` raise
    `RedirectValidationError`.

    Property này bám R4.7: Location phải khớp shape
    `/transactions/{encoded_tx_url}?sig={sig}` — bất kỳ path khác đều là
    dấu hiệu Stripe đổi luồng và không thể suy diễn `encoded_tx_url`/`sig`.

    **Validates: Requirements 4.7, 4.8**
    """
    handler = _make_302_bad_location_handler()

    with pytest.raises(RedirectValidationError):
        _run_follow_redirect(handler)


if __name__ == "__main__":  # pragma: no cover
    # Cho phép chạy file trực tiếp để smoke test cục bộ — production dùng
    # `pytest` từ backend/.
    pytest.main([__file__, "-v"])
