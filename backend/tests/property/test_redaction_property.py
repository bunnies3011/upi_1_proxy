"""Property test cho `core/redaction.py` — Property 5.

**Property 5: Sensitive_Data_Redaction bao trùm mọi trường nhạy cảm**

**Validates: Requirements 1.9, 3.6, 4.9, 14.8**

Import trực tiếp `_SENSITIVE_FIELD_NAMES`/`_REDACTED_MASK` từ module đang test
để tham số hoá property theo đúng mọi field nhạy cảm đã định nghĩa, và để
so sánh chính xác với giá trị mask mà `redact_dict`/`redact_message` dùng.
"""

from __future__ import annotations

import json

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app.core.redaction import (
    _REDACTED_MASK,
    _SENSITIVE_FIELD_NAMES,
    redact_dict,
    redact_message,
)

_SENSITIVE_FIELDS = sorted(_SENSITIVE_FIELD_NAMES)

# Giá trị chuỗi ngẫu nhiên bao gồm cả rỗng, unicode, ký tự đặc biệt.
_value_strategy = st.text(min_size=0, max_size=40)

# Giá trị "noise" dùng để chèn thêm vào dict/list xung quanh field nhạy cảm,
# để mô phỏng dict thực tế có nhiều key/item khác không liên quan.
_noise_scalar_strategy = st.one_of(
    st.text(min_size=0, max_size=20),
    st.integers(),
    st.booleans(),
    st.none(),
)


@st.composite
def _wrap_random(draw, inner):
    """Bọc `inner` vào 1 cấp dict hoặc list ngẫu nhiên, kèm noise xung quanh."""
    kind = draw(st.sampled_from(["dict", "list"]))
    if kind == "dict":
        noise = draw(
            st.dictionaries(
                st.text(min_size=1, max_size=10),
                _noise_scalar_strategy,
                max_size=3,
            )
        )
        wrapper_key = draw(st.text(min_size=1, max_size=10))
        result = dict(noise)
        result[wrapper_key] = inner
        return result

    extras = draw(st.lists(_noise_scalar_strategy, max_size=3))
    idx = draw(st.integers(min_value=0, max_value=len(extras)))
    result = list(extras)
    result.insert(idx, inner)
    return result


def _collect_noise_atoms(node: object, target_node: object, atoms: list[str]) -> None:
    """Thu thập mọi chuỗi (key và giá trị scalar đã serialize) trong `node`,
    NGOẠI TRỪ chính `target_node` (so khớp theo identity) — dùng để phát
    hiện va chạm ngẫu nhiên giữa giá trị noise và giá trị thô đang test.
    """
    if node is target_node:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str):
                atoms.append(key)
            _collect_noise_atoms(value, target_node, atoms)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _collect_noise_atoms(item, target_node, atoms)
    else:
        atoms.append(node if isinstance(node, str) else json.dumps(node))


@st.composite
def _payload_with_sensitive_field(draw):
    """Sinh 1 dict lồng nhau (top-level hoặc nested depth 1-3) chứa đúng 1
    field nhạy cảm với giá trị đã biết, để assert sau redact_dict() giá trị
    thô đó không còn xuất hiện ở bất kỳ đâu trong output.
    """
    field = draw(st.sampled_from(_SENSITIVE_FIELDS))
    value = draw(_value_strategy)
    depth = draw(st.integers(min_value=1, max_value=3))

    target_node = {field: value}
    node: object = target_node
    for _ in range(depth - 1):
        node = draw(_wrap_random(node))

    # redact_dict() nhận vào dict — nếu cấp bọc ngoài cùng ra list thì bọc
    # thêm 1 lớp dict để hợp lệ input, không ảnh hưởng tới ngữ nghĩa test
    # (field nhạy cảm vẫn nằm ở depth ngẫu nhiên 1-3 đã sinh phía trong).
    if not isinstance(node, dict):
        node = {"root": node}

    # Guard chống va chạm ngẫu nhiên: nếu giá trị thô (`value`) trùng/là
    # substring của bất kỳ key/giá trị noise nào KHÁC vị trí field nhạy cảm
    # đang test, discard example này — đây là artifact của việc sinh noise
    # ngẫu nhiên, không phải vi phạm property thật (redact_dict chỉ có thể
    # mask theo TÊN key, không thể phân biệt 2 giá trị giống nhau ở 2 vị trí
    # khác nhau trong cây).
    if value:
        noise_atoms: list[str] = []
        _collect_noise_atoms(node, target_node, noise_atoms)
        assume(not any(value in atom for atom in noise_atoms))

    return node, field, value


