"""Property test cho `payments/ideal/qr_renderer.py` — QR PNG round-trip
encode/decode bảo toàn nội dung deeplink (Property 20).

**Property 20: QR PNG round-trip encode/decode bảo toàn nội dung deeplink**

Với mọi chuỗi `deeplink` ASCII printable độ dài biến thiên (1..200 ký tự),
`QrRenderer.render_png(deeplink, job_id, output_dir)` PHẢI:

1. Ghi ra 1 file PNG hợp lệ tại `output_dir / f"{job_id}.png"` — file tồn
   tại, size lớn hơn ngưỡng sanity, và bắt đầu bằng PNG magic bytes chuẩn
   `\\x89PNG\\r\\n\\x1a\\n` (ISO/IEC 15948) — chứng minh Requirement 7.2
   (ảnh QR PNG thuần, không phải file placeholder/format khác).
2. NẾU env có thư viện decode QR (`pyzbar`) cài sẵn: decode file PNG vừa
   ghi và assert nội dung decoded == `deeplink` gốc — chứng minh round-trip
   encode/decode bảo toàn nội dung deeplink tuyệt đối (R7.1: `deeplink`
   được dùng làm nội dung QR chứ không phải chuỗi khác).
3. NẾU không có decoder QR trong env (macOS chưa `brew install zbar` +
   `pip install pyzbar`, CI chưa cài): chấp nhận SEMI-PROPERTY — chỉ verify
   file PNG hợp lệ ở bước 1. Việc decode nghiêm ngặt sẽ tự động reactivate
   ngay khi env cài `pyzbar`, KHÔNG cần sửa test.

Ngoài property chính, có 1 test riêng cho nhánh Fail_Fast_Policy của R7.6:
deeplink độ dài 5000 ký tự vượt sức chứa QR (version 40, ERROR_CORRECT_L
~2953 byte binary) → `render_png` LUÔN raise `QrRenderError` với
`error_code="qr_render_failed"`, không silently ghi file corrupt.

**Validates: Requirements 7.1, 7.2, 7.6**
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import Callable, Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.payments.ideal.errors import QrRenderError
from app.payments.ideal.qr_renderer import QrRenderer

# ---------------------------------------------------------------------------
# Optional QR decoder — cố gắng import `pyzbar` để round-trip NGHIÊM NGẶT.
# Nếu env không có (pyzbar chưa cài, hoặc lib native `libzbar` thiếu ở
# macOS/Linux), rơi về SEMI-PROPERTY (chỉ verify PNG signature ở bước 1).
# ---------------------------------------------------------------------------

# Kiểu hàm decode: nhận `Path` tới file PNG, trả về chuỗi decoded hoặc
# `None` nếu decoder không tìm thấy QR code trong ảnh (edge case bất khả
# thi với ảnh do `qrcode` sinh, nhưng vẫn giữ type an toàn).
_QrDecoder = Callable[[Path], "str | None"]

_DECODER: "_QrDecoder | None"

try:
    # pyzbar cần `zbar` native (macOS: `brew install zbar`, Debian: `apt
    # install libzbar0`). Nếu thiếu native, import sẽ raise ImportError kèm
    # message riêng — try/except broad để cover CẢ 2 case: package chưa cài
    # VÀ package cài rồi nhưng native lib thiếu.
    from pyzbar.pyzbar import decode as _pyzbar_decode  # type: ignore[import-not-found]
    from PIL import Image

    def _decode_via_pyzbar(png_path: Path) -> "str | None":
        with Image.open(png_path) as img:
            results = _pyzbar_decode(img)
        if not results:
            return None
        raw = results[0].data
        # `pyzbar` trả `bytes` — QR nội dung là UTF-8 theo spec khi mode byte.
        return raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)

    _DECODER = _decode_via_pyzbar
except Exception:  # pragma: no cover — fallback semi-property khi env thiếu decoder
    _DECODER = None


#: PNG magic bytes — 8 byte đầu file PNG hợp lệ theo ISO/IEC 15948
#: (mọi file PNG hợp lệ đều bắt đầu bằng chính xác 8 byte này).
_PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"

#: Ngưỡng size sanity — 1 QR PNG nhỏ nhất (version 1, box_size=10, border=4)
#: đã > 100 byte rất nhiều; dùng chặn file rỗng/truncated giả dạng PNG.
_MIN_PNG_SIZE_BYTES: Final[int] = 100

#: Độ dài deeplink vượt sức chứa QR version 40 ở ERROR_CORRECT_L (~2953 byte
#: binary). 5000 ký tự byte-encoded chắc chắn overflow ở mọi cấu hình QR mà
#: `QrRenderer` dùng — ép DataOverflowError → `QrRenderError`.
_OVERSIZED_DEEPLINK_LEN: Final[int] = 5000


# ---------------------------------------------------------------------------
# Property 20 — round-trip nội dung deeplink qua QR PNG.
# ---------------------------------------------------------------------------


@settings(
    max_examples=25,
    deadline=None,
    # tempfile.TemporaryDirectory() được tạo mới trong body mỗi example nên
    # không dùng function-scoped fixture; suppress phòng khi hypothesis chạy
    # kèm profile khác kích hoạt health check này.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    deeplink=st.text(
        # ASCII printable (space..tilde) — bao trọn ký tự URL RFC 3986 hợp
        # lệ (deeplink iDEAL thực tế), tránh unicode/emoji shrink chậm và
        # không phản ánh input production. Codepoint 32..126.
        alphabet=st.characters(min_codepoint=32, max_codepoint=126),
        min_size=1,
        max_size=200,
    ),
)
def test_qr_render_writes_valid_png_and_round_trips_deeplink(deeplink: str) -> None:
    """R7.1, R7.2: `render_png` LUÔN ghi ra 1 file PNG HỢP LỆ (magic bytes
    đúng chuẩn + size > 100 byte) cho mọi deeplink ASCII printable độ dài
    1..200 ký tự; nội dung QR round-trip qua decode trả về ĐÚNG chuỗi gốc
    (nếu env có decoder).
    """
    renderer = QrRenderer()

    with tempfile.TemporaryDirectory() as tmp_dir_str:
        output_dir = Path(tmp_dir_str)
        # `job_id` unique per example — khớp Requirement 7.3 (tên file dựa
        # `job_id`, unique cross-job) và tránh lẫn file giữa các lần thử.
        job_id = f"job-{uuid.uuid4().hex}"

        output_path = renderer.render_png(deeplink, job_id, output_dir)

        # 1) Đường dẫn khớp docstring `render_png` (Requirement 7.3).
        assert output_path == output_dir / f"{job_id}.png"
        assert output_path.exists() and output_path.is_file()

        # 2) File có PNG signature đúng chuẩn + size vượt ngưỡng sanity.
        raw = output_path.read_bytes()
        assert raw.startswith(_PNG_SIGNATURE), (
            "File PNG do QrRenderer sinh ra phải bắt đầu bằng PNG magic "
            f"bytes chuẩn, thấy: {raw[:8]!r}"
        )
        assert len(raw) > _MIN_PNG_SIZE_BYTES, (
            f"File PNG size = {len(raw)} byte, quá nhỏ để là QR hợp lệ "
            "(có thể là file rỗng/truncated)."
        )

        # 3) Round-trip nghiêm ngặt CHỈ khi env có `pyzbar` — decoded phải
        #    trùng chuỗi gốc TUYỆT ĐỐI. Fallback semi-property (skip decode)
        #    khi env thiếu decoder — vẫn đủ chứng minh R7.2 qua bước 1-2.
        if _DECODER is not None:
            decoded = _DECODER(output_path)
            assert decoded == deeplink, (
                f"QR round-trip mất nội dung: encoded={deeplink!r}, "
                f"decoded={decoded!r}"
            )


# ---------------------------------------------------------------------------
# R7.6 — deeplink vượt sức chứa QR (v40 ECC L) LUÔN raise QrRenderError.
# ---------------------------------------------------------------------------


def test_qr_render_raises_qr_render_error_on_oversized_deeplink() -> None:
    """R7.6 (Fail_Fast_Policy): deeplink dài 5000 ký tự vượt sức chứa QR
    version 40 (~2953 byte binary với ERROR_CORRECT_L mà `QrRenderer` dùng)
    → `render_png` PHẢI raise `QrRenderError` mang `error_code="qr_render_
    failed"` (khớp mapping của `IdealFlowHandler` sang `JobResult`); TUYỆT
    ĐỐI KHÔNG silently ghi file corrupt hoặc fallback ảnh placeholder.
    """
    renderer = QrRenderer()
    # 5000 ký tự 'a' — lowercase nên `qrcode` chọn byte mode (không phải
    # alphanumeric), giới hạn ~2953 byte ở v40 ECC L → chắc chắn overflow.
    oversized_deeplink = "a" * _OVERSIZED_DEEPLINK_LEN

    with tempfile.TemporaryDirectory() as tmp_dir_str:
        output_dir = Path(tmp_dir_str)

        with pytest.raises(QrRenderError) as exc_info:
            renderer.render_png(
                oversized_deeplink,
                job_id="oversized-job",
                output_dir=output_dir,
            )

        # `error_code` cố định = "qr_render_failed" — hợp đồng ổn định lộ ra
        # API/log qua `JobResult`, không được đổi khi encode fail.
        assert exc_info.value.error_code == "qr_render_failed"
        # `step` cố định = "qr_render" (dùng cho log realtime định danh bước
        # flow xảy ra lỗi — R14.2).
        assert exc_info.value.step == "qr_render"
