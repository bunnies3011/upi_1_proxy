"""Property test cho `core/proxy_format.py::materialize_template` (Property 31).

**Property 31: Materialize template {SID} không làm đổi phần còn lại của chuỗi**
Dùng `hypothesis`, sinh prefix/suffix/sid ngẫu nhiên, assert không còn literal
`{SID}` và phần còn lại giữ nguyên.

**Validates: Requirements 9.7**
"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from app.core.proxy_format import materialize_template

_SID_PLACEHOLDER = "{SID}"

# sid không được chứa literal "{SID}" để tránh tự nhiễu kết quả assert
# (nếu sid == "{SID}", output vẫn chứa "{SID}" một cách hợp lệ nhưng không
# còn là "placeholder gốc" — filter bỏ case này khỏi phạm vi property).
_sid_strategy = st.text(min_size=1).filter(lambda s: _SID_PLACEHOLDER not in s)
_text_strategy = st.text()  # có thể rỗng, unicode, ký tự đặc biệt


@given(prefix=_text_strategy, suffix=_text_strategy, sid=_sid_strategy)
@settings(max_examples=20)
def test_single_placeholder_preserves_surrounding_text(
    prefix: str, suffix: str, sid: str
) -> None:
    """Template với đúng 1 occurrence `{SID}` → chỉ phần placeholder bị thay,
    phần còn lại (prefix/suffix) giữ nguyên tuyệt đối."""
    template = prefix + _SID_PLACEHOLDER + suffix

    result = materialize_template(template, sid)

    assert _SID_PLACEHOLDER not in result
    assert result == prefix + sid + suffix


@given(
    prefix=_text_strategy,
    middle=_text_strategy,
    suffix=_text_strategy,
    sid=_sid_strategy,
)
@settings(max_examples=20)
def test_multiple_placeholders_all_replaced_and_rest_preserved(
    prefix: str, middle: str, suffix: str, sid: str
) -> None:
    """Template có NHIỀU occurrence `{SID}` → cả 2 occurrence đều được thay
    bằng `sid`, các phần còn lại (prefix/middle/suffix) giữ nguyên."""
    template = prefix + _SID_PLACEHOLDER + middle + _SID_PLACEHOLDER + suffix

    result = materialize_template(template, sid)

    assert _SID_PLACEHOLDER not in result
    assert result == prefix + sid + middle + sid + suffix


@given(raw=st.text().filter(lambda s: _SID_PLACEHOLDER not in s), sid=_sid_strategy)
@settings(max_examples=20)
def test_no_placeholder_returns_input_unchanged(raw: str, sid: str) -> None:
    """Template KHÔNG chứa `{SID}` (proxy raw line không có placeholder) →
    trả về nguyên vẹn input, không đổi."""
    result = materialize_template(raw, sid)

    assert result == raw
