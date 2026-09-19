"""Proxy Pool xoay vòng cho mỗi Job (Requirement 9).

Thuộc `core/` (Payment_Module_Boundary — Requirement 9.8, 13): module này
KHÔNG được import bất kỳ gì từ `app.payments.*`, để dùng lại được cho các
payment module khác trong tương lai (không chỉ iDEAL).

`ProxyPool` quản lý danh sách proxy (raw line hoặc template chứa placeholder
`{SID}`) lấy từ Settings_Store (`proxy.list`, `proxy.rotation_mode`,
`proxy.max_leases_per_proxy`, `proxy.dead_threshold`), cấp phát (`acquire`)
và trả lại (`release`) theo chiến lược round-robin hoặc least-used
lease-based, có khả năng `mark_dead`/`mark_alive` cho từng proxy.

`ProxyPool` KHÔNG tự query DB — `apply_settings(snapshot)` nhận snapshot dict
đã được truyền từ ngoài (ví dụ Job_Manager hoặc `main.py` startup hook gọi
`settings_repo.bulk_get(...)` rồi truyền vào), theo đúng pattern hydrate
"apply_settings" dùng chung trong toàn hệ thống.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.errors import ProxyExhaustedError
from app.core.proxy_health import ProbeConfig

if TYPE_CHECKING:
    from app.core.payment_flow import CancellationToken
    from app.core.settings_store import SettingsRepository

_DEFAULT_ROTATION_MODE = "round_robin"
# Off-the-shelf default: 10 lease/proxy — phù hợp pool nhỏ (~5 proxy) chạy
# concurrency ~50 job; user set qua UI khi cần siết chặt hơn.
_DEFAULT_MAX_LEASES_PER_PROXY = 10
_DEFAULT_DEAD_THRESHOLD = 3
# TTL "quarantine" cho proxy vừa bị đánh dấu dead — sau khoảng này proxy tự
# được revive (reset `dead=False`, `consecutive_errors=0`) trong lần `acquire`
# / `stats` tiếp theo mà KHÔNG cần user bấm nút nào. Chọn 120s vì:
#   - Đủ dài để "cho pool nghỉ" qua một đợt lỗi nhất thời (rate-limit tạm,
#     upstream reset, DNS flap) — thường tự khỏi trong 1–2 phút.
#   - Đủ ngắn để tránh trạng thái "cả pool dead vĩnh viễn" khi user không
#     nhìn UI → job không dùng được proxy cho tới khi có ai đó can thiệp.
# Set `0` để tắt TTL (dead = vĩnh viễn, hành vi cũ) — dành cho môi trường
# muốn kiểm soát bằng tay 100%.
_DEFAULT_DEAD_COOLDOWN_SECONDS: float = 120.0

# Khoảng thời gian giữa 2 lần retry `acquire` khi pool còn proxy alive
# nhưng tất cả đang bị lease. Chọn 0.5s làm compromise:
#   - Đủ ngắn để user cảm nhận không lag khi 1 slot vừa được release.
#   - Đủ dài để tránh spin loop CPU cho hàng chục job đang chờ đồng thời.
# Chỉ dùng khi caller gọi `acquire(..., wait_for_release=True)` — hành vi
# cũ (raise ngay khi hết proxy) vẫn là default để giữ compat với test
# hiện có và caller khác không cần semantic chờ.
_ACQUIRE_WAIT_POLL_SECONDS: float = 0.5


@dataclass(frozen=True)
class ProxyLease:
    """1 lượt 1 Job thuê 1 proxy từ `ProxyPool` (Requirement 9.3, 9.7).

    Attributes:
        proxy_id: Định danh proxy trong `proxy.list` — chính là chuỗi
            raw line/template gốc, dùng để `release`/`mark_dead`/`mark_alive`
            đúng proxy đã cấp.
        materialized_url: Chuỗi proxy đã materialize placeholder `{SID}`
            (nếu có) thành session id cụ thể cho lease này.
        leased_at: Timestamp (`time.time()`) lúc lease được cấp.
    """

    proxy_id: str
    materialized_url: str
    leased_at: float


@dataclass
class _ProxyState:
    """Trạng thái nội bộ của 1 proxy trong pool — KHÔNG public.

    Attributes:
        template: Raw line/template gốc từ `proxy.list`.
        lease_count: Số lease đang active trên proxy này.
        consecutive_errors: Đếm số lỗi liên tiếp (reset về 0 khi `mark_alive`
            hoặc khi auto-revive sau cooldown).
        dead: Cờ "đang bị quarantine". Chuyển True khi
            `consecutive_errors >= dead_threshold` (qua `mark_dead`) hoặc
            khi `force_dead`. Auto revive sau `dead_cooldown_seconds` — xem
            `ProxyPool._revive_expired`.
        dead_at: Timestamp (`time.time()`) tại thời điểm chuyển `dead=True`.
            `None` khi proxy đang alive. Dùng để tính TTL cooldown; khi
            `dead=True` mà `dead_at is None` → coi như dead vĩnh viễn
            (defensive, không xảy ra trong flow bình thường).
    """

    template: str
    lease_count: int = 0
    consecutive_errors: int = 0
    dead: bool = False
    dead_at: float | None = None


class ProxyPool:
    """Lease-based, `round_robin` | `least_used`. Direct_Mode khi `proxy.list` rỗng.

    Khởi tạo với trạng thái an toàn mặc định (pool rỗng => Direct_Mode) để
    `acquire()` vẫn hoạt động đúng trước khi `apply_settings()` được gọi lần
    đầu tại startup.
    """

    def __init__(self, settings: "SettingsRepository") -> None:
        self._settings = settings
        self._proxies: dict[str, _ProxyState] = {}
        self._rotation_mode: str = _DEFAULT_ROTATION_MODE
        self._max_leases_per_proxy: int = _DEFAULT_MAX_LEASES_PER_PROXY
        self._dead_threshold: int = _DEFAULT_DEAD_THRESHOLD
        self._dead_cooldown_seconds: float = _DEFAULT_DEAD_COOLDOWN_SECONDS
        self._round_robin_cursor: int = 0
        # Snapshot config probe — build từ `apply_settings`. Default là
        # `ProbeConfig()` (probe enabled, endpoint ipify). Consumer
        # (`job_manager`) đọc qua `pool.probe_config`.
        self._probe_config: ProbeConfig = ProbeConfig()
        # `acquire` là async — dùng `asyncio.Lock` để đảm bảo chọn proxy +
        # tăng `lease_count` là 1 thao tác nguyên tử ngay cả khi
        # Job_Manager có nhiều coroutine gọi `acquire` đồng thời
        # (concurrency ≥ 1). Các method sync (`release`/`mark_dead`/
        # `mark_alive`) không cần lock vì thao tác dict/int GIL-safe cho
        # single op trong CPython — tradeoff đơn giản có ý thức.
        self._lock: asyncio.Lock = asyncio.Lock()

    def apply_settings(self, snapshot: dict) -> None:
        """Hydrate `proxy.*` từ `snapshot` (Requirement 9.1).

        KHÔNG tự query DB — `snapshot` là dict đã được đọc từ ngoài (ví dụ
        `settings_repo.bulk_get([...])`). Rebuild danh sách proxy nội bộ:
        GIỮ nguyên trạng thái lease/dead/consecutive_errors của proxy đã tồn
        tại nếu proxy đó vẫn còn trong `proxy.list` mới (không reset toàn bộ
        vô cớ), chỉ thêm proxy mới / bỏ proxy không còn trong list.
        """
        proxy_list: list[str] = snapshot.get("proxy.list") or []
        self._rotation_mode = snapshot.get("proxy.rotation_mode") or _DEFAULT_ROTATION_MODE
        self._max_leases_per_proxy = (
            snapshot.get("proxy.max_leases_per_proxy") or _DEFAULT_MAX_LEASES_PER_PROXY
        )
        self._dead_threshold = snapshot.get("proxy.dead_threshold") or _DEFAULT_DEAD_THRESHOLD
        # `dead_cooldown_seconds` = 0 hợp lệ (tắt TTL, dead vĩnh viễn) →
        # KHÔNG dùng `or` (coi 0 là falsy) mà check `is None` để giữ nguyên
        # ý định "0 = disabled" của user.
        cooldown_raw = snapshot.get("proxy.dead_cooldown_seconds")
        self._dead_cooldown_seconds = (
            float(cooldown_raw) if cooldown_raw is not None else _DEFAULT_DEAD_COOLDOWN_SECONDS
        )
        # Rebuild probe config từ cùng snapshot — 1 lần apply_settings hydrate
        # tất cả knob liên quan proxy để tránh drift giữa 2 lần đọc DB.
        self._probe_config = ProbeConfig.from_snapshot(snapshot)

        # Pool lưu RAW LINE (không pre-normalize) — mọi transform (URL-encode
        # credential, prepend scheme, {SID} rotate) diễn ra ở `_materialize`
        # khi acquire. Cách này (pattern của gpt_signup_hybrid):
        #   - Dedupe theo chính xác chuỗi user nhập (tránh case 2 entry cùng
        #     host khác cách viết được coi là 1).
        #   - 1 template `host:port:user-{SID}:pass` → vô hạn sticky session
        #     mà chỉ tốn 1 entry pool.
        #   - Chỉ strip whitespace + bỏ dòng rỗng để pool không giữ phần tử vô
        #     nghĩa. Chấp nhận cả 5 format proxy phổ biến (xem `proxy_format`).
        rebuilt: dict[str, _ProxyState] = {}
        for raw in proxy_list:
            if not isinstance(raw, str):
                continue
            proxy_id = raw.strip()
            if not proxy_id:
                continue
            existing = self._proxies.get(proxy_id)
            rebuilt[proxy_id] = existing if existing is not None else _ProxyState(template=proxy_id)
        self._proxies = rebuilt
        self._round_robin_cursor = 0

    async def acquire(
        self,
        job_id: str,
        *,
        wait_for_release: bool = False,
        cancellation_token: "CancellationToken | None" = None,
    ) -> ProxyLease | None:
        """Cấp 1 `ProxyLease` cho `job_id`, hoặc `None` nếu Direct_Mode.

        - `proxy.list` rỗng → trả `None` ngay (Direct_Mode, Requirement 9.2)
          — KHÔNG raise, đây là hành vi hợp lệ.
        - Tất cả proxy đã bị `mark_dead`/`force_dead`:
            - Nếu `proxy.dead_cooldown_seconds > 0` (default 120s): proxy
              tự revive khi qua cooldown → nếu `wait_for_release=True`,
              caller sẽ chờ trong vòng poll cho tới lúc đó. Nếu
              `wait_for_release=False` (default), raise
              `ProxyExhaustedError` ngay theo semantic cũ.
            - Nếu cooldown = 0 hoặc mọi proxy dead mà `dead_at is None`
              (dead vĩnh viễn, khó xảy ra trong flow bình thường): raise
              `ProxyExhaustedError` ngay bất kể `wait_for_release`.
        - Còn ít nhất 1 proxy alive nhưng tất cả đang đạt `max_leases_per_proxy`:
          hành vi phụ thuộc `wait_for_release`.
            - `False` (mặc định, backward compat): raise `ProxyExhaustedError`
              ngay lập tức — semantic cũ.
            - `True`: **poll đợi** cho tới khi có slot được release (hoặc
              proxy dead sau khi thất bại đủ threshold, hoặc pool được
              apply_settings lại). Đây là cách Job_Manager gọi để tránh
              bug "concurrency > proxy count → 15/20 job fail ngay khi
              5 proxy đầu vừa bị lease". Job đợi trong vòng poll thay vì
              chết trẻ.
        - Có proxy khả dụng: chọn theo `proxy.rotation_mode`, tăng
          `lease_count`, materialize URL và trả `ProxyLease`.

        Toàn bộ thao tác "chọn proxy + tăng `lease_count` + xoay
        `_round_robin_cursor`" được bọc bằng `asyncio.Lock` để bảo đảm
        atomic khi Job_Manager gọi `acquire` đồng thời từ nhiều coroutine
        job (concurrency ≥ 1). Vòng poll `await asyncio.sleep(...)` chạy
        NGOÀI lock để `release()`/`mark_alive()`/`apply_settings()` không
        bị chặn khi caller khác đang chờ.

        Args:
            job_id: Định danh job đang xin lease — chỉ dùng cho log/debug
                phía caller, KHÔNG ảnh hưởng logic chọn proxy.
            wait_for_release: `True` để bật chế độ chờ khi hết proxy alive
                (xem semantic ở trên). Keyword-only để tránh nhầm với arg
                khác. Mặc định `False` để giữ compat với test hiện có và
                caller không cần semantic chờ.
            cancellation_token: Optional token để user hủy job đang chờ
                proxy (VD `DELETE /api/jobs/{id}`). Khi token cancel,
                acquire raise `asyncio.CancelledError` để `_run_handler`
                đưa job về `STOPPED` — không chờ vô hạn.

        Raises:
            ProxyExhaustedError: Khi `dead_count == total_proxies`, hoặc
                khi `wait_for_release=False` và pool không còn proxy khả
                dụng ngay lập tức.
            asyncio.CancelledError: Khi đang chờ trong vòng poll và
                `cancellation_token.is_cancelled()` trả `True`.
        """
        while True:
            async with self._lock:
                if not self._proxies:
                    return None

                # Lazy revive: proxy quá cooldown TTL tự "sống lại" NGAY tại
                # điểm được cần đến, không phụ thuộc background task. Chạy
                # trước pick để `_pick_*` thấy state chuẩn.
                self._revive_expired()

                if self._rotation_mode == "least_used":
                    proxy_id = self._pick_least_used()
                else:
                    proxy_id = self._pick_round_robin()

                if proxy_id is not None:
                    state = self._proxies[proxy_id]
                    state.lease_count += 1
                    # Pool lưu và trả RAW LINE (giữ nguyên user input, có
                    # thể chứa `{SID}`). Consumer (`payments/*/flow.py`)
                    # chịu trách nhiệm gọi `materialize_proxy(...)` trước
                    # khi feed cho httpx — đây là pattern boundary rõ
                    # ràng: pool = "dumb container", format handling = consumer.
                    return ProxyLease(
                        proxy_id=proxy_id,
                        materialized_url=state.template,
                        leased_at=time.time(),
                    )

                total_proxies = len(self._proxies)
                dead_count = sum(1 for state in self._proxies.values() if state.dead)
                leased_out_count = sum(
                    1
                    for state in self._proxies.values()
                    if not state.dead and state.lease_count >= self._max_leases_per_proxy
                )

                # Với TTL cooldown, "all-dead" KHÔNG còn tuyệt vọng: mỗi
                # proxy có `dead_at` sẽ tự revive khi qua cooldown. Chỉ raise
                # ngay khi có proxy dead permanent (`dead_at is None`) và
                # KHÔNG có proxy nào có triển vọng revive/release.
                #
                # Khi cooldown = 0 (user tắt TTL) — không proxy nào được
                # revive tự động → coi như KHÔNG có pending revival, tránh
                # caller kẹt vòng poll vô hạn.
                has_pending_revival = self._dead_cooldown_seconds > 0 and any(
                    state.dead and state.dead_at is not None
                    for state in self._proxies.values()
                )

                if leased_out_count == 0 and not has_pending_revival:
                    # Toàn bộ pool dead vĩnh viễn (VD user set cooldown=0 và
                    # tất cả đã force_dead) → không thể tự phục hồi bằng cách
                    # đợi. Raise bất kể `wait_for_release` để caller không
                    # treo vô hạn.
                    raise ProxyExhaustedError(
                        total_proxies=total_proxies,
                        dead_count=dead_count,
                        leased_out_count=leased_out_count,
                    )

                if not wait_for_release:
                    # Semantic cũ: fail-fast khi caller không opt-in chờ.
                    raise ProxyExhaustedError(
                        total_proxies=total_proxies,
                        dead_count=dead_count,
                        leased_out_count=leased_out_count,
                    )

                # Fall-through xuống dưới để wait — hoặc 1 lease được release,
                # hoặc 1 proxy tự revive sau cooldown, vòng poll kế sẽ thấy.

            # ------ Ngoài lock — cho phép release/mark_alive/apply_settings chạy ------
            if cancellation_token is not None and cancellation_token.is_cancelled():
                # User cancel job đang chờ proxy — CancelledError để
                # `_run_handler` đưa job về STOPPED thay vì kẹt vô hạn.
                raise asyncio.CancelledError()

            await asyncio.sleep(_ACQUIRE_WAIT_POLL_SECONDS)

    def release(self, lease: ProxyLease | None) -> None:
        """Trả lại `lease` cho pool (Requirement 9.5). No-op nếu `lease is None`.

        Giảm `lease_count` của đúng proxy đã cấp lease này về đúng trạng
        thái trước khi `acquire` (round-trip). No-op nếu `proxy_id` không
        còn tồn tại trong pool (đã bị loại khỏi `proxy.list` qua
        `apply_settings` trong lúc job đang chạy) — không có gì để trả lại.
        """
        if lease is None:
            return

        state = self._proxies.get(lease.proxy_id)
        if state is None:
            return

        if state.lease_count > 0:
            state.lease_count -= 1

    def mark_dead(self, proxy_id: str) -> None:
        """Ghi nhận 1 lỗi kết nối liên tiếp trên `proxy_id` (Requirement 9.6).

        Tên method giữ nguyên `mark_dead` theo tasks.md, nhưng hành vi thực
        tế là "ghi nhận lỗi kết nối" — caller (ví dụ httpx client wrapper ở
        `payments/ideal/`) PHẢI gọi method này MỖI KHI có 1 lỗi kết nối xảy
        ra trên proxy đó, KHÔNG chỉ gọi khi muốn loại bỏ proxy ngay. Bên
        trong, method tự tăng `consecutive_errors` và CHỈ thực sự chuyển
        proxy sang trạng thái dead khi `consecutive_errors >= proxy.dead_threshold`.

        No-op nếu `proxy_id` không tồn tại trong pool (đã bị loại khỏi
        `proxy.list`).
        """
        state = self._proxies.get(proxy_id)
        if state is None:
            return

        state.consecutive_errors += 1
        if state.consecutive_errors >= self._dead_threshold and not state.dead:
            # Chỉ set `dead_at` khi chuyển alive → dead. Nếu proxy đã dead
            # sẵn thì giữ nguyên timestamp cũ để TTL không bị "gia hạn"
            # mỗi lần gọi lại mark_dead — tránh proxy bị "khoá" mãi khi
            # có burst lỗi liên tiếp sau khi đã dead.
            state.dead = True
            state.dead_at = time.time()

    def mark_alive(self, proxy_id: str) -> None:
        """Khôi phục `proxy_id` về khả dụng, reset bộ đếm lỗi liên tiếp về 0.

        Gọi thủ công qua Frontend_App HOẶC tự động khi job thành công
        (`JobStatus.QR_READY`) → reset `consecutive_errors` để proxy được
        coi là "healthy" cho job kế tiếp. No-op nếu `proxy_id` không tồn
        tại trong pool.
        """
        state = self._proxies.get(proxy_id)
        if state is None:
            return

        state.dead = False
        state.dead_at = None
        state.consecutive_errors = 0

    def force_dead(self, proxy_id: str) -> None:
        """Mark 1 proxy dead NGAY LẬP TỨC, bypass `dead_threshold`.

        Khác với `mark_dead()` (tăng counter, chỉ dead khi ≥ threshold),
        `force_dead()` set `dead=True` ngay từ lần fail đầu tiên. Dùng cho
        các lỗi HARD không hồi phục được:
            - Format proxy line sai → `ValueError` từ `materialize_proxy`.
            - Preflight probe fail `reason=auth` (DNS/host/proxy-auth
              hỏng) → cả line hỏng, không có hope tự phục hồi.

        Ngược lại với `mark_dead()` — dùng cho fail SOFT (timeout / reset
        / IP throttle) có thể chỉ do glitch tạm.

        No-op nếu `proxy_id` không tồn tại trong pool.
        """
        state = self._proxies.get(proxy_id)
        if state is None:
            return

        # Chỉ (re)set `dead_at` khi chuyển alive → dead — cùng lý do như
        # `mark_dead`: tránh gia hạn TTL vô hạn nếu caller gọi force_dead
        # lặp lại trên proxy đã dead.
        if not state.dead:
            state.dead_at = time.time()
        state.dead = True
        # Set consecutive_errors đủ lớn để `mark_alive` thủ công qua UI
        # trước khi được dùng lại — tránh reset về 0 vô tình qua flow logic.
        state.consecutive_errors = max(state.consecutive_errors, self._dead_threshold)

    @property
    def probe_config(self) -> ProbeConfig:
        """Snapshot config probe hiện tại — đọc-only. Rebuild qua `apply_settings`."""
        return self._probe_config

    def stats(self) -> tuple[int, int, int]:
        """Snapshot count `(total_proxies, dead_count, leased_out_count)`.

        Dùng cho `acquire_live_proxy` build `ProxyExhaustedError` với số
        chính xác khi probe budget cạn nhưng pool chưa hoàn toàn hỏng
        (edge case: probe fail nhiều lần trong khi threshold chưa đạt).

        Chạy `_revive_expired` trước khi đếm để số `dead_count` phản ánh
        đúng state hiện tại — proxy đã quá cooldown TTL sẽ được coi là
        alive ngay tại lần `stats()` này (lazy revive pattern).
        """
        self._revive_expired()
        total = len(self._proxies)
        dead = sum(1 for state in self._proxies.values() if state.dead)
        leased = sum(
            1
            for state in self._proxies.values()
            if not state.dead and state.lease_count >= self._max_leases_per_proxy
        )
        return (total, dead, leased)

    def _revive_expired(self) -> None:
        """Auto-revive các proxy đã dead quá `_dead_cooldown_seconds`.

        Lazy pattern: KHÔNG cần background task — hàm này được gọi ở đầu
        `acquire()` và `stats()`, hai điểm duy nhất cần trạng thái dead
        chính xác. `_pick_*` chạy ngay sau đó nên đọc `state.dead` là đủ.

        Điều kiện revive:
            - `state.dead is True`.
            - `state.dead_at is not None` (proxy set từ code mới có TTL).
            - `_dead_cooldown_seconds > 0` (0 = TTL disabled, giữ dead).
            - `time.time() - state.dead_at >= _dead_cooldown_seconds`.

        Khi revive: reset cả `dead=False`, `dead_at=None`,
        `consecutive_errors=0` — cho proxy "làm lại từ đầu", tránh trạng
        thái flapping dead ↔ alive liên tục do counter còn treo.
        """
        if self._dead_cooldown_seconds <= 0:
            return
        now = time.time()
        for state in self._proxies.values():
            if not state.dead or state.dead_at is None:
                continue
            if now - state.dead_at >= self._dead_cooldown_seconds:
                state.dead = False
                state.dead_at = None
                state.consecutive_errors = 0

    def _pick_round_robin(self) -> str | None:
        """Chọn proxy khả dụng kế tiếp theo thứ tự cyclic, xoay từ con trỏ hiện tại."""
        proxy_ids = list(self._proxies.keys())
        total = len(proxy_ids)
        for offset in range(total):
            index = (self._round_robin_cursor + offset) % total
            proxy_id = proxy_ids[index]
            state = self._proxies[proxy_id]
            if not state.dead and state.lease_count < self._max_leases_per_proxy:
                self._round_robin_cursor = (index + 1) % total
                return proxy_id
        return None

    def _pick_least_used(self) -> str | None:
        """Chọn proxy khả dụng có `lease_count` thấp nhất (tie-break: thứ tự trong list)."""
        best_proxy_id: str | None = None
        best_lease_count: int | None = None
        for proxy_id, state in self._proxies.items():
            if state.dead or state.lease_count >= self._max_leases_per_proxy:
                continue
            if best_lease_count is None or state.lease_count < best_lease_count:
                best_proxy_id = proxy_id
                best_lease_count = state.lease_count
        return best_proxy_id
