"""Thin curl_cffi client cho Telegram Bot API.

Implement các method dùng cho notifier + polling job mode:
    - `send_photo(token, chat_id, photo_bytes, caption)`: upload ảnh QR
      + caption HTML qua endpoint `sendPhoto`.
    - `send_message(token, chat_id, text, reply_markup=None)`: gửi
      text-only (dùng cho nút "Test gửi" trong UI + fallback nếu chưa
      có QR), có thể kèm inline keyboard.
    - `send_message_with_reply_to(...)`: gửi text reply vào 1 message cụ
      thể (dùng khi bot trả lời trong topic/thread).
    - `get_updates(...)`: long-poll `getUpdates` cho `PollingSupervisor`.
    - `answer_callback_query(...)`, `edit_message_caption(...)`,
      `edit_message_reply_markup(...)`: xử lý callback từ inline button.

Client tách 2 session riêng biệt:
    - "sending" session (timeout ngắn, 15s): dùng cho mọi call
      request/response nhanh (`send_photo`, `send_message`, edit*,
      `answerCallbackQuery`).
    - "polling" session (timeout dài, 30s): dùng riêng cho `get_updates`
      vì Telegram long-poll giữ connection mở tới `timeout` giây (mặc
      định 25s) — cần HTTP client timeout lớn hơn giá trị long-poll để
      không bị client tự cắt kết nối trước khi Telegram trả response.

Fail_Fast tại boundary:
    - HTTP status != 200 hoặc body `{"ok": false}` → raise `TelegramApiError`
      với message chi tiết (bao gồm error_code + description từ Telegram).
    - Network error / timeout → propagate `http_client.HTTPError` (caller
      `TelegramNotifier` tự swallow qua boundary `_invoke_terminal_hooks`).

KHÔNG tự retry — Telegram Bot API rate limit rất khắt khe (429 với
retry_after), và với round-robin distribution, retry cùng chat có thể
làm lệch cursor. Retry policy do notifier layer quyết định (hiện tại:
không retry, chấp nhận mất 1 notify khi 1 chat lỗi — job core đã success
và có audit qua SSE).

Multipart upload (`send_photo`): curl_cffi 0.15 KHÔNG hỗ trợ `files=`
kwarg của httpx style — phải dùng `CurlMime` với `multipart=` kwarg.
Semantic bytes-in-memory upload giống nhau (Telegram nhận multipart/form-data
với 1 part photo binary + N part text field).
"""

from __future__ import annotations

from io import BytesIO
import json
import logging

from curl_cffi import CurlMime

from app.core import http_client as http

_logger = logging.getLogger(__name__)

# Base URL Telegram Bot API — endpoint format `https://api.telegram.org/bot<token>/<method>`.
# Token nằm trong path → PHẢI URL-encode nếu token chứa ký tự lạ, nhưng
# token format Telegram (`<int>:<base64url>`) an toàn cho URL không cần
# encode. Vẫn dùng f-string trực tiếp — nếu future Telegram đổi format,
# thêm quote ở đây.
_API_BASE = "https://api.telegram.org"

# Timeout đủ dài cho sendPhoto (~50KB PNG QR upload trên mạng chậm). Ngắn
# hơn 30s để hook không block scheduler lâu.
_DEFAULT_TIMEOUT_SECONDS = 15.0

# Timeout cho polling session — PHẢI lớn hơn giá trị `timeout` (long-poll
# giây) truyền vào `getUpdates`, mặc định 25s. 30s đủ margin cho network
# latency round-trip mà không giữ connection treo vô hạn.
_POLLING_TIMEOUT_SECONDS = 30.0


def _prepare_telegram_qr_png(photo_bytes: bytes) -> bytes:
    """Add a modest quiet zone so Telegram photo preview stays scannable while keeping QR large and crisp."""
    if not photo_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return photo_bytes
    try:
        from PIL import Image

        source = Image.open(BytesIO(photo_bytes)).convert("RGB")
        # High resolution canvas: 1024x1024, QR takes up 92% of the frame (940x940)
        # Leaving a clean 42px quiet zone so scanner camera detects borders instantly
        canvas_size = 1024
        qr_size = 940
        source = source.resize((qr_size, qr_size), Image.Resampling.NEAREST)
        canvas = Image.new("RGB", (canvas_size, canvas_size), "white")
        offset = (
            (canvas_size - source.width) // 2,
            (canvas_size - source.height) // 2,
        )
        canvas.paste(source, offset)
        out = BytesIO()
        canvas.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:
        _logger.warning("telegram QR framing failed; sending raw PNG", exc_info=True)
        return photo_bytes


