"""Preflight probe + `acquire_live_proxy` + `is_network_error` classifier.

Filter proxy dead TRƯỚC khi job chạy + phân loại lỗi network vs business
để mark_dead chính xác.

Port ý tưởng từ ``gpt_signup_hybrid/web/proxy_health.py``, adapt về interface
`ProxyPool` lease-based của ``ideal_qr_tool``.

Flow:
    1. Job xin proxy → gọi `acquire_live_proxy(pool, ...)`.
    2. Loop tối đa `probe_max_tries` lần:
        a. `pool.acquire()` lease 1 proxy (round_robin/least_used).
        b. Materialize `{SID}` → concrete URL.
        c. Probe URL bằng curl_cffi (target = `probe_endpoint` — KHÔNG phải
           chatgpt.com để tránh fingerprint-gate / rate-limit).
        d. OK → replace lease.materialized_url = URL đã materialize (fix
           SID cho lease này) → return.
        e. Auth fail (host không resolve, 407 proxy-auth) → `force_dead`
           line ngay (không đợi threshold) + release lease + next iteration.
        f. IP fail (timeout/reset/refused/tunnel) + line có `{SID}` →
           release + rotate SID (tối đa `probe_sid_retry_per_line` lần
           trên cùng line).
        g. IP fail + line không template → `mark_dead` (tăng counter, dead
           khi ≥ threshold) + release + next.
    3. Cạn `probe_max_tries` hoặc pool trả `None` → return None (caller
       quyết định: fallback direct hoặc `proxy_exhausted` error).

Concurrency policy:
    - `acquire_live_proxy` (per-job): KHÔNG có semaphore riêng. Số probe
      song song = số job đang trong phase acquire, đã bị giới hạn bởi
      `JobManager._semaphore = ideal.max_concurrent` outer. Trước đây có
      1 semaphore process-global size 5 ở đây khiến `max_concurrent=15`
      chỉ chạy được 5 job song song (10 job kẹt chờ probe slot) — đã gỡ.
    - `probe_pool_batch` (preflight startup / on-save + `POST /api/proxy/
      probe-batch`): DÙNG `asyncio.Semaphore(config.concurrency)` LOCAL
      per-call. Cần vì batch có thể quét đồng thời 100-500 proxy khi
      user test lô lớn, không phải job flow.

Probe target lý tưởng — `api64.ipify.org` (default): trả IP thô, hỗ trợ
HTTPS, IPv6 dual-stack, KHÔNG rate limit, KHÔNG fingerprint-gate. User có
thể override qua Settings nếu cần probe target khác. Vì mỗi probe đi qua
1 proxy KHÁC NHAU nên probe song song không dồn về 1 IP → không DoS
endpoint dù concurrency cao.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import TYPE_CHECKING, Awaitable, Callable, Final

from app.core import http_client as http
from app.core.errors import ProxyExhaustedError
from app.core.proxy_format import has_template, mask_proxy, materialize_proxy

if TYPE_CHECKING:
    from app.core.payment_flow import CancellationToken
    from app.core.proxy_pool import ProxyLease, ProxyPool


# ---------------------------------------------------------------------------
# Knobs — nguồn duy nhất cho default. `ProxyPool.apply_settings` đọc snapshot
# rồi expose qua `pool.probe_config` cho `acquire_live_proxy`. Giá trị dưới
# đây chỉ dùng khi Settings_Store trả `None` (chưa từng set qua UI).
# ---------------------------------------------------------------------------

_DEFAULT_PROBE_ENABLED: Final[bool] = True
_DEFAULT_PROBE_ENDPOINT: Final[str] = "https://api64.ipify.org"
_DEFAULT_PROBE_TIMEOUT: Final[int] = 6
_DEFAULT_PROBE_MAX_TRIES: Final[int] = 10
_DEFAULT_PROBE_SID_RETRY: Final[int] = 2
_DEFAULT_PROBE_CONCURRENCY: Final[int] = 5
_DEFAULT_FALLBACK_DIRECT: Final[bool] = True


@dataclasses.dataclass(frozen=True)
class ProbeConfig:
    """Snapshot config probe đọc từ Settings_Store — immutable per apply_settings.

    Đặt trong `proxy_health` (không phải `proxy_pool`) để giữ boundary rõ:
    pool = "dumb container", probe policy = tách module riêng.
    """

    enabled: bool = _DEFAULT_PROBE_ENABLED
    endpoint: str = _DEFAULT_PROBE_ENDPOINT
    timeout_seconds: int = _DEFAULT_PROBE_TIMEOUT
    max_tries: int = _DEFAULT_PROBE_MAX_TRIES
    sid_retry_per_line: int = _DEFAULT_PROBE_SID_RETRY
    #: Chỉ dùng cho `probe_pool_batch` (batch preflight startup / on-save).
    #: KHÔNG áp dụng cho `acquire_live_proxy` — per-job probe đã bị giới
    #: hạn bởi `JobManager._semaphore` outer.
    concurrency: int = _DEFAULT_PROBE_CONCURRENCY
    fallback_direct_on_exhausted: bool = _DEFAULT_FALLBACK_DIRECT

    @classmethod
    def from_snapshot(cls, snapshot: dict) -> "ProbeConfig":
        """Build từ snapshot Settings_Store — validate + fallback default.

        Snapshot key: ``proxy.probe_enabled``, ``proxy.probe_endpoint``, ...
        `None` / missing → dùng default. KHÔNG raise ở tầng này (Settings_Store
        đã validate qua whitelist khi user set); coerce bool an toàn để không
        vì 1 giá trị lỗi lịch sử mà crash startup.
        """
        def _bool(key: str, default: bool) -> bool:
            v = snapshot.get(key)
            if isinstance(v, bool):
                return v
            return default

        def _int(key: str, default: int) -> int:
            v = snapshot.get(key)
            if isinstance(v, int) and not isinstance(v, bool):
                return v
            return default

        def _str(key: str, default: str) -> str:
            v = snapshot.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
            return default

        return cls(
            enabled=_bool("proxy.probe_enabled", _DEFAULT_PROBE_ENABLED),
            endpoint=_str("proxy.probe_endpoint", _DEFAULT_PROBE_ENDPOINT),
            timeout_seconds=_int("proxy.probe_timeout_seconds", _DEFAULT_PROBE_TIMEOUT),
            max_tries=_int("proxy.probe_max_tries", _DEFAULT_PROBE_MAX_TRIES),
            sid_retry_per_line=_int(
                "proxy.probe_sid_retry_per_line", _DEFAULT_PROBE_SID_RETRY
            ),
            concurrency=_int("proxy.probe_concurrency", _DEFAULT_PROBE_CONCURRENCY),
            fallback_direct_on_exhausted=_bool(
                "proxy.fallback_direct_on_exhausted", _DEFAULT_FALLBACK_DIRECT
            ),
        )


# ---------------------------------------------------------------------------
# Exception classifier
# ---------------------------------------------------------------------------

# Reason phân loại probe fail — quyết định policy tiếp theo (mark_dead
# permanent vs rotate SID).
PROBE_REASON_OK: Final[str] = "ok"
PROBE_REASON_AUTH: Final[str] = "auth"  # host/DNS/proxy-auth fail → dead
PROBE_REASON_IP: Final[str] = "ip"  # timeout/reset → IP-level, rotate SID


# Markers phổ biến của lỗi network/proxy trong exception message —
# port từ `gpt_signup_hybrid/_browser_retry.py::NETWORK_ERROR_MARKERS`.
# Dùng cho `is_network_error()` để `job_manager` phân biệt:
#   - Job fail vì proxy chết (mark_dead proxy đó).
#   - Job fail vì lỗi business (password sai, MFA sai, ...) — KHÔNG mark
#     để không oan cho proxy đang hoạt động.
NETWORK_ERROR_MARKERS: Final[tuple[str, ...]] = (
    # curl_cffi transport-level (path chính của tool này). RequestException
    # message thường có dạng "Failed to perform, curl: (N) <text>".
    "Failed to perform",
    "Operation timed out",
    "Timeout",
    "Connection reset",
    "Connection refused",
    "Could not connect",
    "Failed to connect",
    "Recv failure",
    "Send failure",
    "SSL connection",
    "Empty reply",
    # Exception class names — curl_cffi
    "ConnectionError",
    "ConnectTimeout",
    "ReadTimeout",
    "ProxyError",
    "DNSError",
    "SSLError",
    "CurlError",
    "RequestException",
    # DNS
    "getaddrinfo",
    "Could not resolve host",
    "Couldn't resolve host",
    "Name or service not known",
    "Temporary failure in name resolution",
    # Proxy-level auth
    "407",
    "proxy authentication",
    # Reason keyword — LoginError._LOGIN_ERROR_NETWORK
    "network_error",
    "reason=network_error",
)


def is_network_error(exc_or_msg: BaseException | str | None) -> bool:
    """True nếu `exc_or_msg` là lỗi network/proxy → đáng mark_dead proxy.

    Nhận exception hoặc string message (VD `error_message` từ `JobResult`).
    Case-insensitive matching để chắc chắn không miss format khác nhau.

    Ví dụ:
        - `"Đăng nhập thất bại: reason=network_error"` → True (login fail
          do transport error, mark_dead proxy).
        - `"Đăng nhập thất bại: reason=invalid_credential"` → False
          (password sai, KHÔNG mark).
        - `curl_cffi.exceptions.ConnectionError("...")` → True.
    """
    if exc_or_msg is None:
        return False
    msg = str(exc_or_msg).lower()
    if not msg:
        return False
    return any(marker.lower() in msg for marker in NETWORK_ERROR_MARKERS)


def _classify_probe_exc(exc: BaseException) -> str:
    """Phân loại exception probe → `PROBE_REASON_AUTH` hoặc `PROBE_REASON_IP`.

    Dùng `http_client.classify_error()` (type-based) làm nguồn chính, kèm
    fallback string-match cho DNS/407 để cover edge case curl_cffi wrap
    lỗi thành RequestException base.

    Mapping:
        - `dns`, `proxy_auth` → PROBE_REASON_AUTH (line hỏng, không rotate SID).
        - `timeout`, `connect`, `proxy_connect`, `ssl`, `protocol`, `unknown`
          → PROBE_REASON_IP (rotate SID nếu template).
    """
    label = http.classify_error(exc)
    if label in ("dns", "proxy_auth"):
        return PROBE_REASON_AUTH
    if label != "unknown":
        return PROBE_REASON_IP
    # Fallback string-match cho curl error message không được `classify_error`
    # cover (RequestException base với text raw từ libcurl).
    msg = str(exc).lower()
    if any(
        m in msg
        for m in (
            "could not resolve host",
            "couldn't resolve host",
            "name or service not known",
            "nodename nor servname",
            "getaddrinfo",
        )
    ):
        return PROBE_REASON_AUTH
    if "proxy authentication" in msg or " 407" in msg or msg.endswith("407"):
        return PROBE_REASON_AUTH
    return PROBE_REASON_IP


# ---------------------------------------------------------------------------
# Probe function — pluggable qua param cho test
# ---------------------------------------------------------------------------

ProbeFn = Callable[..., Awaitable[tuple[bool, str]]]


async def probe_proxy(
    url: str,
    *,
    endpoint: str,
    timeout: int | float,
    impersonate: str | None = http.DEFAULT_IMPERSONATE,
) -> tuple[bool, str]:
    """Probe 1 proxy URL đã materialize → `(ok, reason)`.

    Args:
        url: URL proxy đã materialize (concrete, không chứa `{SID}`).
        endpoint: URL target probe. Thường là https://api64.ipify.org.
        timeout: Timeout tổng (connect + read) tính bằng giây. Curl sẽ
            áp timeout duy nhất cho toàn bộ request, không tách phase.
        impersonate: Chrome fingerprint profile. Mặc định
            `http.DEFAULT_IMPERSONATE` (`chrome136`) — nhiều proxy provider
            block User-Agent Python thô, giữ impersonate để probe pass qua
            các provider strict về UA/JA3. Endpoint probe không có
            Cloudflare vẫn accept fingerprint chrome bình thường; overhead
            handshake ~50-100ms/probe chấp nhận được. Truyền `None` để tắt.

    Returns:
        `(True, "ok")` khi HTTP 2xx.
        `(False, "auth")` khi HTTP 407 hoặc exception DNS/proxy-auth.
        `(False, "ip")` khi timeout/reset/tunnel-fail hoặc HTTP status khác.

    KHÔNG raise — mọi exception → convert sang `(False, reason)`.
    """
    try:
        # curl_cffi `AsyncSession(proxy=...)` tự routes cả http:// và
        # https:// qua proxy. `timeout` là tổng (connect + read) — nếu
        # proxy chậm (residential/mobile) TCP handshake có thể > 3s
        # nhưng vẫn tới được response trong tổng timeout, tránh
        # false-negative mark_dead oan proxy chậm.
        async with http.create_async_client(
            proxy=url,
            timeout=timeout,
            allow_redirects=False,
            impersonate=impersonate,
        ) as client:
            response = await client.get(endpoint)
        code = response.status_code
        if 200 <= code < 300:
            return (True, PROBE_REASON_OK)
        if code == 407:
            return (False, PROBE_REASON_AUTH)
        # 3xx/4xx/5xx khác → coi là ip-level (endpoint có thể tạm lỗi,
        # không giết oan proxy).
        return (False, PROBE_REASON_IP)
    except Exception as exc:  # noqa: BLE001 — cần catch mọi transport error
        return (False, _classify_probe_exc(exc))


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


async def acquire_live_proxy(
    pool: "ProxyPool",
    job_id: str,
    config: ProbeConfig,
    *,
    logger: logging.Logger,
    cancellation_token: "CancellationToken | None" = None,
    probe_fn: ProbeFn | None = None,
) -> "ProxyLease | None":
    """Acquire 1 proxy đã probe live cho job — tương đương `pool.acquire()`
    nhưng đã lọc dead + rotate SID.

    Semantic:
        - Config `enabled=False` → passthrough gọi `pool.acquire()` như cũ.
        - Pool rỗng (`proxy.list=[]`) → return `None` (Direct_Mode).
        - Pool có proxy nhưng tất cả dead/fail probe hết `max_tries` →
          return `None` (caller quyết định fallback direct hay raise
          `ProxyExhaustedError`).
        - Có proxy live → return `ProxyLease` với `materialized_url` là URL
          đã materialize SID cụ thể (KHÔNG phải template raw).

    Raises:
        ProxyExhaustedError: Bubbling từ `pool.acquire()` khi
            `dead_count == total_proxies` — không có hope tự phục hồi.
        asyncio.CancelledError: User cancel job đang chờ.
    """
    probe = probe_fn or probe_proxy

    # Config disabled → passthrough. Giữ backward-compat cho test hiện tại.
    if not config.enabled:
        return await pool.acquire(
            job_id,
            wait_for_release=True,
            cancellation_token=cancellation_token,
        )

    # KHÔNG có semaphore riêng ở đây — số probe song song = số job trong
    # phase acquire, đã bị giới hạn bởi `JobManager._semaphore` outer
    # (`ideal.max_concurrent`). Trước đây `_get_probe_semaphore(5)`
    # process-global khiến `max_concurrent=15` bị bottleneck xuống 5 job
    # song song (10 job kẹt chờ probe slot). Xem module docstring.
    tries = 0
    # Ít nhất 1 lần `pool.acquire()` trả non-None → có nghĩa pool KHÔNG rỗng.
    # Phân biệt "pool rỗng (Direct_Mode legit)" vs "pool có proxy nhưng
    # probe fail hết" để job_manager xử lý policy khác nhau.
    ever_got_lease = False

    while tries < config.max_tries:
        if cancellation_token is not None and cancellation_token.is_cancelled():
            raise asyncio.CancelledError()

        # `wait_for_release=True` → chờ có slot khi tất cả proxy đang bị
        # lease (concurrency > pool size). ProxyExhaustedError chỉ raise
        # khi `dead_count == total_proxies` — bubble lên caller xử lý.
        lease = await pool.acquire(
            job_id,
            wait_for_release=True,
            cancellation_token=cancellation_token,
        )
        if lease is None:
            # Pool rỗng → Direct_Mode legit (`proxy.list=[]`).
            return None
        ever_got_lease = True

        # Rotate SID trong cùng 1 line — tối đa `sid_retry_per_line + 1`
        # lần probe (bao gồm lần đầu). Đếm chung vào `tries` để tổng số
        # lần probe/job không vượt `max_tries`.
        template_line = lease.materialized_url
        line_has_template = has_template(template_line)
        sid_attempt = 0
        line_result: tuple[bool, str] | None = None
        chosen_url: str | None = None

        while sid_attempt <= config.sid_retry_per_line and tries < config.max_tries:
            tries += 1
            try:
                chosen_url = materialize_proxy(template_line)
            except ValueError as exc:
                # Format rác → mark_dead ngay (không phí retry). `force_dead`
                # đảm bảo line này không tái xuất trong pool cho tới khi
                # `mark_alive` thủ công / apply_settings mới.
                logger.warning(
                    "[probe] bad proxy format: line=%s error=%s → force_dead",
                    mask_proxy(template_line),
                    exc,
                )
                pool.force_dead(lease.proxy_id)
                line_result = (False, PROBE_REASON_AUTH)
                break

            ok, reason = await probe(
                chosen_url,
                endpoint=config.endpoint,
                timeout=config.timeout_seconds,
            )

            if ok:
                logger.info(
                    "[probe] live job_id=%s proxy=%s try=%d",
                    job_id,
                    mask_proxy(chosen_url),
                    tries,
                )
                # Trả lease với materialized_url = URL đã materialize (SID
                # fix cho lease này). Consumer (`flow.py`) gọi lại
                # `materialize_proxy(url)` sẽ passthrough vì có `://`.
                return dataclasses.replace(lease, materialized_url=chosen_url)

            line_result = (False, reason)
            logger.info(
                "[probe] fail job_id=%s proxy=%s reason=%s try=%d",
                job_id,
                mask_proxy(chosen_url),
                reason,
                tries,
            )

            if reason == PROBE_REASON_AUTH:
                # Auth-level: cả line hỏng, không rotate SID được. Force
                # dead luôn (bypass threshold) để line không tái xuất.
                pool.force_dead(lease.proxy_id)
                break

            # reason == "ip"
            if not line_has_template:
                # Line no-template + ip fail → tăng counter, dead khi đủ
                # threshold. Không có gì để rotate.
                pool.mark_dead(lease.proxy_id)
                break

            # Line có template + ip fail → rotate SID, thử lại.
            sid_attempt += 1
            logger.debug(
                "[probe] rotate SID job_id=%s line=%s sid_attempt=%d",
                job_id,
                mask_proxy(template_line),
                sid_attempt,
            )

        # Kết thúc 1 line (auth-fail, exhausted SID retry, hoặc no-template
        # ip-fail). Release lease trước khi thử line khác. Lease count sẽ
        # về 0 → round_robin/least_used chọn proxy khác ở iteration kế.
        pool.release(lease)

        # Nếu chưa cạn max_tries → tiếp tục thử proxy khác.
        # Vòng lặp `while tries < max_tries` sẽ tự dừng khi cạn.

    # Cạn `max_tries` — pool có proxy nhưng không probe live được cái nào.
    # Raise `ProxyExhaustedError` để job_manager xử lý policy fallback direct.
    # KHÁC pool rỗng (early return None ở trên) — cần phân biệt để không
    # tự động fallback direct khi user chưa opt-in.
    if ever_got_lease:
        # Pool CÓ proxy nhưng probe fail hết `max_tries` → raise
        # `ProxyExhaustedError` để job_manager áp dụng policy fallback.
        # Số liệu (total/dead/leased_out) lấy từ `pool.stats()` để user
        # đọc log/UI biết chính xác cấu hình pool ở thời điểm exhausted.
        total, dead, leased = pool.stats()
        raise ProxyExhaustedError(
            total_proxies=total,
            dead_count=dead,
            leased_out_count=leased,
        )
    logger.warning(
        "[probe] no lease acquired job_id=%s max_tries=%d",
        job_id,
        config.max_tries,
    )
    return None


# ---------------------------------------------------------------------------
# Batch preflight — probe TẤT CẢ proxy trong pool cùng lúc, mark_dead sớm
# ---------------------------------------------------------------------------


async def probe_pool_batch(
    pool: "ProxyPool",
    config: ProbeConfig,
    *,
    logger: logging.Logger,
    total_timeout_seconds: float = 30.0,
) -> dict[str, tuple[bool, str]]:
    """Probe SONG SONG tất cả proxy live trong pool → mark_dead sớm.

    Chạy 1 lần tại 2 boundary:
        1. Startup (`bootstrap.py` sau `proxy_pool.apply_settings`).
        2. Runtime khi user save Settings với `proxy.list` mới
           (`routes_settings.py` sau `proxy_pool.apply_settings`).

    Với mỗi proxy line trong pool:
        - Materialize → probe → phân loại:
            * OK → mark_alive (đảm bảo counter reset).
            * Auth fail → force_dead ngay.
            * IP fail → mark_dead tăng counter (dead khi ≥ threshold).
        - Format rác → force_dead.

    Bảo vệ `total_timeout_seconds` để KHÔNG BLOCK startup vô hạn — nếu
    pool 20 proxy toàn timeout 6s × concurrency=4 = 30s worst-case. Timeout
    tổng cắt sớm, proxy chưa probe xong để nguyên trạng thái cũ (sẽ được
    probe lại per-job).

    KHÔNG raise — return dict `{proxy_id: (ok, reason)}` cho caller log/UI.
    Config disabled → return dict rỗng (skip preflight).

    Args:
        pool: `ProxyPool` singleton — probe từ `list(pool._proxies)` public
            surface qua iterate. Cần method `pool.stats()` để đếm; iterate
            proxy_id qua private nhưng pool cùng module core nên OK.
        config: `ProbeConfig` từ pool. `enabled=False` → skip.
        logger: Log tiến trình từng proxy (mask credential).
        total_timeout_seconds: Timeout tổng cho toàn batch probe. Default
            30s — phù hợp pool ≤20 proxy.

    Returns:
        Dict `{proxy_id: (ok, reason)}`. Ví dụ:
            `{"host1:8080:user:pass": (True, "ok"),
              "host2:8080": (False, "auth"),
              "host3:8080:{SID}:x": (False, "ip")}`
        Empty nếu config disabled hoặc pool rỗng.
    """
    if not config.enabled:
        return {}

    # Lấy snapshot proxy_id live để probe. Iterate `pool._proxies` là
    # cross-module private access — chấp nhận vì `proxy_health` cùng
    # namespace `app.core` với `proxy_pool`, không phá boundary.
    proxy_ids: list[str] = list(pool._proxies.keys())  # noqa: SLF001
    if not proxy_ids:
        return {}

    # Semaphore LOCAL per-call, size = `probe_concurrency`. Cần vì batch
    # có thể quét đồng thời 100-500 proxy (pool lớn khi user paste lô lớn)
    # → không muốn burst hết cùng lúc. Khác `acquire_live_proxy` (per-job,
    # đã bị `JobManager._semaphore` giới hạn) — chỗ này là batch riêng,
    # không đi qua semaphore đó nên phải có rate limit riêng.
    sem = asyncio.Semaphore(max(1, config.concurrency))
    results: dict[str, tuple[bool, str]] = {}
    logger.info(
        "[probe-batch] start: %d proxies, timeout=%ss, concurrency=%d",
        len(proxy_ids),
        total_timeout_seconds,
        config.concurrency,
    )

    async def _probe_one(proxy_id: str) -> None:
        try:
            url = materialize_proxy(proxy_id)
        except ValueError:
            pool.force_dead(proxy_id)
            results[proxy_id] = (False, PROBE_REASON_AUTH)
            logger.warning(
                "[probe-batch] bad format: %s → force_dead",
                mask_proxy(proxy_id),
            )
            return

        async with sem:
            ok, reason = await probe_proxy(
                url,
                endpoint=config.endpoint,
                timeout=config.timeout_seconds,
            )
        results[proxy_id] = (ok, reason)

        if ok:
            pool.mark_alive(proxy_id)
            logger.info(
                "[probe-batch] live: %s reason=%s",
                mask_proxy(proxy_id),
                reason,
            )
            return
        if reason == PROBE_REASON_AUTH:
            pool.force_dead(proxy_id)
            logger.warning(
                "[probe-batch] auth-fail: %s → force_dead",
                mask_proxy(proxy_id),
            )
        else:
            pool.mark_dead(proxy_id)
            logger.info(
                "[probe-batch] ip-fail: %s → mark_dead (counter+1)",
                mask_proxy(proxy_id),
            )

    tasks = [asyncio.create_task(_probe_one(pid)) for pid in proxy_ids]
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=total_timeout_seconds,
        )
    except asyncio.TimeoutError:
        # Cancel tasks chưa xong — giữ nguyên trạng thái pool cho proxy
        # chưa probe (sẽ probe lại per-job).
        for task in tasks:
            if not task.done():
                task.cancel()
        # `await` các task đã cancel để cleanup tránh warning
        # `Task was destroyed but it is pending`.
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.warning(
            "[probe-batch] timeout %ss — %d/%d probes complete",
            total_timeout_seconds,
            len(results),
            len(proxy_ids),
        )

    live = sum(1 for ok, _ in results.values() if ok)
    logger.info(
        "[probe-batch] done: %d live / %d total",
        live,
        len(proxy_ids),
    )
    return results
