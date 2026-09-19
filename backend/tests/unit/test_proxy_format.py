"""Unit test cho `core/proxy_format.py::materialize_template`.

Chỉ verify vài case cụ thể (có placeholder, không có placeholder, nhiều
occurrence `{SID}`). Property test tổng quát (Property 31) thuộc task 6.2
riêng ở `tests/property/`.
"""

from __future__ import annotations

from app.core.proxy_format import materialize_template, safe_proxy_id


def test_safe_proxy_id_stable_and_non_reversible() -> None:
    """AC-7 helper: same input → same mask; mask does not contain raw id."""
    raw = "brightdata-session-abc-xyz-super-secret-line-99"
    a = safe_proxy_id(raw)
    b = safe_proxy_id(raw)
    assert a == b
    assert a != raw
    assert raw not in a
    assert isinstance(a, str) and len(a) >= 8
    # Different ids must not collide on the short prefix we care about.
    other = safe_proxy_id("completely-different-proxy-id")
    assert other != a


def test_safe_proxy_id_empty() -> None:
    assert safe_proxy_id("") != ""
    assert safe_proxy_id("") == safe_proxy_id("")


def test_materialize_template_with_single_placeholder() -> None:
    """Template chứa 1 occurrence `{SID}` → thay đúng vị trí, giữ nguyên phần còn lại."""
    result = materialize_template("http://user:pass@proxy.example.com:8080?sid={SID}", "abc123")

    assert result == "http://user:pass@proxy.example.com:8080?sid=abc123"
    assert "{SID}" not in result


def test_materialize_template_without_placeholder() -> None:
    """Proxy raw line không có placeholder → trả về nguyên vẹn, không đổi."""
    raw_line = "http://user:pass@proxy.example.com:8080"

    result = materialize_template(raw_line, "abc123")

    assert result == raw_line


def test_materialize_template_with_multiple_placeholders() -> None:
    """Nhiều occurrence `{SID}` trong cùng template → thay THẾ TOÀN BỘ."""
    template = "sid1={SID};sid2={SID};tag={SID}-end"

    result = materialize_template(template, "xyz")

    assert result == "sid1=xyz;sid2=xyz;tag=xyz-end"
    assert "{SID}" not in result
