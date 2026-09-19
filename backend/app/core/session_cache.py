"""AccountSessionCache — cache cookie/session theo account (Requirement 10).

Thuộc `core/` (Payment_Module_Boundary — Requirement 13.6): module này KHÔNG
import bất kỳ gì từ `app.payments.*`. Logic revalidate HTTP cụ thể
(`GET /api/auth/session` theo Requirement 10.4) thuộc
`payments/ideal/chatgpt_client.py` — module này CHỈ biết TTL + lưu/đọc file,
KHÔNG biết cách revalidate của từng payment method; caller phía payment sẽ
gọi `clear(account_key)` khi revalidate fail.

Tên file cache = `sha256(account_key)[:32].json` — KHÔNG dùng `account_key`
thô làm tên file (defensive redaction ở tầng `core/`: dù caller đã hash
sẵn hay chưa, tầng này vẫn hash lại để không bao giờ lộ định danh thô qua
tên file trên đĩa).

Sensitive_Data_Redaction (Requirement 1.9, 3.6, 4.9, 14.8): payload thô chứa
cookie/access_token — module này CHỈ log `account_key` + trạng thái
(hit/miss/expired/write_error), KHÔNG log giá trị payload.

Fail_Fast_Policy — lỗi I/O trong `save()` KHÔNG raise (chỉ log warning) —
đây là ngoại lệ DUY NHẤT của Fail_Fast_Policy trong toàn hệ thống, theo
Requirement 10.7 (job vẫn đăng nhập bình thường, chỉ mất khả năng reuse
cache ở lần chạy sau).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.settings_store import SettingsRepository

logger = logging.getLogger(__name__)

_DEFAULT_ENABLED = True
_DEFAULT_TTL_HOURS = 24
#: Default "skip revalidate" window = 6h (1/4 TTL default). Session ChatGPT
#: thực tế bền 24h+, và bug rotating-proxy + CF challenge khiến revalidate
#: bị 403 tần suất cao → cắt luôn revalidate cho 6h đầu là kinh tế nhất.
#: Nếu cache thật sự invalid trong 6h này, `create_checkout` sẽ fail sớm
#: (401) và flow xử lý qua path lỗi domain bình thường. Set `0` qua UI để
#: tắt hoàn toàn (luôn revalidate — behaviour cũ, dùng khi debug).
_DEFAULT_SKIP_REVALIDATE_IF_FRESH_HOURS = 6
_SECONDS_PER_HOUR = 3600
_FILENAME_HASH_LENGTH = 32  # đủ dài để tránh collision thực tế, gọn hơn hex full 64.


@dataclass(frozen=True)
class CachedSession:
    """1 bản ghi session đã cache cho 1 account.

    Attributes:
        account_key: Định danh account gốc do caller truyền vào (tầng `core/`
            KHÔNG tự sinh — payment module tự quyết cách tạo). Field này
            chỉ được giữ trong-memory để trả về cho caller sau `get()`;
            trên đĩa, filename dùng sha256 hash của giá trị này.
        payload: Nội dung session generic (cookie, access_token...) — do
            payment module tự định nghĩa cấu trúc, `core/` không biết ý
            nghĩa nghiệp vụ bên trong.
        saved_at: Epoch timestamp (giây, unix time) tại thời điểm `save`
            được gọi thành công.
    """

    account_key: str
    payload: dict
    saved_at: float


class AccountSessionCache:
    """Cache session theo account, dùng chung cho mọi payment method (R10).

    Cấu hình (`session_cache.enabled`, `session_cache.ttl_hours`) được
    hydrate qua `apply_settings()` gọi tại startup — theo pattern Hydration
    dùng chung trong toàn hệ thống (`ProxyPool.apply_settings` cùng shape).

    Các method I/O (`get`/`save`/`clear`/`clear_all`) là `async` để giữ
    interface đồng nhất với phần còn lại của Backend_Service (aiosqlite,
    curl_cffi.AsyncSession...) — I/O filesystem thực sự bên trong vẫn là
    sync (mọi call là local disk, latency ~µs), không cần offload thread
    pool.
    """

    def __init__(self, settings: "SettingsRepository", cache_dir: Path) -> None:
        """Khởi tạo cache.

        Args:
            settings: Reference đến `SettingsRepository` — giữ để có thể mở
                rộng trong tương lai (ví dụ tự bulk_get khi reload). Hiện
                tại hydrate qua `apply_settings(snapshot)` từ ngoài như
                pattern `ProxyPool` (Requirement 9.1 style).
            cache_dir: Thư mục lưu file JSON cache (được inject từ `main.py`,
                thực tế là `backend/runtime/session_cache/`). KHÔNG hardcode
                path trong module này.
        """
        self._settings = settings
        self._cache_dir = cache_dir
        self._enabled: bool = _DEFAULT_ENABLED
        self._ttl_hours: int = _DEFAULT_TTL_HOURS
        self._skip_revalidate_if_fresh_hours: int = (
            _DEFAULT_SKIP_REVALIDATE_IF_FRESH_HOURS
        )

        # Tạo cache_dir best-effort ngay ở init. Mọi lỗi I/O cụ thể sau này
        # trong save() được xử lý theo R10.7 (chỉ log warning, không raise).
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "AccountSessionCache: không tạo được cache_dir=%s (%s); "
                "save() sau đó sẽ log warning theo R10.7",
                self._cache_dir,
                exc,
            )

    def apply_settings(self, snapshot: dict) -> None:
        """Hydrate `session_cache.*` từ `snapshot`.

        `snapshot` là dict đã được `settings_repo.bulk_get([...])` từ ngoài
        (`main.py` startup hook hoặc reload hook). Nếu key thiếu (ví dụ DB
        chưa từng ghi giá trị nào), dùng default trong whitelist Requirement
        11.7: `enabled=True`, `ttl_hours=24`,
        `skip_revalidate_if_fresh_hours=0` (backward-compat: giữ hành vi
        luôn revalidate cho môi trường chưa migrate).

        Method này là sync — chỉ ghi vào field in-memory, không đụng DB.
        """
        enabled = snapshot.get("session_cache.enabled")
        ttl_hours = snapshot.get("session_cache.ttl_hours")
        skip_revalidate = snapshot.get("session_cache.skip_revalidate_if_fresh_hours")
        self._enabled = enabled if enabled is not None else _DEFAULT_ENABLED
        self._ttl_hours = ttl_hours if ttl_hours is not None else _DEFAULT_TTL_HOURS
        self._skip_revalidate_if_fresh_hours = (
            skip_revalidate
            if skip_revalidate is not None
            else _DEFAULT_SKIP_REVALIDATE_IF_FRESH_HOURS
        )

    @property
    def skip_revalidate_if_fresh_hours(self) -> int:
        """Ngưỡng "cache còn trẻ" để skip revalidate — đọc cho caller layer flow.

        Trả giá trị đã hydrate (0 = luôn revalidate). Property (read-only)
        thay vì để caller đọc trực tiếp field `_skip_...` để giữ boundary
        rõ ràng: caller chỉ đọc, không được ghi.
        """
        return self._skip_revalidate_if_fresh_hours

    # ---- Filename helpers ------------------------------------------------

    def _filename_for(self, account_key: str) -> str:
        """Sinh tên file cache cho `account_key` bằng sha256[:32].

        Defensive redaction ở tầng `core/`: KHÔNG dùng `account_key` thô
        làm tên file (tránh lộ PII qua tên file trên đĩa) — dù caller
        (`payments/ideal/chatgpt_client.py`) đã hash sẵn, tầng này vẫn hash
        lại một lớp nữa để không phụ thuộc contract từ tầng ngoài.
        """
        digest = hashlib.sha256(account_key.encode("utf-8")).hexdigest()
        return f"{digest[:_FILENAME_HASH_LENGTH]}.json"

    def _cache_file_path(self, account_key: str) -> Path:
        """Build đường dẫn file cache tuyệt đối cho `account_key`."""
        return self._cache_dir / self._filename_for(account_key)

    # ---- Public async API -----------------------------------------------

    async def get(self, account_key: str) -> CachedSession | None:
        """Đọc bản ghi cache còn hợp lệ của `account_key`.

        Trả `None` nếu:
        - `session_cache.enabled=False` (R10.6);
        - file cache không tồn tại;
        - file corrupt / parse lỗi — coi như cache miss, log warning và xoá
          file để tự phục hồi (self-heal, không giữ file rác vô tận);
        - bản ghi đã vượt `session_cache.ttl_hours` tính từ `saved_at`
          (R10.3, R10.5) — trường hợp này cũng best-effort xoá file expired.
        """
        if not self._enabled:
            logger.debug("session_cache miss (disabled): account_key=%s", account_key)
            return None

        file_path = self._cache_file_path(account_key)
        if not file_path.exists():
            logger.debug("session_cache miss (no file): account_key=%s", account_key)
            return None

        try:
            raw = file_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            saved_at = float(data["saved_at"])
            payload = data["payload"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning(
                "session_cache miss (corrupt file, self-healing by delete): "
                "account_key=%s (%s)",
                account_key,
                exc,
            )
            self._best_effort_unlink(file_path, reason="corrupt")
            return None

        age_seconds = time.time() - saved_at
        if age_seconds > self._ttl_hours * _SECONDS_PER_HOUR:
            logger.debug(
                "session_cache expired: account_key=%s (age=%ds > ttl=%dh)",
                account_key,
                int(age_seconds),
                self._ttl_hours,
            )
            self._best_effort_unlink(file_path, reason="expired")
            return None

        logger.debug("session_cache hit: account_key=%s", account_key)
        return CachedSession(account_key=account_key, payload=payload, saved_at=saved_at)

    async def save(self, account_key: str, payload: dict) -> None:
        """Ghi/cập nhật bản ghi cache cho `account_key` (R10.5).

        - Nếu `session_cache.enabled=False` → no-op ngay (R10.6), không
          chạm filesystem.
        - Ghi atomic: `write_text` ra file `.tmp` rồi `replace()` sang tên
          cuối — tránh reader (`get`) đọc file half-written nếu process
          crash giữa chừng.
        - Lỗi I/O bất kỳ (`OSError`/`PermissionError`/...) CHỈ log warning,
          KHÔNG raise — R10.7, ngoại lệ DUY NHẤT của Fail_Fast_Policy.
        - Log message KHÔNG chứa `payload` thô (Sensitive_Data_Redaction —
          payload có cookie/access_token).
        """
        if not self._enabled:
            logger.debug(
                "session_cache save skipped (disabled): account_key=%s", account_key
            )
            return

        record: dict[str, Any] = {"payload": payload, "saved_at": time.time()}
        final_path = self._cache_file_path(account_key)
        tmp_path = final_path.with_suffix(final_path.suffix + ".tmp")

        try:
            # Đảm bảo cache_dir tồn tại (có thể bị user xoá runtime).
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(json.dumps(record), encoding="utf-8")
            tmp_path.replace(final_path)
            logger.debug("session_cache write ok: account_key=%s", account_key)
        except OSError as exc:
            # R10.7 — ngoại lệ Fail_Fast_Policy duy nhất: warning + swallow.
            logger.warning(
                "session_cache write_error: account_key=%s (%s); "
                "job tiếp tục, mất khả năng reuse ở lần chạy sau",
                account_key,
                exc,
            )
            # Dọn file .tmp nếu tồn tại (best-effort, không raise).
            self._best_effort_unlink(tmp_path, reason="tmp_cleanup")

    async def clear(self, account_key: str) -> None:
        """Xoá bản ghi cache của `account_key`, idempotent (R10.8).

        Không raise nếu file không tồn tại (`missing_ok=True`). I/O error
        khác (permission...) chỉ log warning để giữ semantics idempotent —
        endpoint HTTP gọi method này KHÔNG bao giờ nên fail vì lý do này.
        """
        file_path = self._cache_file_path(account_key)
        try:
            file_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "session_cache clear I/O error: account_key=%s (%s)",
                account_key,
                exc,
            )

    async def clear_all(self) -> None:
        """Xoá toàn bộ bản ghi cache của mọi account (R10.9).

        Best-effort: nếu 1 file xoá fail, log warning + tiếp tục xoá các
        file khác (không raise, không dừng giữa chừng). Nếu `cache_dir`
        không tồn tại → no-op (idempotent).
        """
        if not self._cache_dir.exists():
            return
        for file_path in self._cache_dir.glob("*.json"):
            try:
                file_path.unlink()
            except OSError as exc:
                logger.warning(
                    "session_cache clear_all: bỏ qua file=%s (%s), "
                    "tiếp tục xoá các file khác",
                    file_path.name,
                    exc,
                )

    # ---- Private helpers ------------------------------------------------

    def _best_effort_unlink(self, path: Path, *, reason: str) -> None:
        """Xoá file, nuốt lỗi I/O (chỉ log warning) — dùng cho self-heal path."""
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "session_cache: không xoá được file=%s reason=%s (%s)",
                path.name,
                reason,
                exc,
            )