def _iter_dict_items(node: object):
    """Duyệt đệ quy toàn bộ (key, value) của mọi dict lồng trong `node`."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _iter_dict_items(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _iter_dict_items(item)


@given(payload_field_value=_payload_with_sensitive_field())
@settings(max_examples=20)
def test_redact_dict_removes_raw_sensitive_value_everywhere(payload_field_value):
    payload, field, raw_value = payload_field_value

    redacted = redact_dict(payload)

    # Bất biến cấu trúc: mọi key nhạy cảm (case-insensitive) trong toàn bộ
    # cây kết quả phải có giá trị bị mask, bất kể nằm ở depth nào.
    for key, value in _iter_dict_items(redacted):
        if isinstance(key, str) and key.lower() in _SENSITIVE_FIELD_NAMES:
            assert value == _REDACTED_MASK

    # Bất biến rò rỉ: giá trị thô đã sinh không còn xuất hiện ở bất kỳ đâu
    # trong output khi serialize ra JSON string.
    # - Bỏ qua khi giá trị thô rỗng: "" luôn là substring của mọi chuỗi nên
    #   phép kiểm tra "not in" sẽ vô nghĩa (luôn False) với input rỗng.
    # - Bỏ qua khi giá trị thô là substring của mask string: sau khi redact,
    #   giá trị bị thay bằng mask `***REDACTED***`; nếu giá trị thô ngẫu nhiên
    #   trùng/là substring của mask (ví dụ "A" nằm trong "REDACTED") thì nó sẽ
    #   "xuất hiện lại" trong output như một phần của mask — đây là va chạm
    #   ngẫu nhiên của việc sinh dữ liệu, KHÔNG phải rò rỉ thật (redact_dict
    #   mask theo TÊN key, không thể phân biệt giá trị trùng ký tự với mask).
    if raw_value and raw_value not in _REDACTED_MASK:
        serialized = json.dumps(redacted, ensure_ascii=False)
        assert raw_value not in serialized


@st.composite
def _message_with_secrets(draw):
    """Sinh message dài + tập secret non-empty, chèn secret vào vị trí bất
    kỳ (đầu/giữa/cuối) trong message.
    """
    secrets = draw(
        st.lists(
            st.text(min_size=1, max_size=30),
            min_size=1,
            max_size=5,
            unique=True,
        )
    )
    # Loại các secret ngẫu nhiên trùng/chứa mask string — trường hợp hiếm
    # nhưng sẽ làm phép assert "secret not in output" vô nghĩa (secret vẫn
    # xuất hiện lại vì nó chính là 1 phần của mask được chèn bởi việc redact
    # 1 secret khác), không phải lỗi rò rỉ dữ liệu thật.
    assume(not any(s in _REDACTED_MASK or _REDACTED_MASK in s for s in secrets))

    message = draw(st.text(min_size=0, max_size=200))
    for secret in secrets:
        idx = draw(st.integers(min_value=0, max_value=len(message)))
        message = message[:idx] + secret + message[idx:]

    return message, secrets


@given(message_secrets=_message_with_secrets())
@settings(max_examples=20)
def test_redact_message_removes_all_known_secrets(message_secrets):
    message, secrets = message_secrets

    redacted = redact_message(message, secrets)

    for secret in secrets:
        assert secret not in redacted