class TelegramApiError(Exception):
    """Raise khi Telegram Bot API trả `{"ok": false}` hoặc HTTP status
    khác 200 với body có `description`.

    Attributes:
        status_code: HTTP status code từ Telegram (200 nếu API-level error).
        error_code: `error_code` từ body Telegram (VD 401, 429, 400).
        description: `description` từ body Telegram (VD "Bad Request:
            chat not found", "Unauthorized").
        retry_after: Số giây Telegram yêu cầu chờ trước khi gọi lại — chỉ
            có giá trị khi `error_code=429` (body kèm
            `parameters.retry_after`). `None` cho mọi lỗi khác. Retry
            layer (`notifier.py`) dùng field này để tôn trọng rate limit
            thực tế của Telegram thay vì đoán backoff cố định.
    """

    def __init__(
        self,
        status_code: int,
        error_code: int | None,
        description: str,
        retry_after: float | None = None,
    ) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.description = description
        self.retry_after = retry_after
        super().__init__(
            f"Telegram API error: http_status={status_code} "
            f"error_code={error_code} description={description!r} "
            f"retry_after={retry_after!r}"
        )


class TelegramBotClient:
    """`curl_cffi.AsyncSession` wrapper cho Telegram Bot API.

    Client được tạo lazy ở call đầu tiên (không tạo trong `__init__` để
    tránh cần event loop khi startup) và tái sử dụng — giữ 1 kết nối
    HTTP/1.1 keep-alive cho cả process.

    Life-cycle: đóng qua `aclose()` khi shutdown backend.
    """

    def __init__(self, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout_seconds
        self._sending_client: http.AsyncSession | None = None
        self._polling_client: http.AsyncSession | None = None

    def _get_sending_client(self) -> http.AsyncSession:
        """Lazy-init AsyncSession dùng cho request/response nhanh (send*,
        edit*, answerCallbackQuery). Không thread-safe (async-safe OK).
        """
        if self._sending_client is None:
            self._sending_client = http.create_async_client(timeout=self._timeout)
        return self._sending_client

    def _get_polling_client(self) -> http.AsyncSession:
        """Lazy-init AsyncSession riêng cho `get_updates` — timeout dài
        hơn (`_POLLING_TIMEOUT_SECONDS`) để chịu được long-poll của
        Telegram. Tách riêng khỏi sending client để `PollingSupervisor`
        có thể đóng/mở lại session này độc lập (VD khi đổi token) mà
        không ảnh hưởng các call sending đang chạy.
        """
        if self._polling_client is None:
            self._polling_client = http.create_async_client(
                timeout=_POLLING_TIMEOUT_SECONDS
            )
        return self._polling_client

    async def aclose(self) -> None:
        """Đóng cả 2 session (sending + polling). Idempotent — gọi nhiều
        lần an toàn.
        """
        if self._sending_client is not None:
            await self._sending_client.close()
            self._sending_client = None
        if self._polling_client is not None:
            await self._polling_client.close()
            self._polling_client = None

    async def close_polling_session(self) -> None:
        """Đóng riêng polling session (dùng khi `PollingSupervisor` cancel
        hoặc đổi token). Lỗi đóng session cũ CHỈ log warning — không raise,
        để không chặn việc tạo session mới ngay sau đó.
        """
        if self._polling_client is not None:
            try:
                await self._polling_client.close()
            except Exception:
                _logger.warning(
                    "Lỗi khi đóng polling session cũ — bỏ qua, tiếp tục tạo mới",
                    exc_info=True,
                )
            finally:
                self._polling_client = None

    async def reopen_polling_session(self) -> None:
        """Đóng polling session hiện tại (nếu có) rồi để lần gọi
        `_get_polling_client()` tiếp theo (VD trong `get_updates`) tự tạo
        session mới. Dùng khi `PollingSupervisor` đổi token hoặc phục hồi
        sau lỗi 409 Conflict.
        """
        await self.close_polling_session()

    async def send_photo(
        self,
        token: str,
        chat_id: str,
        photo_bytes: bytes,
        caption: str,
        *,
        photo_filename: str = "qr.png",
        reply_markup: dict | None = None,
    ) -> dict:
        """Gọi `POST /bot<token>/sendPhoto` với multipart form.

        Args:
            token: Bot token (`<int>:<base64url>`).
            chat_id: Target chat (string — có thể là số âm cho group).
            photo_bytes: Nội dung binary file PNG.
            caption: HTML caption (Telegram hỗ trợ subset: <b>, <code>,
                <a href>). Max 1024 ký tự theo Bot API.
            photo_filename: Tên file trong multipart part — Telegram
                ignore nội dung nhưng cần Content-Type kèm.
            reply_markup: Inline keyboard dict theo format Telegram Bot
                API (VD `{"inline_keyboard": [[...]]}`). `None` (default)
                = không kèm field này (giữ hành vi cũ trước khi Pull_Mode
                cần đính nút "Hoàn thành"/"Thất bại" ngay trên message QR
                — R8.3 spec `telegram-pull-job-mode`). Telegram Bot API
                `sendPhoto` chấp nhận `reply_markup` như 1 field JSON-encoded
                trong multipart form (giống `chat_id`/`caption`).

        Returns:
            Dict `result` từ body Telegram (message metadata).

        Raises:
            TelegramApiError: HTTP status != 200 hoặc `ok=false`.
            http.HTTPError: Network error / timeout.
        """
        client = self._get_sending_client()
        url = f"{_API_BASE}/bot{token}/sendPhoto"
        photo_bytes = _prepare_telegram_qr_png(photo_bytes)
        # curl_cffi 0.15 KHÔNG chấp nhận `files=` như httpx — phải build
        # `CurlMime` với addpart cho MỖI field (kể cả text). Boundary +
        # Content-Type: multipart/form-data do curl tự set khi có
        # `multipart=` kwarg. `data=` không thể mix với `multipart=`.
        #
        # Note quan trọng: CurlMime giữ curl handle nội tại, cần
        # `mime.close()` sau khi `perform`. Ở curl_cffi 0.15 việc close
        # tự động xảy ra khi CurlMime bị garbage-collected — không cần
        # gọi thủ công cho trường hợp nhỏ như 1 photo + 3 text field. Nếu
        # scale lên upload lớn, xem xét explicit try/finally.
        mime = CurlMime()
        mime.addpart(
            name="photo",
            content_type="image/png",
            filename=photo_filename,
            data=photo_bytes,
        )
        mime.addpart(name="chat_id", data=chat_id.encode("utf-8"))
        mime.addpart(name="caption", data=caption.encode("utf-8"))
        mime.addpart(name="parse_mode", data=b"HTML")
        if reply_markup is not None:
            mime.addpart(
                name="reply_markup",
                data=json.dumps(reply_markup).encode("utf-8"),
            )
        response = await client.post(url, multipart=mime)
        return self._parse_response(response)

    async def send_message(
        self,
        token: str,
        chat_id: str,
        text: str,
        *,
        reply_markup: dict | None = None,
    ) -> dict:
        """Gọi `POST /bot<token>/sendMessage` (JSON body, HTML parse_mode).

        Dùng cho nút "Test gửi" trong UI — không cần upload ảnh, chỉ text.

        Args:
            token: Bot token.
            chat_id: Target chat.
            text: Nội dung text (HTML parse_mode).
            reply_markup: Inline keyboard / reply keyboard dict theo
                format Telegram Bot API (VD `{"inline_keyboard": [[...]]}`).
                `None` = không kèm markup (field bị bỏ qua khỏi payload).

        Returns:
            Dict `result` từ body Telegram (message metadata).

        Raises:
            TelegramApiError: HTTP status != 200 hoặc `ok=false`.
            http.HTTPError: Network error / timeout.
        """
        client = self._get_sending_client()
        url = f"{_API_BASE}/bot{token}/sendMessage"
        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        response = await client.post(url, json=payload)
        return self._parse_response(response)

    async def send_message_with_reply_to(
        self,
        token: str,
        chat_id: str,
        text: str,
        reply_to_message_id: int,
        *,
        reply_markup: dict | None = None,
    ) -> dict:
        """Gọi `POST /bot<token>/sendMessage` kèm `reply_to_message_id` —
        gửi text như 1 reply trực tiếp vào message cụ thể (VD bot trả lời
        vào message user gửi lệnh trong group).

        Args:
            token: Bot token.
            chat_id: Target chat.
            text: Nội dung text (HTML parse_mode).
            reply_to_message_id: ID message cần reply vào.
            reply_markup: Inline keyboard / reply keyboard dict. `None` =
                không kèm markup.

        Returns:
            Dict `result` từ body Telegram (message metadata).

        Raises:
            TelegramApiError: HTTP status != 200 hoặc `ok=false`.
            http.HTTPError: Network error / timeout.
        """
        client = self._get_sending_client()
        url = f"{_API_BASE}/bot{token}/sendMessage"
        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_to_message_id": reply_to_message_id,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        response = await client.post(url, json=payload)
        return self._parse_response(response)

    async def get_updates(
        self,
        token: str,
        offset: int,
        *,
        timeout: int = 25,
    ) -> list[dict]:
        """Gọi `POST /bot<token>/getUpdates` — long-poll cho
        `PollingSupervisor`.

        Dùng POLLING session (`_get_polling_client()`, HTTP timeout 30s)
        — PHẢI lớn hơn `timeout` (giây long-poll của Telegram, mặc định
        25s) để client không tự cắt kết nối trước khi Telegram trả về.

        Args:
            token: Bot token.
            offset: `update_id` kế tiếp cần lấy (thường = last update_id
                + 1) — Telegram tự xoá update đã ACK qua offset này.
            timeout: Số giây Telegram giữ connection mở để long-poll chờ
                update mới trước khi trả `result: []`. Mặc định 25s.

        Returns:
            List dict `result` từ body Telegram — mỗi item là 1 Update
            object (có thể rỗng nếu không có update mới trong khoảng
            `timeout`).

        Raises:
            TelegramApiError: HTTP status != 200 hoặc `ok=false`. Đặc
                biệt quan trọng: HTTP 409 Conflict (bot instance khác
                đang polling cùng token) surface với `status_code=409` để
                `PollingSupervisor` phát hiện và xử lý.
            http.HTTPError: Network error / timeout.
        """
        client = self._get_polling_client()
        url = f"{_API_BASE}/bot{token}/getUpdates"
        payload = {"offset": offset, "timeout": timeout}
        response = await client.post(url, json=payload)
        return self._parse_list_response(response)

    async def answer_callback_query(
        self,
        token: str,
        callback_query_id: str,
        text: str = "",
        *,
        show_alert: bool = False,
    ) -> dict:
        """Gọi `POST /bot<token>/answerCallbackQuery` — ACK 1 callback từ
        inline button, tắt trạng thái loading trên client Telegram.

        Args:
            token: Bot token.
            callback_query_id: ID callback query cần answer.
            text: Notification text hiện ngắn cho user (rỗng = không hiện).
            show_alert: True = hiện dạng alert dialog thay vì toast nhỏ.

        Returns:
            Dict `result` từ body Telegram (thường `True`/`{}`).

        Raises:
            TelegramApiError: HTTP status != 200 hoặc `ok=false`.
            http.HTTPError: Network error / timeout.
        """
        client = self._get_sending_client()
        url = f"{_API_BASE}/bot{token}/answerCallbackQuery"
        payload = {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        }
        response = await client.post(url, json=payload)
        return self._parse_response(response)

    async def edit_message_text(
        self,
        token: str,
        chat_id: str,
        message_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
    ) -> dict:
        """Gọi `POST /bot<token>/editMessageText` — sửa nội dung text của
        1 message đã gửi (dùng cho status card Pull_Mode: cùng 1 message
        cập nhật realtime qua nhiều pha "đang tìm/đang xử lý/retry/kết quả"
        thay vì gửi nhiều message rời).

        Args:
            token: Bot token.
            chat_id: Chat chứa message.
            message_id: ID message cần sửa.
            text: Nội dung text mới (HTML parse_mode).
            reply_markup: Inline keyboard mới. `None` = bỏ field khỏi
                payload — Telegram giữ nguyên keyboard hiện có, KHÔNG xoá.
                Truyền `{"inline_keyboard": []}` nếu muốn xoá keyboard.

        Returns:
            Dict `result` từ body Telegram (message metadata).

        Raises:
            TelegramApiError: HTTP status != 200 hoặc `ok=false` (VD
                message quá cũ để edit, hoặc nội dung không đổi — Telegram
                trả `Bad Request: message is not modified`).
            http.HTTPError: Network error / timeout.
        """
        client = self._get_sending_client()
        url = f"{_API_BASE}/bot{token}/editMessageText"
        payload: dict = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        response = await client.post(url, json=payload)
        return self._parse_response(response)

    async def edit_message_caption(
        self,
        token: str,
        chat_id: str,
        message_id: int,
        caption: str,
    ) -> dict:
        """Gọi `POST /bot<token>/editMessageCaption` — sửa caption của 1
        message đã gửi (VD cập nhật trạng thái sau khi user bấm nút).

        Args:
            token: Bot token.
            chat_id: Chat chứa message.
            message_id: ID message cần sửa caption.
            caption: Caption mới (HTML parse_mode).

        Returns:
            Dict `result` từ body Telegram (message metadata).

        Raises:
            TelegramApiError: HTTP status != 200 hoặc `ok=false`.
            http.HTTPError: Network error / timeout.
        """
        client = self._get_sending_client()
        url = f"{_API_BASE}/bot{token}/editMessageCaption"
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "caption": caption,
            "parse_mode": "HTML",
        }
        response = await client.post(url, json=payload)
        return self._parse_response(response)

    async def edit_message_reply_markup(
        self,
        token: str,
        chat_id: str,
        message_id: int,
        *,
        reply_markup: dict | None = None,
    ) -> dict:
        """Gọi `POST /bot<token>/editMessageReplyMarkup` — sửa/xoá inline
        keyboard của 1 message đã gửi.

        Args:
            token: Bot token.
            chat_id: Chat chứa message.
            message_id: ID message cần sửa reply markup.
            reply_markup: Markup mới. `None` = bỏ field khỏi payload —
                Telegram hiểu là xoá toàn bộ inline keyboard hiện có.

        Returns:
            Dict `result` từ body Telegram (message metadata).

        Raises:
            TelegramApiError: HTTP status != 200 hoặc `ok=false`.
            http.HTTPError: Network error / timeout.
        """
        client = self._get_sending_client()
        url = f"{_API_BASE}/bot{token}/editMessageReplyMarkup"
        payload: dict = {
            "chat_id": chat_id,
            "message_id": message_id,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        response = await client.post(url, json=payload)
        return self._parse_response(response)

    @staticmethod
    def _check_ok_or_raise(response: http.Response):
        """Parse body JSON + kiểm tra `ok` field — helper CHUNG cho mọi
        method, trả về nguyên vẹn giá trị thô `result` (KHÔNG ép kiểu)
        để caller (`_parse_response` cho dict, `_parse_list_response` cho
        list của `getUpdates`) tự quyết định type mong đợi. Tách riêng vì
        `result` của `getUpdates` là LIST — nếu ép về dict như trước đây
        sẽ SILENT trả `{}`, che giấu bug mất update.

        Raise `TelegramApiError` khi:
        - HTTP status != 200 (kể cả body không hợp lệ) — bao gồm 409
          Conflict khi có instance khác đang polling cùng token
          (`getUpdates`).
        - Body `ok=false` (Telegram-level error, VD chat_id không tồn tại).
        """
        try:
            body = response.json()
        except ValueError:
            # Body không phải JSON — thường xảy ra khi token sai format
            # (Telegram trả HTML 404 page). Raise với description trần.
            raise TelegramApiError(
                status_code=response.status_code,
                error_code=None,
                description=f"non-json response: {response.text[:200]}",
            )

        if not isinstance(body, dict):
            raise TelegramApiError(
                status_code=response.status_code,
                error_code=None,
                description=f"unexpected body type: {type(body).__name__}",
            )

        if body.get("ok") is True:
            return body.get("result")

        # Telegram-level error hoặc HTTP non-200. Rate limit (429) kèm
        # `parameters.retry_after` (giây) — extract để retry layer tôn
        # trọng đúng thời gian Telegram yêu cầu thay vì backoff đoán mò.
        retry_after: float | None = None
        parameters = body.get("parameters")
        if isinstance(parameters, dict):
            raw_retry_after = parameters.get("retry_after")
            if isinstance(raw_retry_after, (int, float)) and not isinstance(
                raw_retry_after, bool
            ):
                retry_after = float(raw_retry_after)

        raise TelegramApiError(
            status_code=response.status_code,
            error_code=body.get("error_code"),
            description=str(body.get("description", "")),
            retry_after=retry_after,
        )

    @classmethod
    def _parse_list_response(cls, response: http.Response) -> list[dict]:
        """Parse Telegram response cho endpoint trả `result` là LIST (chỉ
        `getUpdates` dùng path này) — dùng chung `_check_ok_or_raise`.
        """
        result = cls._check_ok_or_raise(response)
        return result if isinstance(result, list) else []

    @classmethod
    def _parse_response(cls, response: http.Response) -> dict:
        """Parse Telegram response chuẩn `{ok, result | error_code, description}`
        cho trường hợp `result` là dict (mọi method trừ `get_updates`) —
        dùng chung `_check_ok_or_raise`.
        """
        result = cls._check_ok_or_raise(response)
        return result if isinstance(result, dict) else {}
