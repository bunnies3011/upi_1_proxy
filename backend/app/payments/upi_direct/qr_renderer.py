"""Render UPI URI / validated PNG bytes to job artifact."""

from __future__ import annotations

import io
import logging
from pathlib import Path

from app.payments.upi_direct.errors import QrRenderError
from app.payments.upi_direct.network_safety import atomic_write_png, validate_and_reencode_png

logger = logging.getLogger(__name__)


def _optimize_qr_png(png_bytes: bytes) -> bytes:
    """Enhance QR PNG: decode & re-render at high-res, or crop white border & upscale NEAREST."""
    from PIL import Image

    # 1. Try decoding payload using cv2 QRCodeDetector to re-render fresh vector QR
    try:
        import cv2
        import numpy as np

        np_arr = np.frombuffer(png_bytes, dtype=np.uint8)
        cv_img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        if cv_img is not None:
            detector = cv2.QRCodeDetector()
            decoded_text, _, _ = detector.detectAndDecode(cv_img)
            if decoded_text and decoded_text.strip():
                import qrcode
                from qrcode.constants import ERROR_CORRECT_M

                qr = qrcode.QRCode(
                    error_correction=ERROR_CORRECT_M, box_size=15, border=2
                )
                qr.add_data(decoded_text.strip())
                qr.make(fit=True)
                crisp_img = qr.make_image(fill_color="black", back_color="white")
                buf = io.BytesIO()
                crisp_img.save(buf, format="PNG", optimize=True)
                return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.debug("cv2 qr decode/re-render fallback: %s", exc)

    # 2. Fallback: crop excessive white padding and upscale with NEAREST (crystal clear, no blur)
    try:
        import numpy as np

        with Image.open(io.BytesIO(png_bytes)) as img:
            gray = img.convert("L")
            arr = np.array(gray)
            non_white = np.where(arr < 250)
            if len(non_white[0]) > 0:
                ymin, ymax = int(np.min(non_white[0])), int(np.max(non_white[0]))
                xmin, xmax = int(np.min(non_white[1])), int(np.max(non_white[1]))
                # Keep small 15px quiet zone so QR scanners detect boundary
                pad = 15
                crop_box = (
                    max(0, xmin - pad),
                    max(0, ymin - pad),
                    min(img.width, xmax + pad),
                    min(img.height, ymax + pad),
                )
                cropped = img.crop(crop_box)
                # NEAREST neighbor upscale preserves perfectly sharp black/white square edges
                upscaled = cropped.resize((1024, 1024), Image.Resampling.NEAREST)
                buf = io.BytesIO()
                upscaled.save(buf, format="PNG", optimize=True)
                return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.debug("crop & upscale fallback: %s", exc)

    return png_bytes


class QrRenderer:
    def publish_png_bytes(self, png_bytes: bytes, job_id: str, output_dir: Path) -> Path:
        try:
            reencoded = validate_and_reencode_png(png_bytes)
            optimized = _optimize_qr_png(reencoded)
        except Exception as exc:
            raise QrRenderError(detail=str(exc)) from exc
        path = output_dir / f"{job_id}.png"
        try:
            return atomic_write_png(path, optimized)
        except OSError as exc:
            raise QrRenderError(detail=f"io:{exc}") from exc

    def render_upi_uri(self, upi_uri: str, job_id: str, output_dir: Path) -> Path:
        try:
            import qrcode
            from qrcode.constants import ERROR_CORRECT_M
        except ImportError as exc:
            raise QrRenderError(detail="qrcode_missing") from exc
        try:
            # box_size=15 with border=2 produces a crisp ~1000px QR with tight borders
            qr = qrcode.QRCode(
                error_correction=ERROR_CORRECT_M, box_size=15, border=2
            )
            qr.add_data(upi_uri)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")

            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            path = output_dir / f"{job_id}.png"
            return atomic_write_png(path, buf.getvalue())
        except Exception as exc:
            raise QrRenderError(detail=f"encode:{exc.__class__.__name__}") from exc


__all__ = ["QrRenderer"]

