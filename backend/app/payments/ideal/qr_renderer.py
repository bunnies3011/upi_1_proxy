"""QrRenderer — vẽ QR PNG thuần từ deeplink của IssuerBank đã chọn.

Bước cuối cùng của luồng iDEAL: nhận `deeplink` do `IssuerSelector` trả về
và vẽ ảnh QR PNG **thuần** (không overlay text/logo/watermark — Requirement
7.2) ghi vào thư mục runtime với tên file dựa trên `job_id` (unique cross-
job khi chạy song song — Requirement 7.3).

Payment_Module_Boundary: KHÔNG import từ `app.core.*`. Exception dùng
`QrRenderError` thuộc `app.payments.ideal.errors` (hierarchy exception độc
lập của `payments/ideal/`).

Fail_Fast_Policy: mọi lỗi encode QR (deeplink vượt sức chứa) hoặc I/O
(mkdir/ghi file PNG thất bại) đều raise `QrRenderError` — KHÔNG fallback
ghi ảnh rỗng/placeholder ngầm định (Requirement 7.6).

_Requirements: 7.1, 7.2, 7.3, 7.6_
"""

from __future__ import annotations

from pathlib import Path

import qrcode
from qrcode.constants import ERROR_CORRECT_L
from qrcode.exceptions import DataOverflowError

from app.payments.ideal.errors import QrRenderError


# Cấu hình QR chuẩn cho quét mobile:
# - ERROR_CORRECT_L (7% dung sai) tối ưu cho deeplink URL sạch hiển thị trên
#   màn hình — không cần dung sai cao vì không in ấn/dính bẩn; giữ cho
#   version QR thấp nhất → mật độ module nhẹ, mobile scan nhanh.
# - box_size=10 (px/module) đủ lớn để mobile app camera lấy nét ổn định.
# - border=4 (module) là quiet zone tối thiểu theo QR spec (ISO/IEC 18004).
_QR_ERROR_CORRECTION = ERROR_CORRECT_L
_QR_BOX_SIZE = 10
_QR_BORDER = 4

# Màu ảnh QR PNG — thuần đen/trắng, KHÔNG overlay bất kỳ layer nào lên trên
# (Requirement 7.2). Dùng string tên màu PIL để tương thích với backend PNG
# mặc định của `qrcode.make_image()`.
_QR_FILL_COLOR = "black"
_QR_BACK_COLOR = "white"


class QrRenderer:
    """Vẽ ảnh QR PNG thuần từ chuỗi deeplink iDEAL.

    Stateless — mọi input truyền qua tham số của `render_png`, không phụ
    thuộc Settings_Store hay state nội bộ.
    """

    def render_png(self, deeplink: str, job_id: str, output_dir: Path) -> Path:
        """Vẽ QR PNG từ `deeplink` và ghi vào `output_dir / f"{job_id}.png"`.

        Requirement 7.1 (lấy `deeplink` của IssuerBank làm nội dung QR),
        Requirement 7.2 (ảnh QR PNG thuần, không overlay), Requirement 7.3
        (tên file dựa `job_id` để unique cross-job), Requirement 7.6 (Fail_
        Fast_Policy khi lỗi encode/I/O).

        Args:
            deeplink: Chuỗi deeplink của IssuerBank được chọn. Trường hợp
                rỗng đã được chặn ở `IssuerSelector` (Requirement 6) —
                KHÔNG re-validate ở đây (R7.6 note).
            job_id: Định danh duy nhất của IdealJob (UUID toàn hệ thống),
                dùng làm tên file PNG để đảm bảo không đụng tên giữa các
                job chạy song song (Requirement 7.3).
            output_dir: Thư mục runtime để ghi file PNG (ngoài version
                control). Tự tạo nếu chưa tồn tại (`parents=True,
                exist_ok=True`).

        Returns:
            `Path` tới file PNG đã ghi thành công.

        Raises:
            QrRenderError: Khi thư viện `qrcode` không thể encode
                `deeplink` (vượt sức chứa QR — `DataOverflowError`), hoặc
                khi lỗi I/O trong `mkdir`/`save` (`OSError`). Boundary
                iDEAL (`IdealFlowHandler`) sẽ bắt và map sang `JobResult
                (status=error, error_code="qr_render_failed")`.
        """
        output_path = output_dir / f"{job_id}.png"

        # 1) Encode deeplink → matrix QR + build PIL image object.
        #    `fit=True` để `qrcode` tự chọn version thấp nhất đủ chứa
        #    dữ liệu; nếu deeplink vượt cả version 40 → DataOverflowError.
        try:
            qr = qrcode.QRCode(
                error_correction=_QR_ERROR_CORRECTION,
                box_size=_QR_BOX_SIZE,
                border=_QR_BORDER,
            )
            qr.add_data(deeplink)
            qr.make(fit=True)
            img = qr.make_image(
                fill_color=_QR_FILL_COLOR,
                back_color=_QR_BACK_COLOR,
            )
        except (DataOverflowError, ValueError) as exc:
            # `qrcode` raise `DataOverflowError` khi encoding thuần vượt sức
            # chứa, HOẶC `ValueError` từ `check_version` khi `fit=True` phải
            # nâng version vượt qua 40 (giới hạn cứng của QR spec) — cả 2
            # đều là "deeplink quá dài", wrap chung thành QrRenderError.
            raise QrRenderError(
                reason=f"deeplink exceeds QR encoding capacity: {exc}"
            ) from exc

        # 2) Ghi file PNG. Tự tạo `output_dir` nếu chưa tồn tại.
        #    `mkdir` có thể raise OSError (permission, disk full, path là
        #    file thay vì dir...); `save` có thể raise OSError (I/O fail).
        #    Cả 2 wrap thành QrRenderError cùng error_code "qr_render_failed".
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            img.save(output_path)
        except OSError as exc:
            raise QrRenderError(
                reason=f"I/O error writing QR PNG at '{output_path}': {exc}"
            ) from exc

        return output_path


__all__ = ["QrRenderer"]
