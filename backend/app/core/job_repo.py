"""Job Repository — persist Job state qua restart Backend_Service.

Thuộc `core/` (Payment_Module_Boundary — Requirement 13): KHÔNG import từ
`app.payments.*`. Repository chỉ CRUD row trong bảng `jobs` — không biết
gì về `_JobRecord`/`Job` cụ thể (caller `JobManager` tự convert record
→ dict rồi truyền vào).

Yêu cầu nghiệp vụ:
- Tắt web/backend rồi bật lại phải nhớ TOÀN BỘ job (bao gồm terminal
  error/success và pending chưa chạy).
- Ordering (`order_num`) cố định cho từng job — persist để restart giữ
  đúng thứ tự tạo lần đầu.
- Job đang `RUNNING` lúc shutdown: JobManager load sẽ chuyển về `PENDING`
  để scheduler kick lại flow từ đầu (proxy lease/network state không thể
  resume qua process restart).

Không persist:
- `logs` (realtime, nhiều — DB write mỗi log = slow; chấp nhận mất log
  qua restart, artifact PNG vẫn còn trên disk).
- `cancellation_token`, `handler_task`, `lease` (chỉ có ý nghĩa runtime).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.db import DbEngine

_logger = logging.getLogger(__name__)


@dataclass
class JobRow:
    """1 row trong bảng `jobs`, snapshot state để persist/load.

    Đây là DTO thuần — KHÔNG có method business logic. `JobManager` tự
    convert `_JobRecord` ↔ `JobRow` khi upsert/load.

    `settings_snapshot` là dict Python (đã decode JSON). Repository chịu
    trách nhiệm serialize/deserialize giữa dict và cột `settings_snapshot_json`.
    """

    job_id: str
    payment_method: str
    account_line: str
    status: str
    order_num: int
    created_at: float
    updated_at: float
    dedup_key: str | None
    artifact_path: str | None
    error_code: str | None
    error_message: str | None
    payment_link: str | None
    settings_snapshot: dict[str, Any]
    # `retry_count`: số lần auto-retry đã dùng. Nhớ qua restart để retry
    # budget không bị reset khi backend khởi động lại giữa 2 lần chạy.
    # Default 0 cho backward compat với DB row cũ (migration ALTER TABLE
    # đã set default 0 ở tầng SQL).
    retry_count: int = 0
    # `started_at`/`finished_at`: mốc thời gian chạy job THẬT (khác
    # `updated_at` là mốc broadcast SSE gần nhất — có thể tăng liên tục
    # theo log). Dùng để hiển thị elapsed đúng:
    #   - `pending` → chưa từng chạy: cả 2 None.
    #   - `running` → started_at set, finished_at None → FE tick 1s tính
    #     `now - started_at`.
    #   - terminal (`qr_ready`/`error`/`stopped`) → cả 2 set: FE hiển thị
    #     `finished_at - started_at` ĐÓNG BĂNG, không đếm nữa.
    # Nullable ở DB — DB row cũ trước migration đọc None → job hiển thị `—`.
    started_at: float | None = None
    finished_at: float | None = None
    # `telegram_notifications`: list bản ghi mỗi lần notifier gửi QR PNG
    # tới 1 chat Telegram. Shape entry (khớp `TelegramNotificationEntry`
    # ở `api/schemas.py`):
    #   {"chat_id": str, "chat_label": str, "sent_at": float,
    #    "success": bool, "error": dict | None}
    # Default `[]` khi job chưa từng notified — repo tự serialize sang JSON
    # ở cột `telegram_notifications_json`. Giữ list-of-dict thuần thay vì
    # dataclass để pass thẳng qua Pydantic model + SSE payload không cần
    # convert 2 lần.
    telegram_notifications: list[dict[str, Any]] = field(default_factory=list)
    # `pull_assignment_state`/`pull_assigned_telegram_user_id`/
    # `pull_origin_chat_id`/`pull_outcome`/`pull_assigned_username`/
    # `pull_assigned_first_name`: Job_Pull_Assignment (feature
    # telegram-pull-job-mode, Requirement 21.1). NULL cho job Push_Mode —
    # chỉ job tạo khi `telegram.mode == "pull"` mới populate. Default
    # `None` cho backward compat với DB row cũ trước migration (R21.2).
    pull_assignment_state: str | None = None
    pull_assigned_telegram_user_id: str | None = None
    pull_origin_chat_id: str | None = None
    pull_outcome: str | None = None
    pull_assigned_username: str | None = None
    pull_assigned_first_name: str | None = None
    # `plus_check_state`/`plus_check_attempts`: state machine 2-lần-thử
    # "armed→checking→verified/exhausted_2" (định nghĩa gốc ở spec
    # `telegram-callback-verify-plus`, dùng chung field cho feature này).
    # NOT NULL ở DB — default `"armed"`/`0` khớp SQL DEFAULT, cho row cũ
    # trước migration đọc ra đúng giá trị khởi tạo.
    plus_check_state: str = "armed"
    plus_check_attempts: int = 0
    # `telegram_message_chat_id`/`telegram_message_id`: định danh message
    # QR đã gửi trên Telegram — cần để `editMessageReplyMarkup` gỡ nút khi
    # verify xong hoặc job bị hủy. Nullable — chỉ job đã gửi message mới có.
    telegram_message_chat_id: str | None = None
    telegram_message_id: int | None = None
    # `held`: cờ orthogonal với `status`. Khi `status="pending"` VÀ
    # `held=True` → job đã tạo nhưng CHƯA vào `_pending_order` (scheduler
    # bỏ qua), chờ user bấm Start. Default `False` cho backward-compat
    # với DB row cũ (migration ALTER TABLE default 0). Terminal state
    # (qr_ready/error/stopped/running) KHÔNG kiểm tra field này —
    # `held` chỉ có ý nghĩa lúc `pending`.
    held: bool = False
    # `plan`: kết quả check-plan cuối cùng của tài khoản (yêu cầu 2026-07
    # persist "Successful accounts" qua reload trang).
    #   - `None` → chưa từng check / rerun reset về None. FE hiển thị "?"
    #     trong badge.
    #   - `"plus"` → đủ điều kiện coi là Successful account (SuccessOutputPanel
    #     lọc theo giá trị này). "Check Plus All" SKIP job đã có `plan="plus"`
    #     (không call ChatGPT nữa) — theo yêu cầu "check plus all bỏ qua
    #     tài khoản plus luôn".
    #   - `"free"` → tài khoản còn Free; UI hiển thị badge FREE ngay sau
    #     reload. Nút "Check Plus All" VẪN check lại (vì free có thể lên
    #     plus theo thời gian).
    # Default `None` cho backward-compat với DB row cũ (migration ALTER
    # TABLE default NULL) → treat như chưa check → hành vi cũ 100%.
    plan: str | None = None


@dataclass
class WorkerStatRow:
    """1 row trong bảng `telegram_worker_stats` — thống kê công/lương của
    1 Telegram_Worker ở Pull_Mode (Requirement 15.1).

    DTO thuần — KHÔNG có method business logic, giống `JobRow`.
    """

    telegram_user_id: str
    username: str | None
    first_name: str | None
    success_count: int
    fail_count: int
    updated_at: str


@dataclass
class BatchTallyRow:
    """1 row trong bảng `telegram_batch_tally` — counters Plus/expired
    theo chat + id message tally đang pin.

    DTO thuần — KHÔNG có method business logic, giống `WorkerStatRow`.
    """

    chat_id: str
    plus_count: int
    expired_count: int
    tally_message_id: int | None
    updated_at: str


class JobRepository:
    """CRUD async cho bảng `jobs` trong SQLite.

    Dùng chung `DbEngine` với `SettingsRepository` — 1 connection duy nhất
    per Backend_Service process. Mọi thao tác đều `commit()` ngay để đảm
    bảo state persist ngay cả khi process bị kill đột ngột (Fail_Fast:
    KHÔNG accumulate transaction chờ commit định kỳ, vì restart mà mất
    state đã upsert vào memory = mất job — phá hợp đồng "nhớ qua restart").
    """

    def __init__(self, engine: "DbEngine") -> None:
        self._engine = engine

    async def upsert(self, row: JobRow) -> None:
        """Insert nếu chưa có, update nếu đã có (theo `job_id` PRIMARY KEY).

        Dùng `INSERT ... ON CONFLICT ... DO UPDATE` (upsert atomic của
        SQLite 3.24+). Alternative `INSERT OR REPLACE` sẽ xóa row cũ và
        tạo mới → mất `rowid` và trigger side-effect không mong muốn.
        Upsert cập nhật in-place chính xác từng field, KHÔNG đụng row cũ.
        """
        connection = await self._engine.get_connection()
        settings_json = json.dumps(row.settings_snapshot, ensure_ascii=False)
        telegram_json = json.dumps(
            row.telegram_notifications, ensure_ascii=False
        )
        await connection.execute(
            """
            INSERT INTO jobs (
                job_id, payment_method, account_line, status, order_num,
                created_at, updated_at, dedup_key,
                artifact_path, error_code, error_message, payment_link,
                settings_snapshot_json, retry_count,
                started_at, finished_at, telegram_notifications_json,
                pull_assignment_state, pull_assigned_telegram_user_id,
                pull_origin_chat_id, pull_outcome, pull_assigned_username,
                pull_assigned_first_name, plus_check_state,
                plus_check_attempts, telegram_message_chat_id,
                telegram_message_id, held, plan
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(job_id) DO UPDATE SET
                payment_method = excluded.payment_method,
                account_line = excluded.account_line,
                status = excluded.status,
                order_num = excluded.order_num,
                updated_at = excluded.updated_at,
                dedup_key = excluded.dedup_key,
                artifact_path = excluded.artifact_path,
                error_code = excluded.error_code,
                error_message = excluded.error_message,
                payment_link = excluded.payment_link,
                settings_snapshot_json = excluded.settings_snapshot_json,
                retry_count = excluded.retry_count,
                started_at = excluded.started_at,
                finished_at = excluded.finished_at,
                telegram_notifications_json = excluded.telegram_notifications_json,
                pull_assignment_state = excluded.pull_assignment_state,
                pull_assigned_telegram_user_id = excluded.pull_assigned_telegram_user_id,
                pull_origin_chat_id = excluded.pull_origin_chat_id,
                pull_outcome = excluded.pull_outcome,
                pull_assigned_username = excluded.pull_assigned_username,
                pull_assigned_first_name = excluded.pull_assigned_first_name,
                plus_check_state = excluded.plus_check_state,
                plus_check_attempts = excluded.plus_check_attempts,
                telegram_message_chat_id = excluded.telegram_message_chat_id,
                telegram_message_id = excluded.telegram_message_id,
                held = excluded.held,
                plan = excluded.plan
            """,
            (
                row.job_id,
                row.payment_method,
                row.account_line,
                row.status,
                row.order_num,
                row.created_at,
                row.updated_at,
                row.dedup_key,
                row.artifact_path,
                row.error_code,
                row.error_message,
                row.payment_link,
                settings_json,
                row.retry_count,
                row.started_at,
                row.finished_at,
                telegram_json,
                row.pull_assignment_state,
                row.pull_assigned_telegram_user_id,
                row.pull_origin_chat_id,
                row.pull_outcome,
                row.pull_assigned_username,
                row.pull_assigned_first_name,
                row.plus_check_state,
                row.plus_check_attempts,
                row.telegram_message_chat_id,
                row.telegram_message_id,
                1 if row.held else 0,
                row.plan,
            ),
        )
        await connection.commit()

    async def delete(self, job_id: str) -> None:
        """Xóa 1 job khỏi DB. No-op nếu không tồn tại."""
        connection = await self._engine.get_connection()
        await connection.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
        await connection.commit()

    async def delete_many(self, job_ids: list[str]) -> None:
        """Bulk delete — nhanh hơn N lần `delete()` cho `clear_jobs` filter."""
        if not job_ids:
            return
        connection = await self._engine.get_connection()
        placeholders = ",".join(["?"] * len(job_ids))
        await connection.execute(
            f"DELETE FROM jobs WHERE job_id IN ({placeholders})",
            tuple(job_ids),
        )
        await connection.commit()

    async def list_all(self) -> list[JobRow]:
        """Đọc tất cả job đã persist, sort theo `order_num ASC`.

        Dùng cho `JobManager.load_from_db()` tại startup — rebuild in-memory
        state theo đúng thứ tự tạo ban đầu.
        """
        connection = await self._engine.get_connection()
        cursor = await connection.execute(
            """
            SELECT
                job_id, payment_method, account_line, status, order_num,
                created_at, updated_at, dedup_key,
                artifact_path, error_code, error_message, payment_link,
                settings_snapshot_json, retry_count,
                started_at, finished_at, telegram_notifications_json,
                pull_assignment_state, pull_assigned_telegram_user_id,
                pull_origin_chat_id, pull_outcome, pull_assigned_username,
                pull_assigned_first_name, plus_check_state,
                plus_check_attempts, telegram_message_chat_id,
                telegram_message_id, held, plan
            FROM jobs
            ORDER BY order_num ASC
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()

        result: list[JobRow] = []
        for r in rows:
            try:
                snapshot = json.loads(r[12]) if r[12] else {}
            except (ValueError, TypeError):
                # DB được sửa thô bên ngoài với JSON hỏng — không crash
                # load, log rồi treat như snapshot rỗng.
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "settings_snapshot_json corrupted for job_id=%s — fallback empty dict",
                    r[0],
                )
                snapshot = {}
            if not isinstance(snapshot, dict):
                snapshot = {}
            # `retry_count` cột thứ 14 — DB đã migrate thì có int, chưa
            # migrate thì SELECT sẽ báo lỗi (init_schema đã chạy trước
            # load_from_db). Fail-safe: dùng 0 nếu null (không nên xảy ra
            # vì cột NOT NULL DEFAULT 0).
            retry_count_raw = r[13] if len(r) > 13 else 0
            try:
                retry_count = int(retry_count_raw) if retry_count_raw is not None else 0
            except (TypeError, ValueError):
                retry_count = 0
            # `started_at`/`finished_at` — cột 15, 16 (nullable REAL). Row
            # tạo trước migration → NULL → Python None. Fail-safe convert
            # str/int → float; giá trị không parse được → None.
            def _parse_optional_float(raw: Any) -> float | None:
                if raw is None:
                    return None
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    return None

            started_at = _parse_optional_float(r[14]) if len(r) > 14 else None
            finished_at = _parse_optional_float(r[15]) if len(r) > 15 else None
            # `telegram_notifications_json` cột thứ 17 — JSON array. DB row
            # cũ trước migration đọc None → parse fallback list rỗng.
            telegram_notifications: list[dict[str, Any]] = []
            if len(r) > 16 and r[16]:
                try:
                    parsed = json.loads(r[16])
                    if isinstance(parsed, list):
                        # Filter phần tử không phải dict — DB có thể bị sửa
                        # thô bên ngoài, tránh crash load vì 1 entry hỏng.
                        telegram_notifications = [
                            e for e in parsed if isinstance(e, dict)
                        ]
                except (ValueError, TypeError):
                    import logging as _logging
                    _logging.getLogger(__name__).warning(
                        "telegram_notifications_json corrupted for job_id=%s — "
                        "fallback empty list",
                        r[0],
                    )
                    telegram_notifications = []
            # `pull_assignment_state`...`pull_assigned_first_name` — cột
            # 18-23 (nullable TEXT). Row tạo trước migration → NULL →
            # Python None, giữ nguyên (đúng semantic "job Push_Mode").
            pull_assignment_state = r[17] if len(r) > 17 else None
            pull_assigned_telegram_user_id = r[18] if len(r) > 18 else None
            pull_origin_chat_id = r[19] if len(r) > 19 else None
            pull_outcome = r[20] if len(r) > 20 else None
            pull_assigned_username = r[21] if len(r) > 21 else None
            pull_assigned_first_name = r[22] if len(r) > 22 else None
            # `plus_check_state` cột 24 — NOT NULL DEFAULT 'armed' ở DB,
            # nhưng row từ DB cũ trước migration (SELECT vẫn trả cột do
            # ALTER TABLE ADD COLUMN DEFAULT áp cho row cũ) — fallback
            # "armed" nếu None/thiếu để không phá state machine.
            plus_check_state_raw = r[23] if len(r) > 23 else None
            plus_check_state = (
                plus_check_state_raw if plus_check_state_raw else "armed"
            )
            # `plus_check_attempts` cột 25 — cùng pattern fail-safe int
            # parse như `retry_count`.
            plus_check_attempts_raw = r[24] if len(r) > 24 else 0
            try:
                plus_check_attempts = (
                    int(plus_check_attempts_raw)
                    if plus_check_attempts_raw is not None
                    else 0
                )
            except (TypeError, ValueError):
                plus_check_attempts = 0
            # `telegram_message_chat_id`/`telegram_message_id` — cột
            # 26-27 (nullable). `telegram_message_id` là INTEGER nên parse
            # int fail-safe giống các cột optional khác.
            telegram_message_chat_id = r[25] if len(r) > 25 else None
            telegram_message_id_raw = r[26] if len(r) > 26 else None
            # `held` cột 28 — INTEGER NOT NULL DEFAULT 0. Row cũ trước
            # migration đọc 0 (default áp cho row cũ khi ALTER TABLE).
            # Fail-safe convert int → bool; giá trị không parse được → False
            # (đúng semantic "hành vi cũ = không held").
            held_raw = r[27] if len(r) > 27 else 0
            try:
                held = bool(int(held_raw)) if held_raw is not None else False
            except (TypeError, ValueError):
                held = False
            try:
                telegram_message_id = (
                    int(telegram_message_id_raw)
                    if telegram_message_id_raw is not None
                    else None
                )
            except (TypeError, ValueError):
                telegram_message_id = None
            # `plan` cột 29 — TEXT NULL. Row cũ trước migration → NULL →
            # Python None (đúng semantic "chưa từng check"). Chỉ chấp
            # nhận whitelist `{"plus", "free"}` để tránh dữ liệu rác từ
            # sửa DB thô (VD "PLUS" viết hoa, "premium") lọt vào FE làm
            # SuccessOutputPanel hiển thị nhầm — mọi giá trị khác treat
            # như None (chưa check).
            plan_raw = r[28] if len(r) > 28 else None
            plan: str | None = (
                plan_raw if plan_raw in ("plus", "free") else None
            )
            result.append(
                JobRow(
                    job_id=r[0],
                    payment_method=r[1],
                    account_line=r[2],
                    status=r[3],
                    order_num=int(r[4]),
                    created_at=float(r[5]),
                    updated_at=float(r[6]),
                    dedup_key=r[7],
                    artifact_path=r[8],
                    error_code=r[9],
                    error_message=r[10],
                    payment_link=r[11],
                    settings_snapshot=snapshot,
                    retry_count=retry_count,
                    started_at=started_at,
                    finished_at=finished_at,
                    telegram_notifications=telegram_notifications,
                    pull_assignment_state=pull_assignment_state,
                    pull_assigned_telegram_user_id=pull_assigned_telegram_user_id,
                    pull_origin_chat_id=pull_origin_chat_id,
                    pull_outcome=pull_outcome,
                    pull_assigned_username=pull_assigned_username,
                    pull_assigned_first_name=pull_assigned_first_name,
                    plus_check_state=plus_check_state,
                    plus_check_attempts=plus_check_attempts,
                    telegram_message_chat_id=telegram_message_chat_id,
                    telegram_message_id=telegram_message_id,
                    held=held,
                    plan=plan,
                )
            )
        return result

    async def get_max_order(self) -> int:
        """Trả `MAX(order_num)` để JobManager tiếp tục counter sau restart.

        Return 0 nếu bảng rỗng — job đầu tiên sau đó sẽ nhận order = 1.
        """
        connection = await self._engine.get_connection()
        cursor = await connection.execute("SELECT MAX(order_num) FROM jobs")
        row = await cursor.fetchone()
        await cursor.close()
        if row is None or row[0] is None:
            return 0
        return int(row[0])

    async def clear_all(self) -> None:
        """Xóa toàn bộ jobs — dùng cho `clear_jobs(filter='all')`."""
        connection = await self._engine.get_connection()
        await connection.execute("DELETE FROM jobs")
        await connection.commit()

    async def upsert_worker_stat_delta(
        self,
        telegram_user_id: str,
        *,
        success_delta: int = 0,
        fail_delta: int = 0,
        username: str | None = None,
        first_name: str | None = None,
    ) -> None:
        """Tăng atomic `success_count`/`fail_count` của 1 Telegram_Worker
        (Requirement 15.3, 15.4, 22.12).

        `success_delta`/`fail_delta` là GIA TRỊ CỘNG THÊM (thường `1` hoặc
        `0`), KHÔNG phải giá trị tuyệt đối — `ON CONFLICT DO UPDATE SET
        success_count = success_count + excluded.success_count` cộng dồn
        atomic trong 1 statement, tránh race condition đọc-sửa-ghi giữa
        nhiều callback xử lý song song (nhiều Telegram_Worker bấm nút cùng
        lúc). `username`/`first_name` dùng `COALESCE` để giữ giá trị cũ nếu
        lần gọi này không có snapshot mới (`None`).
        """
        connection = await self._engine.get_connection()
        await connection.execute(
            """
            INSERT INTO telegram_worker_stats (
                telegram_user_id, username, first_name,
                success_count, fail_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                success_count = success_count + excluded.success_count,
                fail_count = fail_count + excluded.fail_count,
                username = COALESCE(excluded.username, telegram_worker_stats.username),
                first_name = COALESCE(excluded.first_name, telegram_worker_stats.first_name),
                updated_at = excluded.updated_at
            """,
            (
                telegram_user_id,
                username,
                first_name,
                success_delta,
                fail_delta,
            ),
        )
        await connection.commit()

    async def list_worker_stats(self) -> list[WorkerStatRow]:
        """Đọc toàn bộ Telegram_Worker_Stats, sort theo bảng xếp hạng công
        (Requirement 16.1): `success_count DESC, fail_count ASC,
        telegram_user_id ASC` — worker thành công nhiều nhất lên đầu, hoà
        thì worker ít fail hơn xếp trước, hoà tiếp thì sort ổn định theo id.
        """
        connection = await self._engine.get_connection()
        cursor = await connection.execute(
            """
            SELECT telegram_user_id, username, first_name,
                   success_count, fail_count, updated_at
            FROM telegram_worker_stats
            ORDER BY success_count DESC, fail_count ASC, telegram_user_id ASC
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            WorkerStatRow(
                telegram_user_id=r[0],
                username=r[1],
                first_name=r[2],
                success_count=int(r[3]),
                fail_count=int(r[4]),
                updated_at=r[5],
            )
            for r in rows
        ]

    async def reset_worker_stats(self) -> None:
        """Reset `success_count`/`fail_count` về 0 cho TOÀN BỘ worker,
        giữ nguyên `telegram_user_id`/`username`/`first_name` (Requirement
        17.2 — Reset_Command không xóa danh sách worker, chỉ xóa điểm).
        """
        connection = await self._engine.get_connection()
        await connection.execute(
            "UPDATE telegram_worker_stats SET success_count = 0, fail_count = 0"
        )
        await connection.commit()

    async def try_claim_and_increment_tally(
        self,
        chat_id: str,
        job_id: str,
        *,
        plus_delta: int = 0,
        expired_delta: int = 0,
    ) -> tuple[bool, tuple[int, int] | None]:
        """Claim job outcome once and increment per-chat tally atomically.

        Chạy trên write connection riêng (kèm `claim_lock`) — transaction
        claim+increment không bị `commit`/`rollback` của coroutine khác
        trên shared connection xen vào.

        Single transaction / single commit:
          1. `UPDATE jobs SET plan_outcome_notified = 1 WHERE job_id=?
             AND plan_outcome_notified = 0` — claim when flag is countable.
          2. If not claimed: SELECT to distinguish absent vs already-handled;
             warn when the job row is missing; rollback để đóng txn ẩn
             (UPDATE 0-row), return `(False, None)`.
          3. If claimed: upsert `telegram_batch_tally` with atomic deltas,
             read new counts, commit once, return `(True, (plus, expired))`.

        If the increment step raises, the whole transaction rolls back so
        the claim is not stranded. Flag tri-state: 0=countable, 1=counted,
        2=deferred re-arm (treated as already-handled by the claim check).
        """
        async with self._engine.claim_lock:
            connection = await self._engine.get_write_connection()
            try:
                cursor = await connection.execute(
                    """
                    UPDATE jobs
                    SET plan_outcome_notified = 1
                    WHERE job_id = ? AND plan_outcome_notified = 0
                    """,
                    (job_id,),
                )
                claimed = cursor.rowcount == 1
                await cursor.close()

                if not claimed:
                    cursor = await connection.execute(
                        "SELECT plan_outcome_notified FROM jobs WHERE job_id = ?",
                        (job_id,),
                    )
                    row = await cursor.fetchone()
                    await cursor.close()
                    if row is None:
                        _logger.warning(
                            "try_claim_and_increment_tally: job_id=%s absent — "
                            "cannot claim outcome",
                            job_id,
                        )
                    # Đóng txn ẩn do UPDATE 0-row — không để connection
                    # claim mang transaction mở sang lần claim sau.
                    await connection.rollback()
                    return (False, None)

                await connection.execute(
                    """
                    INSERT INTO telegram_batch_tally (
                        chat_id, plus_count, expired_count, updated_at
                    ) VALUES (
                        ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now')
                    )
                    ON CONFLICT(chat_id) DO UPDATE SET
                        plus_count = plus_count + excluded.plus_count,
                        expired_count = expired_count + excluded.expired_count,
                        updated_at = excluded.updated_at
                    """,
                    (chat_id, plus_delta, expired_delta),
                )
                # SELECT in the same open transaction (serialized aiosqlite
                # connection) — portable vs RETURNING on older SQLite builds.
                cursor = await connection.execute(
                    """
                    SELECT plus_count, expired_count
                    FROM telegram_batch_tally
                    WHERE chat_id = ?
                    """,
                    (chat_id,),
                )
                counts_row = await cursor.fetchone()
                await cursor.close()
                if counts_row is None:
                    raise RuntimeError(
                        f"telegram_batch_tally row missing after upsert "
                        f"for chat_id={chat_id!r}"
                    )

                await connection.commit()
                return (True, (int(counts_row[0]), int(counts_row[1])))
            except Exception:
                await connection.rollback()
                raise

    async def read_batch_tally(
        self, chat_id: str
    ) -> tuple[int, int, int | None]:
        """Return `(plus_count, expired_count, tally_message_id)`.

        Missing chat → `(0, 0, None)`.
        """
        connection = await self._engine.get_connection()
        cursor = await connection.execute(
            """
            SELECT plus_count, expired_count, tally_message_id
            FROM telegram_batch_tally
            WHERE chat_id = ?
            """,
            (chat_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return (0, 0, None)
        message_id = None if row[2] is None else int(row[2])
        return (int(row[0]), int(row[1]), message_id)

    async def set_batch_tally_message_id(
        self, chat_id: str, message_id: int
    ) -> None:
        """Upsert `tally_message_id` for a chat (creates row if missing)."""
        connection = await self._engine.get_connection()
        await connection.execute(
            """
            INSERT INTO telegram_batch_tally (
                chat_id, plus_count, expired_count, tally_message_id,
                updated_at
            ) VALUES (
                ?, 0, 0, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now')
            )
            ON CONFLICT(chat_id) DO UPDATE SET
                tally_message_id = excluded.tally_message_id,
                updated_at = excluded.updated_at
            """,
            (chat_id, message_id),
        )
        await connection.commit()

    async def list_batch_tally(self) -> list[BatchTallyRow]:
        """Return every batch-tally row (for the period-close loop)."""
        connection = await self._engine.get_connection()
        cursor = await connection.execute(
            """
            SELECT chat_id, plus_count, expired_count,
                   tally_message_id, updated_at
            FROM telegram_batch_tally
            ORDER BY chat_id ASC
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            BatchTallyRow(
                chat_id=r[0],
                plus_count=int(r[1]),
                expired_count=int(r[2]),
                tally_message_id=None if r[3] is None else int(r[3]),
                updated_at=r[4],
            )
            for r in rows
        ]

    async def close_batch_period(
        self,
        chat_id: str,
        *,
        plus_receipted: int,
        expired_receipted: int,
    ) -> None:
        """Decrement one chat by the receipted amounts and null message id.

        Uses exact receipted deltas (not absolute zero) so a Plus that
        landed after the snapshot survives into the next period. Only the
        named chat is touched.
        """
        connection = await self._engine.get_connection()
        # Clamp at 0 so concurrent period-close never drives counters negative.
        await connection.execute(
            """
            UPDATE telegram_batch_tally
            SET plus_count = MAX(0, plus_count - ?),
                expired_count = MAX(0, expired_count - ?),
                tally_message_id = NULL,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE chat_id = ?
            """,
            (plus_receipted, expired_receipted, chat_id),
        )
        await connection.commit()

    async def defer_plan_outcome_rearm(self, job_id: str) -> None:
        """Mark a counted job for next-period re-arm (`1` → `2` only)."""
        await self.defer_plan_outcome_rearm_many([job_id])

    async def defer_plan_outcome_rearm_many(
        self, job_ids: list[str] | tuple[str, ...]
    ) -> None:
        """Mark counted jobs for next-period re-arm (`1` → `2` only).

        Single commit for the whole id list. Never-counted (`0`) and
        already-deferred (`2`) rows are left unchanged so a rerun of a
        counted account cannot recount in the current period, while a
        never-counted job remains claimable. Empty input is a no-op.
        """
        if not job_ids:
            return
        connection = await self._engine.get_connection()
        placeholders = ",".join("?" * len(job_ids))
        await connection.execute(
            f"""
            UPDATE jobs
            SET plan_outcome_notified = 2
            WHERE plan_outcome_notified = 1
              AND job_id IN ({placeholders})
            """,
            tuple(job_ids),
        )
        await connection.commit()

    async def rearm_deferred_plan_outcomes(self) -> None:
        """Re-arm all deferred outcomes (`2` → `0`) for the next period."""
        connection = await self._engine.get_connection()
        await connection.execute(
            """
            UPDATE jobs
            SET plan_outcome_notified = 0
            WHERE plan_outcome_notified = 2
            """
        )
        await connection.commit()
