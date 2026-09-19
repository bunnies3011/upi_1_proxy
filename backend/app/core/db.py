"""DB engine — connection factory + schema init cho SQLite Settings Store.

Thuộc `core/` (Payment_Module_Boundary — Requirement 13): module này KHÔNG
được import bất kỳ gì từ `app.payments.*`. Đây là hạ tầng dùng chung, không
biết gì về payment method cụ thể (ideal, ...).

Requirements: 11.1 (đọc cấu hình từ SQLite tại startup, không dùng file
JSON/YAML riêng), 11.6 (persist qua restart — schema `settings` phải tồn tại
ngay khi engine khởi tạo lần đầu và giữ nguyên dữ liệu ở các lần mở lại sau).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite

_CREATE_SETTINGS_TABLE = """
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""

_CREATE_SETTINGS_KEY_INDEX = """
CREATE INDEX IF NOT EXISTS idx_settings_key ON settings(key);
"""

# Bảng `jobs` — persist state của toàn bộ job qua restart Backend_Service.
# Yêu cầu nghiệp vụ: tắt web/backend rồi bật lại phải nhớ toàn bộ job (bao
# gồm error/success đã hoàn tất, pending chưa chạy). Không dùng file JSON/
# YAML riêng để tránh 2 nguồn state (SQLite đã có sẵn qua DbEngine).
#
# Field mapping với `_JobRecord` trong `job_manager.py`:
#   - job_id, payment_method, account_line ← Job dataclass
#   - status ← JobStatus.value (str)
#   - order_num ← _JobRecord.order (INTEGER, dùng làm key sort)
#   - created_at ← Job.created_at (epoch seconds float)
#   - updated_at ← _JobRecord.updated_at
#   - dedup_key ← _JobRecord.dedup_key (nullable — handler không hỗ trợ
#     dedup thì NULL)
#   - artifact_path, error_code, error_message, payment_link ←
#     _JobRecord fields (nullable, populate khi terminal)
#   - settings_snapshot_json ← json.dumps(_JobRecord.settings_snapshot)
#     (cần cho rerun consistency — mỗi job giữ snapshot Settings tại thời
#     điểm submit_batch, không phụ thuộc Settings hiện tại)
#
# KHÔNG persist: `lease` (proxy lease chỉ có ý nghĩa trong runtime), `logs`
# (log realtime nhiều, DB write mỗi log = slow — chấp nhận mất log qua
# restart, artifact PNG vẫn còn trên disk), `handler_task`, `cancellation_token`.
#
# `order_num` UNIQUE để index nhanh khi sort — không dùng PRIMARY KEY vì
# job_id (UUID hex) là identity chính, order_num chỉ là ordering key.
_CREATE_JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    payment_method TEXT NOT NULL,
    account_line TEXT NOT NULL,
    status TEXT NOT NULL,
    order_num INTEGER NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    dedup_key TEXT,
    artifact_path TEXT,
    error_code TEXT,
    error_message TEXT,
    payment_link TEXT,
    settings_snapshot_json TEXT NOT NULL DEFAULT '{}',
    retry_count INTEGER NOT NULL DEFAULT 0,
    started_at REAL,
    finished_at REAL,
    telegram_notifications_json TEXT NOT NULL DEFAULT '[]',
    held INTEGER NOT NULL DEFAULT 0
);
"""

# Migration cột `retry_count` — thêm cho DB đã tồn tại trước khi feature
# auto-retry được ship. SQLite không hỗ trợ `ADD COLUMN IF NOT EXISTS`
# nên phải check `PRAGMA table_info` rồi mới execute — idempotent, safe
# gọi lại mỗi lần startup.
_ADD_JOBS_RETRY_COUNT_COLUMN = (
    "ALTER TABLE jobs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"
)

# Migration 2 cột thời gian chạy job — thêm cho DB đã tồn tại trước khi
# feature "elapsed time = finished - started" được ship. Nullable vì:
#   - Job PENDING chưa bao giờ chuyển RUNNING → `started_at IS NULL`.
#   - Job RUNNING đang chạy → `finished_at IS NULL`.
# Cùng cơ chế idempotent check PRAGMA như `retry_count`.
_ADD_JOBS_STARTED_AT_COLUMN = "ALTER TABLE jobs ADD COLUMN started_at REAL"
_ADD_JOBS_FINISHED_AT_COLUMN = "ALTER TABLE jobs ADD COLUMN finished_at REAL"

# Migration cột `telegram_notifications_json` — thêm cho DB đã tồn tại
# trước feature "track Telegram notification per job". JSON array chuỗi
# rỗng `[]` là default để row cũ đọc ra là list rỗng, KHÔNG null (schemas
# API dùng Field(default_factory=list) — cùng semantic).
#
# Shape entry: `{"chat_id": str, "chat_label": str, "sent_at": float,
# "success": bool, "error": dict | null}`. Không dùng bảng con vì:
#   1. Truy vấn luôn theo job_id (không có usecase "list mọi notification
#      cross-job") → JSON column đủ.
#   2. Số entry mỗi job nhỏ (1-N, thường 1) — không lo scan overhead.
#   3. Read/write cùng lúc với các field khác trong `_persist_record` →
#      giữ atomic 1 upsert.
_ADD_JOBS_TELEGRAM_NOTIFICATIONS_COLUMN = (
    "ALTER TABLE jobs ADD COLUMN telegram_notifications_json "
    "TEXT NOT NULL DEFAULT '[]'"
)

# Migration 6 cột Pull_Mode — thêm cho DB đã tồn tại trước khi feature
# "telegram-pull-job-mode" được ship (Requirement 21.1). NULL cho job
# Push_Mode (không có Job_Pull_Assignment) — chỉ job tạo khi
# `telegram.mode == "pull"` mới populate các cột này.
# Cùng cơ chế idempotent check PRAGMA như các migration trên.
_ADD_JOBS_PULL_ASSIGNMENT_STATE_COLUMN = (
    "ALTER TABLE jobs ADD COLUMN pull_assignment_state TEXT NULL"
)
_ADD_JOBS_PULL_ASSIGNED_TELEGRAM_USER_ID_COLUMN = (
    "ALTER TABLE jobs ADD COLUMN pull_assigned_telegram_user_id TEXT NULL"
)
_ADD_JOBS_PULL_ORIGIN_CHAT_ID_COLUMN = (
    "ALTER TABLE jobs ADD COLUMN pull_origin_chat_id TEXT NULL"
)
_ADD_JOBS_PULL_OUTCOME_COLUMN = (
    "ALTER TABLE jobs ADD COLUMN pull_outcome TEXT NULL"
)
_ADD_JOBS_PULL_ASSIGNED_USERNAME_COLUMN = (
    "ALTER TABLE jobs ADD COLUMN pull_assigned_username TEXT NULL"
)
_ADD_JOBS_PULL_ASSIGNED_FIRST_NAME_COLUMN = (
    "ALTER TABLE jobs ADD COLUMN pull_assigned_first_name TEXT NULL"
)

# Migration 4 cột Plus_Verify — định nghĩa gốc ở spec
# `telegram-callback-verify-plus` (state machine 2-lần-thử
# "armed→checking→verified/exhausted_2"), thêm cùng migration này vì
# `_JobRecord` của feature `telegram-pull-job-mode` dùng chung field.
# `plus_check_state`/`plus_check_attempts` NOT NULL vì mọi job (Push và
# Pull) đều đi qua state machine này; `telegram_message_chat_id`/
# `telegram_message_id` nullable vì chỉ job đã gửi message QR mới có.
_ADD_JOBS_PLUS_CHECK_STATE_COLUMN = (
    "ALTER TABLE jobs ADD COLUMN plus_check_state "
    "TEXT NOT NULL DEFAULT 'armed'"
)
_ADD_JOBS_PLUS_CHECK_ATTEMPTS_COLUMN = (
    "ALTER TABLE jobs ADD COLUMN plus_check_attempts "
    "INTEGER NOT NULL DEFAULT 0"
)
_ADD_JOBS_TELEGRAM_MESSAGE_CHAT_ID_COLUMN = (
    "ALTER TABLE jobs ADD COLUMN telegram_message_chat_id TEXT NULL"
)
_ADD_JOBS_TELEGRAM_MESSAGE_ID_COLUMN = (
    "ALTER TABLE jobs ADD COLUMN telegram_message_id INTEGER NULL"
)

# Migration cột `held` — cờ boolean song song với `status='pending'`:
#   - held=0 (mặc định) → job trong `_pending_order`, scheduler nhặt chạy.
#   - held=1 → job đã tạo NHƯNG chưa vào queue, chờ user bấm Start.
# Feature Add / Run tách bạch (Nút "+ Add N" trong UI submit textarea).
# Backward-compat: row cũ auto held=0 → hành vi hiện tại 100%.
_ADD_JOBS_HELD_COLUMN = (
    "ALTER TABLE jobs ADD COLUMN held INTEGER NOT NULL DEFAULT 0"
)

# Migration cột `plan` — kết quả check-plan cuối cùng của tài khoản
# (nguồn: `IdealFlowHandler.check_plan_status`). Nullable TEXT:
#   - NULL (mặc định) → chưa từng check hoặc job đang chạy dở, FE hiển
#     thị "?" trong badge plan.
#   - "plus" → tài khoản đã lên ChatGPT Plus (đủ điều kiện coi là
#     Successful account). Persist qua restart để reload trang giữ
#     nguyên panel Successful accounts.
#   - "free" → tài khoản còn Free (chưa nạp thành công). Persist để
#     UI hiển thị badge FREE ngay sau reload; nút "Check Plus All"
#     VẪN check lại các row `free` (vì free có thể lên plus).
# Chỉ "plus" được dùng để SKIP trong `check_plan_all_bulk` — theo yêu
# cầu 2026-07: "check plus all bỏ qua tài khoản plus luôn". Rerun /
# resubmit reset về NULL để state machine chạy lại từ đầu.
# Backward-compat: row cũ trước migration đọc ra NULL → treat như chưa
# check → hành vi cũ 100%.
_ADD_JOBS_PLAN_COLUMN = "ALTER TABLE jobs ADD COLUMN plan TEXT NULL"

# Migration cột `plan_outcome_notified` — durable dedup cho free-export
# batch tally (thay dict in-memory `_outcome_notified_jobs`). Tri-state:
#   - 0 = countable (chưa đếm trong kỳ hiện tại)
#   - 1 = counted this period
#   - 2 = counted-and-rerun (re-arm về 0 khi close kỳ)
# KHÔNG thread qua JobRow/upsert/list_all — chỉ các query claim/defer/
# rearm chạm cột này. INSERT mới lấy DEFAULT 0; ON CONFLICT upsert
# không liệt kê cột → giá trị cũ được giữ nguyên khi re-persist job.
_ADD_JOBS_PLAN_OUTCOME_NOTIFIED_COLUMN = (
    "ALTER TABLE jobs ADD COLUMN plan_outcome_notified "
    "INTEGER NOT NULL DEFAULT 0"
)

_CREATE_JOBS_ORDER_INDEX = """
CREATE INDEX IF NOT EXISTS idx_jobs_order ON jobs(order_num);
"""

# Composite index cho lookup dedup_key theo payment_method (mỗi payment
# method có namespace dedup riêng — cùng email trong iDEAL và một
# payment method khác trong tương lai là 2 job độc lập).
_CREATE_JOBS_DEDUP_INDEX = """
CREATE INDEX IF NOT EXISTS idx_jobs_dedup ON jobs(payment_method, dedup_key);
"""

# Bảng mới `telegram_worker_stats` (Requirement 15.1) — nguồn duy nhất
# cho tính công/lương Telegram_Worker ở Pull_Mode. Khoá bởi
# `telegram_user_id` (PRIMARY KEY, không dùng AUTOINCREMENT riêng vì
# quan hệ 1-1 tự nhiên với Telegram_Worker). `success_count`/
# `fail_count` tăng atomic qua `ON CONFLICT DO UPDATE` (job_repo.py),
# KHÔNG đọc-sửa-ghi để tránh race condition giữa nhiều callback xử lý
# song song.
_CREATE_TELEGRAM_WORKER_STATS_TABLE = """
CREATE TABLE IF NOT EXISTS telegram_worker_stats (
    telegram_user_id TEXT PRIMARY KEY,
    username TEXT NULL,
    first_name TEXT NULL,
    success_count INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""

# Bảng `telegram_batch_tally` — per-chat counters + id message tally
# cho free-export Plus/expired batch. `plus_count`/`expired_count` tăng
# atomic qua `ON CONFLICT DO UPDATE` (job_repo), KHÔNG đọc-sửa-ghi.
# `tally_message_id` nullable — null khi chưa gửi / sau khi close kỳ.
_CREATE_TELEGRAM_BATCH_TALLY_TABLE = """
CREATE TABLE IF NOT EXISTS telegram_batch_tally (
    chat_id TEXT PRIMARY KEY,
    plus_count INTEGER NOT NULL DEFAULT 0,
    expired_count INTEGER NOT NULL DEFAULT 0,
    tally_message_id INTEGER NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


class DbEngine:
    """Connection factory cho SQLite dùng chung của toàn Backend_Service.

    Dùng 1 `aiosqlite.Connection` shared được mở lười (lazy) và tái sử dụng,
    bảo vệ bằng `asyncio.Lock` để tránh 2 coroutine cùng mở connection song
    song (aiosqlite tự serialize lệnh SQL qua thread nội bộ, nhưng việc
    mở/khởi tạo connection lần đầu vẫn cần khoá để không bị tạo 2 lần).

    Ngoài ra có 1 write connection riêng cho claim+increment tally — transaction
    claim không bị `commit`/`rollback` của caller khác trên shared connection
    xen vào (hoặc rollback nhầm). Truy cập connection đó được serialize bằng
    `claim_lock`.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        # Connection + lock lazy-create riêng cho claim+increment atomic.
        self._write_connection: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        # Serialize mọi coroutine claim trên write connection (1 txn tại 1 thời điểm).
        self._claim_lock = asyncio.Lock()

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def claim_lock(self) -> asyncio.Lock:
        """Khoá serialize path claim+increment trên write connection."""
        return self._claim_lock

    @staticmethod
    async def _apply_connection_pragmas(connection: aiosqlite.Connection) -> None:
        """Áp PRAGMA dùng chung cho mọi connection của engine.

        `busy_timeout` — quan trọng: default của SQLite là 0, nghĩa
        là mọi contention lock (writer khác đang giữ) sẽ raise
        `OperationalError: database is locked` NGAY LẬP TỨC thay vì
        chờ. Trong practice, contention xảy ra khi:
          - CLI chạy song song với Backend_Service cùng DB file.
          - Tool bên ngoài (DB Browser for SQLite) mở DB.
          - Nhiều process test/smoke cùng chạm 1 DB.
          - Hai connection trong cùng process (shared + write claim).
        Bên trong 1 process + 1 connection, aiosqlite đã serialize qua
        thread nội bộ — nhưng process khác / connection khác thì có.
        Set 5000ms để writer chờ đủ lâu cho phần lớn tranh chấp thực tế
        (upsert job hoàn tất trong ~ms) mà không kéo dài request HTTP
        vô hạn.

        `synchronous=NORMAL` — an toàn trong WAL mode (SQLite doc
        official recommendation), giảm số `fsync` mỗi commit so với
        `FULL` default. Ít fsync = writer lock giữ ngắn hơn = ít
        tranh chấp với process khác. Trade-off: mất tối đa 1 commit
        cuối nếu OS crash giữa fsync WAL và fsync checkpoint —
        ACCEPTABLE cho state job (job manager tự re-persist mỗi
        transition; mất 1 event cuối không phá restart semantic).
        """
        await connection.execute("PRAGMA journal_mode=WAL;")
        await connection.execute("PRAGMA foreign_keys=ON;")
        await connection.execute("PRAGMA busy_timeout=5000;")
        await connection.execute("PRAGMA synchronous=NORMAL;")
        await connection.commit()

    async def get_connection(self) -> aiosqlite.Connection:
        """Trả về connection dùng chung, tạo mới (kèm PRAGMA) nếu chưa có.

        Đảm bảo parent directory của `db_path` tồn tại trước khi connect —
        `runtime/` là thư mục dữ liệu runtime của Backend_Service, có thể
        chưa được tạo khi engine khởi tạo lần đầu (Requirement 11.1, 11.6).
        """
        if self._connection is not None:
            return self._connection

        async with self._lock:
            if self._connection is not None:
                return self._connection

            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = await aiosqlite.connect(self._db_path)
            await self._apply_connection_pragmas(connection)
            self._connection = connection
            return connection

    async def get_write_connection(self) -> aiosqlite.Connection:
        """Trả về write connection riêng cho claim+increment tally (lazy).

        Connection này CHỈ phục vụ `try_claim_and_increment_tally` — caller
        phải giữ `claim_lock` khi dùng để không có 2 transaction claim
        chồng lên nhau trên cùng connection.
        """
        if self._write_connection is not None:
            return self._write_connection

        async with self._write_lock:
            if self._write_connection is not None:
                return self._write_connection

            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = await aiosqlite.connect(self._db_path)
            await self._apply_connection_pragmas(connection)
            self._write_connection = connection
            return connection

    async def init_schema(self) -> None:
        """Tạo bảng `settings` + `jobs` + `telegram_worker_stats` +
        `telegram_batch_tally` với index (idempotent).

        Chạy migration bổ sung (nếu bảng `jobs` đã tồn tại trước khi thêm
        cột `retry_count`, ...) — check PRAGMA rồi ALTER, KHÔNG try/except
        mù vì cần phân biệt "cột đã có" với các lỗi SQLite khác (Requirement
        21.5 — fail-fast, không swallow lỗi migration không thuộc loại
        "đã tồn tại"). Mỗi cột/bảng được check TỒN TẠI ĐỘC LẬP (Requirement
        21.7) — không giả định "tất cả hoặc không gì", an toàn để retry
        nếu 1 lần chạy trước bị crash giữa đường.
        """
        connection = await self.get_connection()
        await connection.execute(_CREATE_SETTINGS_TABLE)
        await connection.execute(_CREATE_SETTINGS_KEY_INDEX)
        await connection.execute(_CREATE_JOBS_TABLE)
        await connection.execute(_CREATE_JOBS_ORDER_INDEX)
        await connection.execute(_CREATE_JOBS_DEDUP_INDEX)
        await connection.execute(_CREATE_TELEGRAM_WORKER_STATS_TABLE)
        await connection.execute(_CREATE_TELEGRAM_BATCH_TALLY_TABLE)

        # Migration idempotent: check cột đã có chưa trước khi ALTER TABLE
        # (SQLite không hỗ trợ `ADD COLUMN IF NOT EXISTS`).
        cursor = await connection.execute("PRAGMA table_info(jobs);")
        columns = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        if "retry_count" not in columns:
            await connection.execute(_ADD_JOBS_RETRY_COUNT_COLUMN)
        if "started_at" not in columns:
            await connection.execute(_ADD_JOBS_STARTED_AT_COLUMN)
        if "finished_at" not in columns:
            await connection.execute(_ADD_JOBS_FINISHED_AT_COLUMN)
        if "telegram_notifications_json" not in columns:
            await connection.execute(_ADD_JOBS_TELEGRAM_NOTIFICATIONS_COLUMN)
        if "pull_assignment_state" not in columns:
            await connection.execute(_ADD_JOBS_PULL_ASSIGNMENT_STATE_COLUMN)
        if "pull_assigned_telegram_user_id" not in columns:
            await connection.execute(
                _ADD_JOBS_PULL_ASSIGNED_TELEGRAM_USER_ID_COLUMN
            )
        if "pull_origin_chat_id" not in columns:
            await connection.execute(_ADD_JOBS_PULL_ORIGIN_CHAT_ID_COLUMN)
        if "pull_outcome" not in columns:
            await connection.execute(_ADD_JOBS_PULL_OUTCOME_COLUMN)
        if "pull_assigned_username" not in columns:
            await connection.execute(_ADD_JOBS_PULL_ASSIGNED_USERNAME_COLUMN)
        if "pull_assigned_first_name" not in columns:
            await connection.execute(
                _ADD_JOBS_PULL_ASSIGNED_FIRST_NAME_COLUMN
            )
        if "plus_check_state" not in columns:
            await connection.execute(_ADD_JOBS_PLUS_CHECK_STATE_COLUMN)
        if "plus_check_attempts" not in columns:
            await connection.execute(_ADD_JOBS_PLUS_CHECK_ATTEMPTS_COLUMN)
        if "telegram_message_chat_id" not in columns:
            await connection.execute(_ADD_JOBS_TELEGRAM_MESSAGE_CHAT_ID_COLUMN)
        if "telegram_message_id" not in columns:
            await connection.execute(_ADD_JOBS_TELEGRAM_MESSAGE_ID_COLUMN)
        if "held" not in columns:
            await connection.execute(_ADD_JOBS_HELD_COLUMN)
        if "plan" not in columns:
            await connection.execute(_ADD_JOBS_PLAN_COLUMN)
        if "plan_outcome_notified" not in columns:
            await connection.execute(_ADD_JOBS_PLAN_OUTCOME_NOTIFIED_COLUMN)

        await connection.commit()

    async def close(self) -> None:
        """Đóng shared + write connection nếu đang mở (idempotent)."""
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
        if self._write_connection is not None:
            await self._write_connection.close()
            self._write_connection = None
