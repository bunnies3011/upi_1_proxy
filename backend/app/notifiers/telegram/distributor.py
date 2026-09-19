"""Round-robin chat_id distributor cho Telegram notifier.

State: 1 int counter (cursor) tăng đơn điệu mỗi lần `pick(chat_ids)` được
gọi. In-memory only — reset khi restart backend (chốt với user: chấp
nhận, ngữ nghĩa "phân bổ đều theo thời gian dài" vẫn giữ, mất fairness
ngắn hạn sau restart không đáng kể).

Thread/async safety: JobManager gọi `_invoke_terminal_hooks` tuần tự
trong `_run_handler` (per-job task) — 2 job success đồng thời sẽ chạy 2
task async khác nhau, mỗi task gọi `pick()` → race trên `_cursor`. Dùng
`asyncio.Lock` để đảm bảo cursor tăng đúng thứ tự, tránh 2 job cùng nhận
chung 1 chat_id do đọc cursor trước khi task kia increment.

Empty list handling: `pick([])` raise `ValueError` — Fail_Fast, caller
(TelegramNotifier) phải check `chat_ids` trước khi gọi. Không silently
return `None` để tránh caller vô tình gửi tới chat_id trống.
"""

from __future__ import annotations

import asyncio


class RoundRobinDistributor:
    """In-memory round-robin selector cho list chat_id.

    Attributes:
        _cursor: Vị trí tiếp theo trong list. Đọc + increment atomic dưới
            `_lock`. Modulo với len(chat_ids) tại thời điểm pick (không
            lưu trước) để list co giãn qua thời gian không gây IndexError.
    """

    def __init__(self) -> None:
        self._cursor: int = 0
        self._lock: asyncio.Lock = asyncio.Lock()

    async def pick(self, chat_ids: list[str]) -> str:
        """Trả về chat_id kế tiếp theo round-robin, tăng cursor.

        Args:
            chat_ids: Snapshot list chat_id tại thời điểm gọi. Caller
                (TelegramNotifier) đọc từ Settings live trước mỗi lần
                pick, đảm bảo user cập nhật chat_ids qua UI có hiệu lực
                ngay ở lần notify kế tiếp.

        Returns:
            1 chat_id string. Cursor tăng 1 sau mỗi call.

        Raises:
            ValueError: Nếu `chat_ids` rỗng — caller phải guard trước.
        """
        if not chat_ids:
            raise ValueError("chat_ids không được rỗng")
        async with self._lock:
            index = self._cursor % len(chat_ids)
            self._cursor += 1
            return chat_ids[index]

    def snapshot_cursor(self) -> int:
        """Đọc cursor hiện tại (không mutate) — dùng cho test/debug/observability.

        KHÔNG dùng cho logic distribution (đọc-ngoài-lock có race). Chỉ
        báo cáo state.
        """
        return self._cursor

    async def reset(self) -> int:
        """Reset cursor về 0 — chat_id kế tiếp sẽ là phần tử đầu list.

        Dùng khi user muốn "phân bổ lại từ đầu" (VD sau khi thêm/xóa
        chat vào list, hoặc chỉ đơn giản muốn round-robin restart để
        chat đầu tiên nhận notify tiếp theo).

        Trả về cursor CŨ (trước khi reset) — hữu ích cho UI báo "đã
        reset (cursor N → 0)" hoặc endpoint `/reset-cursor` trả metadata.
        """
        async with self._lock:
            old = self._cursor
            self._cursor = 0
            return old
