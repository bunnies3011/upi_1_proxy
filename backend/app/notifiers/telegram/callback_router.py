"""Callback_Router — dispatch mọi update loại `callback_query` (nút bấm
inline keyboard) theo tiền tố `callback_query["data"]` (design.md §3.4,
Requirement 18.1, 18.2, 18.3).

Trách nhiệm DUY NHẤT: nhận `callback_query` (dict thô từ Telegram Update),
đọc tiền tố của `data`, rồi định tuyến tới đúng coordinator xử lý — module
này KHÔNG tự chứa logic nghiệp vụ nào ngoài việc dispatch + 2 nhánh
fallback (stub `plus_check:` + fallback không nhận diện được):
    - `data.startswith("pull_job:")` → `PullJobCoordinator.handle_callback`
      (task 27, `pull_mode.py`) — coordinator này đã tự bind
      `job_manager`/`client`/`settings`/`bot_token_getter` qua `__init__`,
      nên chỉ cần truyền nguyên `callback_query` (R18.2).
    - `data.startswith("plus_check:")` → stub tối giản (R18.1). Plus_Check_Coordinator
      ĐẦY ĐỦ (xử lý nút xác nhận thanh toán Plus trong flow push-mode
      cũ) THUỘC PHẠM VI SPEC `telegram-callback-verify-plus`, KHÔNG
      implement ở đây — chỉ ACK best-effort để client Telegram không hiện
      trạng thái loading treo vô hạn trên nút.
    - Không khớp tiền tố nào → log warning + `answerCallbackQuery` rỗng
      best-effort (R18.3) — không raise, không làm crash polling loop.

Boundary dependency direction (Requirement 20): module này CHỈ gọi
`TelegramBotClient.answer_callback_query` (cho 2 nhánh không có
coordinator riêng) và `PullJobCoordinator.handle_callback` (đã có sẵn từ
task 27) — không chứa logic đọc/ghi `JobManager` trực tiếp.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.notifiers.telegram.client import TelegramBotClient

if TYPE_CHECKING:
    from app.core.job_manager import JobManager
    from app.notifiers.telegram.pull_mode import PullJobCoordinator

logger = logging.getLogger(__name__)


async def handle_callback_query(
    callback_query: dict,
    *,
    client: TelegramBotClient,
    bot_token: str,
    job_manager: "JobManager",
    pull_coordinator: "PullJobCoordinator",
) -> None:
    """Entry point Callback_Router (design.md §3.4) — dispatch theo tiền
    tố `callback_query["data"]`.

    R18.1: `data.startswith("plus_check:")` → stub tối giản, KHÔNG
    implement đầy đủ Plus_Check_Coordinator ở đây (xem docstring module).
    R18.2: `data.startswith("pull_job:")` → `pull_coordinator.handle_callback`
    (coordinator tự đứng độc lập, chỉ cần nguyên `callback_query`).
    R18.3: không khớp tiền tố nào → log warning + ACK rỗng best-effort.

    Args:
        callback_query: Dict thô `callback_query` từ Telegram Update.
        client: `TelegramBotClient` dùng cho `answer_callback_query` ở 2
            nhánh không có coordinator riêng (stub `plus_check:` + fallback).
        bot_token: Bot token hiện tại (đã resolve từ Settings bởi caller).
        job_manager: `JobManager` — hiện KHÔNG được dùng trực tiếp bởi
            logic dispatch trong hàm này (nhánh `pull_job:` đi qua
            `pull_coordinator` — coordinator đó đã tự bind `job_manager`
            riêng của nó; nhánh `plus_check:`/fallback chỉ cần
            `answerCallbackQuery`, không cần đọc/ghi job). Giữ trong
            signature để khớp đúng hợp đồng design.md §3.4 — dự phòng cho
            Plus_Check_Coordinator đầy đủ (spec `telegram-callback-verify-plus`,
            ngoài phạm vi spec này) có thể cần tới.
        pull_coordinator: `PullJobCoordinator` xử lý mọi callback tiền tố
            `pull_job:` (task 27, `pull_mode.py`).
    """
    data = callback_query.get("data", "")

    if data.startswith("plus_check:"):
        await _handle_plus_check_stub(callback_query, client=client, bot_token=bot_token)
    elif data.startswith("pull_job:"):
        await pull_coordinator.handle_callback(callback_query)
    else:
        logger.warning(
            "callback_router: unrecognized callback_data prefix: %r", data
        )
        await _best_effort_answer(client, bot_token, callback_query.get("id", ""))


async def _handle_plus_check_stub(
    callback_query: dict,
    *,
    client: TelegramBotClient,
    bot_token: str,
) -> None:
    """Stub tối giản — Plus_Check_Coordinator đầy đủ thuộc phạm vi spec
    `telegram-callback-verify-plus`, chưa implement ở đây. Chỉ ACK để
    Telegram client không hiện loading treo vô hạn.
    """
    try:
        await client.answer_callback_query(
            bot_token,
            callback_query["id"],
            text="Chức năng đang được phát triển",
            show_alert=False,
        )
    except Exception as exc:
        # FIX C: bot token nằm trong URL request nên KHÔNG dùng
        # `exc_info=True` (traceback có thể lộ token). Chỉ log loại + message.
        logger.warning(
            "callback_router: answer_callback_query (plus_check stub) failed "
            "error_type=%s detail=%s",
            type(exc).__name__,
            str(exc),
        )


async def _best_effort_answer(
    client: TelegramBotClient, bot_token: str, callback_query_id: str
) -> None:
    """`answerCallbackQuery` rỗng best-effort cho nhánh fallback (R18.3) —
    lỗi CHỈ log warning, KHÔNG raise (không làm crash polling loop)."""
    try:
        await client.answer_callback_query(
            bot_token, callback_query_id, text="", show_alert=False
        )
    except Exception as exc:
        # FIX C: bot token nằm trong URL request nên KHÔNG dùng
        # `exc_info=True` (traceback có thể lộ token). Chỉ log loại + message.
        logger.warning(
            "callback_router: answer_callback_query (fallback) failed "
            "error_type=%s detail=%s",
            type(exc).__name__,
            str(exc),
        )
