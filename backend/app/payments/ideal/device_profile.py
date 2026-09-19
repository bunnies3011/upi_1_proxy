"""DeviceProfileAllocator — cấp phát DeviceProfile cho từng IdealJob (R5.1-R5.3).

Payment_Module_Boundary (Requirement 13.1, 13.6): chỉ import
`app.core.settings_store` (interface Settings_Store) và `app.payments.ideal.models`
(dataclass `DeviceProfile` + hàm module-level `validate_device_profile`).
KHÔNG phụ thuộc ProxyPool/DB engine/HTTP client — allocator thuần logic
+ reference tới Settings_Store.

Vai trò theo Requirement 5:
    - R5.1: chọn 1 DeviceProfile từ danh sách `ideal.device_profiles`, cố
      định cho toàn bộ vòng đời 1 IdealJob — cache theo `job_id` in-memory.
    - R5.2: validate `language`/`timeZone` khớp locale NL tại thời điểm LƯU
      vào Settings_Store (proxy sang `validate_device_profile()` ở
      `models.py`), KHÔNG validate lại mỗi lần `pick_for_job()`.
    - R5.3: luân phiên giữa các IdealJob khác nhau bằng round-robin cursor
      `_pick_cursor: int` để tránh 2 job đồng thời chạy cùng fingerprint.

_Requirements: 5.1, 5.2, 5.3_
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

from app.core.settings_store import SettingsRepository
from app.payments.ideal.models import DeviceProfile, validate_device_profile

#: Settings_Store key chứa danh sách DeviceProfile ứng viên. Type constraint
#: `list_object` (mỗi phần tử là dict khớp shape DeviceProfile) — đăng ký
#: bởi `payments/ideal/__init__.py` qua `SettingsRepository.register_namespace`
#: (Requirement 11.7). Ở tầng allocator, key được dùng LIVE (không snapshot
#: tại constructor) để user cập nhật danh sách qua Settings API luôn có hiệu
#: lực cho job mới; job đã pick giữ nguyên profile trong cache theo R5.1.
_SETTINGS_KEY_DEVICE_PROFILES: Final[str] = "ideal.device_profiles"


class DeviceProfileAllocator:
    """Cấp phát DeviceProfile cho mỗi IdealJob (Requirement 5.1-5.3).

    Đọc danh sách ứng viên từ `ideal.device_profiles` (Settings_Store — đã
    qua `validate_profile()` khi user lưu), pick theo round-robin cursor
    (R5.3) và cache theo `job_id` (R5.1). Fail_Fast_Policy: danh sách rỗng
    hoặc chưa cấu hình → raise `ValueError` — KHÔNG silently fallback về
    hardcoded profile (vi phạm rule Fail-Fast của repo).
    """

    def __init__(self, settings: SettingsRepository) -> None:
        """Khởi tạo allocator với reference tới `SettingsRepository`.

        KHÔNG snapshot danh sách profile tại constructor — mỗi lần
        `pick_for_job()` gọi lại `settings.get()` để lấy danh sách mới nhất.
        Điều này để user cập nhật `ideal.device_profiles` qua API mà không
        cần restart Backend; job đã cache trước đó giữ nguyên profile cũ
        (R5.1), job mới sẽ pick từ danh sách vừa cập nhật.

        Args:
            settings: `SettingsRepository` instance duy nhất của Backend
                (được inject từ `bootstrap.py` / `main.py`).
        """
        self._settings = settings
        # job_id -> DeviceProfile đã pick (R5.1). KHÔNG tự bounded theo
        # thời gian — mỗi entry chỉ được giải phóng qua `forget(job_id)`,
        # được `JobManager` gọi qua `register_job_removed_hook` mỗi khi
        # job bị xoá khỏi `_jobs` (delete_job / cleanup TTL). Nếu thiếu
        # bước forget này, cache phình vô hạn theo tổng số job đã TỪNG
        # tồn tại trong suốt đời process (memory leak khi chạy hàng chục
        # nghìn account nhiều ngày liên tục) — xem `main.py` nơi hook
        # được đăng ký.
        self._cache: dict[str, DeviceProfile] = {}
        # Cursor round-robin cho các `job_id` KHÁC NHAU (R5.3). Cùng job_id
        # gọi lặp không tăng cursor vì bị cache chặn ở đầu hàm.
        self._pick_cursor: int = 0
        # Lock serialize race giữa nhiều async task đồng thời gọi
        # `pick_for_job` cho các `job_id` khác nhau — đảm bảo `_pick_cursor`
        # tăng nguyên tử và cùng `job_id` gọi song song vẫn cache đúng 1 lần.
        self._lock = asyncio.Lock()

    def validate_profile(self, profile: dict[str, Any]) -> None:
        """Validate 1 DeviceProfile candidate trước khi lưu vào Settings_Store.

        Thin wrapper cho tầng API layer / Settings write-through gọi (R5.2)
        — delegate sang `validate_device_profile()` module-level ở
        `models.py` để logic validate locale NL nằm cạnh dataclass
        `DeviceProfile`.

        Args:
            profile: Dict candidate người dùng gửi qua API (chưa nhất thiết
                là `DeviceProfile` đã dataclass hoá).

        Raises:
            ValueError: Nếu `language != "nl-NL"` hoặc
                `timeZone != "Europe/Amsterdam"` (Requirement 5.2). KHÔNG
                dùng `IdealFlowError` vì đây là lỗi VALIDATE SETTINGS tại
                boundary Settings_Store, không phải lỗi phát sinh trong 1
                IdealJob đang chạy (Requirement 14.1, 14.3).
        """
        validate_device_profile(profile)

    async def pick_for_job(self, job_id: str) -> DeviceProfile:
        """Trả về `DeviceProfile` cố định cho `job_id` trong vòng đời của job.

        Chiến lược:
            1. Nếu `job_id` đã có trong cache → trả về ngay (R5.1) — cùng
               job gọi nhiều lần trong `IdealFlowHandler.run()` luôn nhận
               cùng 1 profile.
            2. Đọc `ideal.device_profiles` từ Settings_Store. Danh sách
               rỗng/chưa cấu hình → raise `ValueError` (Fail_Fast_Policy —
               không có profile hợp lệ, KHÔNG fallback).
            3. Pick profile tại `_pick_cursor % len(profiles)`, tăng cursor
               sau khi pick — luân phiên round-robin giữa các job khác
               nhau (R5.3).
            4. Parse dict → `DeviceProfile` dataclass, cache theo `job_id`,
               trả về.

        Args:
            job_id: Định danh IdealJob. Cùng `job_id` gọi lần thứ N luôn
                trả về cùng `DeviceProfile` với lần 1.

        Returns:
            `DeviceProfile` đã pick, tồn tại trong danh sách
            `ideal.device_profiles` hiện tại (hoặc tại thời điểm pick lần
            đầu tiên cho `job_id` này).

        Raises:
            ValueError: Nếu Settings_Store key `ideal.device_profiles` rỗng
                hoặc chưa từng được set — Backend chưa được cấu hình đủ
                để chạy IdealJob (Requirement 5.1).
        """
        # Fast-path không cần lock: cache đọc-nhiều-ghi-ít, và Python dict
        # `.get()` là atomic ở tầng GIL — nếu hit thì trả ngay để tránh
        # serialize không cần thiết các job đã cache.
        cached = self._cache.get(job_id)
        if cached is not None:
            return cached

        async with self._lock:
            # Double-check: 1 task khác có thể đã fill cache cho `job_id`
            # trong lúc task hiện tại chờ acquire lock.
            cached = self._cache.get(job_id)
            if cached is not None:
                return cached

            raw_profiles = await self._settings.get(_SETTINGS_KEY_DEVICE_PROFILES)
            # `not raw_profiles` bao trùm cả `None` (key chưa từng set) và
            # `[]` (list rỗng đã set). Cả 2 đều là trạng thái không hợp lệ
            # theo R5.1 vì `initiate` bắt buộc phải có `deviceInfo`.
            if not raw_profiles:
                raise ValueError(
                    f"Settings key '{_SETTINGS_KEY_DEVICE_PROFILES}' rỗng — "
                    "cấu hình ít nhất 1 DeviceProfile hợp lệ (locale NL) "
                    "trước khi khởi chạy IdealJob (Requirement 5.1)."
                )

            index = self._pick_cursor % len(raw_profiles)
            self._pick_cursor += 1
            selected_raw = raw_profiles[index]

            # Explicit field mapping (không dùng `**selected_raw`) để:
            #   1. Fail_Fast rõ ràng với `KeyError` chỉ đích danh field
            #      thiếu nếu Settings_Store type constraint không kịp bắt.
            #   2. Bỏ qua các field lạ (nếu có) — không khớp DeviceProfile
            #      nhưng vô hại; dataclass frozen sẽ raise `TypeError` với
            #      `**dict` khi gặp field lạ, cứng nhắc không cần thiết ở
            #      tầng runtime.
            selected = DeviceProfile(
                language=selected_raw["language"],
                timeZone=selected_raw["timeZone"],
                screenWidth=selected_raw["screenWidth"],
                screenHeight=selected_raw["screenHeight"],
                screenAvailableWidth=selected_raw["screenAvailableWidth"],
                screenAvailableHeight=selected_raw["screenAvailableHeight"],
                colorDepth=selected_raw["colorDepth"],
            )
            self._cache[job_id] = selected
            return selected

    def forget(self, job_id: str) -> None:
        """Giải phóng entry cache của `job_id` (memory-leak fix).

        Gọi bởi `JobManager` mỗi khi 1 job bị xoá vĩnh viễn khỏi `_jobs`
        (`delete_job()` hoặc `_cleanup_old_terminal_jobs()`) — xem hook
        đăng ký ở `main.py` qua `job_manager.register_job_removed_hook`.

        Idempotent: `job_id` không có trong cache (chưa từng pick, hoặc
        đã `forget` trước đó) → no-op, không raise. An toàn gọi nhiều lần
        / với `job_id` lạ.
        """
        self._cache.pop(job_id, None)


__all__ = ["DeviceProfileAllocator"]
