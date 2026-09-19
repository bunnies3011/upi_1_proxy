"""Proxy admin routes — probe batch read-only.

Endpoint:
    - `POST /api/proxy/probe-batch` — 1-shot JSON: batch xong hết mới trả
      kết quả. Giữ nguyên contract cho backward-compat.
    - `POST /api/proxy/probe-batch/stream` — NDJSON stream: push từng
      result ngay khi có, FE cập nhật row live không phải chờ hết batch.
      Client-side có thể abort mid-flight (đóng connection) để dừng probe.

Mục tiêu:
    User đang gõ / dán danh sách proxy trong modal `ProxyConfigButton`
    (FE), bấm "Test all" trước khi Lưu để biết proxy nào live/dead. Cả 2
    endpoint đều KHÔNG đụng `ProxyPool` state — không mark_dead, không
    mark_alive, không force_dead. Đây là **read-only preview** cho draft.

    Khác với hook auto-preflight trong `routes_settings._write_through_runtime_state`
    (chạy `probe_pool_batch()` SAU khi user Lưu `proxy.list`):
    - Preflight tự động: có side-effect (mark_dead sớm) nhưng chỉ chạy
      sau khi Lưu.
    - Endpoint này: không side-effect nhưng chạy khi user muốn (kể cả
      không Lưu).

Payment_Module_Boundary: module chỉ import từ `app.core.*` + `app.api.*`,
KHÔNG import `app.payments.*`.

Fail-safe: mọi exception trong probe 1 proxy được `probe_proxy()` convert
sang `(False, reason)` — route KHÔNG raise 5xx vì 1 proxy lỗi.

Tối ưu 2026-07 (so với version pre-stream):
    - `_MAX_CONCURRENCY` 20 → 100 để pool lớn (~100 proxy) không phải
      chia nhiều wave tuần tự. Không gây DoS vì probe song song đi qua
      NHIỀU proxy khác nhau, không dồn về 1 IP.
    - `_TOTAL_BATCH_TIMEOUT_SECONDS` cứng 60s → tính động theo
      `ceil(n / concurrency) * per_probe_timeout + slack`, có min 30s
      và max 180s để bảo vệ HTTP response không treo mãi.
    - `probe_proxy()` tách `(connect_timeout=3s, read_timeout=timeout)` +
      tắt `impersonate` mặc định (xem `proxy_health.probe_proxy`).
    - Endpoint stream push từng result → FE thấy progress realtime, không
      phải chờ hết batch mới thấy proxy nào live/dead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import AsyncIterator, Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.api import deps
from app.api.schemas import (
    ProxyProbeBatchRequest,
    ProxyProbeBatchResponse,
    ProxyProbeItemResult,
)
from app.core.proxy_format import mask_proxy, materialize_proxy, sanitize_proxy_text
from app.core.proxy_health import PROBE_REASON_OK, probe_proxy
from app.core.proxy_pool import ProxyPool


# Guardrail clamp values — bảo vệ backend khỏi input quá đáng. Concurrency
# 100 vẫn an toàn vì mỗi probe đi qua 1 proxy KHÁC NHAU (không dồn về 1 IP
# → không DoS provider endpoint).
_MIN_TIMEOUT: Final[int] = 3
_MAX_TIMEOUT: Final[int] = 30
_MIN_CONCURRENCY: Final[int] = 1
_MAX_CONCURRENCY: Final[int] = 100
_MAX_PROXIES_PER_BATCH: Final[int] = 500

# Batch wall-clock timeout — tính động dựa trên (n_proxies, concurrency,
# per_probe_timeout). Clamp [30s, 180s].
_MIN_BATCH_TIMEOUT: Final[float] = 30.0
_MAX_BATCH_TIMEOUT: Final[float] = 180.0
_BATCH_TIMEOUT_SLACK: Final[float] = 10.0

router = APIRouter(prefix="/api/proxy", tags=["proxy"])

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp(value: int | None, minimum: int, maximum: int, default: int) -> int:
    """Clamp value vào `[minimum, maximum]`, fallback `default` khi `None`."""
    if value is None:
        return default
    return max(minimum, min(maximum, value))


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    """Strip whitespace + bỏ dòng rỗng + dedupe (giữ thứ tự xuất hiện đầu)."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in items:
        line = (raw or "").strip()
        if not line or line in seen:
            continue
        seen.add(line)
        result.append(line)
    return result


def _compute_batch_timeout(
    n_proxies: int, concurrency: int, per_probe_timeout: int
) -> float:
    """Tính timeout tổng cho batch động theo input.

    Formula: `ceil(n/conc) * per_probe_timeout + slack` — giả định worst-case
    tất cả proxy dead, chia thành `ceil(n/conc)` wave tuần tự, mỗi wave
    mất tối đa `per_probe_timeout`. `+slack` để có buffer cho overhead
    (event loop, task cancel, TLS handshake).

    Clamp `[_MIN_BATCH_TIMEOUT, _MAX_BATCH_TIMEOUT]`:
        - Min 30s: batch nhỏ (VD 1-2 proxy) vẫn có buffer chờ DNS/TLS chậm.
        - Max 180s: bảo vệ HTTP response không treo mãi nếu concurrency
          quá thấp so với n_proxies.

    Ví dụ:
        - n=100, conc=100, timeout=6 → ceil(1) * 6 + 10 = 16s → clamp 30s.
        - n=100, conc=20, timeout=6 → ceil(5) * 6 + 10 = 40s.
        - n=100, conc=5, timeout=30 → ceil(20) * 30 + 10 = 610s → clamp 180s.
    """
    if n_proxies <= 0 or concurrency <= 0:
        return _MIN_BATCH_TIMEOUT
    n_waves = math.ceil(n_proxies / concurrency)
    est = n_waves * per_probe_timeout + _BATCH_TIMEOUT_SLACK
    return max(_MIN_BATCH_TIMEOUT, min(_MAX_BATCH_TIMEOUT, float(est)))


def _mask_line(line: str) -> str:
    """Mask 1 proxy line — fallback `'***'` nếu format rác không materialize được.

    Dùng cho các code path cần mask credential mà không muốn probe
    (VD placeholder khi batch timeout cắt sớm).
    """
    try:
        return mask_proxy(materialize_proxy(line))
    except ValueError:
        return "***"


def _reason_to_hint(reason: str, timeout_seconds: int) -> str:
    """Convert `reason` enum → hint text ngắn cho UI (đã sanitize credential).

    KHÔNG ném exception chi tiết từ curl_cffi ra client vì có thể chứa
    proxy URL có credential — dùng hint cố định theo phân loại.
    """
    if reason == PROBE_REASON_OK:
        return ""
    if reason == "auth":
        return "auth_fail (DNS/407)"
    if reason == "ip":
        return f"ip_fail (timeout {timeout_seconds}s / connection reset)"
    if reason == "format":
        return "format_invalid"
    return f"unknown_reason={reason}"


async def _probe_single_line(
    line: str,
    *,
    endpoint: str,
    timeout: int,
    semaphore: asyncio.Semaphore,
) -> ProxyProbeItemResult:
    """Probe 1 line → `ProxyProbeItemResult` — dùng chung 2 endpoint.

    Materialize trước semaphore.acquire để format rác không phí slot.
    """
    try:
        materialized_url = materialize_proxy(line)
    except ValueError as exc:
        return ProxyProbeItemResult(
            proxy=line,
            proxy_masked="***",
            ok=False,
            reason="format",
            latency_ms=0,
            error=sanitize_proxy_text(str(exc)),
        )

    masked = mask_proxy(materialized_url)
    item_started = time.perf_counter()
    async with semaphore:
        ok, reason = await probe_proxy(
            materialized_url, endpoint=endpoint, timeout=timeout
        )
    latency_ms = int((time.perf_counter() - item_started) * 1000)

    return ProxyProbeItemResult(
        proxy=line,
        proxy_masked=masked,
        ok=ok,
        reason=reason,
        latency_ms=latency_ms,
        error=None if ok else _reason_to_hint(reason, timeout),
    )


def _prepare_batch_params(
    payload: ProxyProbeBatchRequest, proxy_pool: ProxyPool
) -> tuple[list[str], str, int, int]:
    """Chuẩn hóa input chung cho 2 endpoint — dedupe + clamp + default.

    Returns:
        `(unique_lines, endpoint, timeout, concurrency)` — sẵn sàng probe.
    """
    unique_lines = _dedupe_preserve_order(payload.proxies)
    if len(unique_lines) > _MAX_PROXIES_PER_BATCH:
        _logger.warning(
            "[probe-batch-api] input %d proxies exceeds max %d, truncating",
            len(unique_lines),
            _MAX_PROXIES_PER_BATCH,
        )
        unique_lines = unique_lines[:_MAX_PROXIES_PER_BATCH]

    probe_config = proxy_pool.probe_config
    endpoint = (payload.endpoint or "").strip() or probe_config.endpoint
    timeout = _clamp(
        payload.timeout_seconds,
        _MIN_TIMEOUT,
        _MAX_TIMEOUT,
        probe_config.timeout_seconds,
    )
    concurrency = _clamp(
        payload.concurrency,
        _MIN_CONCURRENCY,
        _MAX_CONCURRENCY,
        probe_config.concurrency,
    )
    return unique_lines, endpoint, timeout, concurrency


# ---------------------------------------------------------------------------
# 1-shot endpoint (backward compat)
# ---------------------------------------------------------------------------


@router.post("/probe-batch", response_model=ProxyProbeBatchResponse)
async def probe_batch(
    payload: ProxyProbeBatchRequest,
    proxy_pool: ProxyPool = Depends(deps.get_proxy_pool),
) -> ProxyProbeBatchResponse:
    """Probe song song danh sách proxy → chờ hết batch mới trả kết quả.

    Contract giữ nguyên (backward-compat cho client cũ). Internal đã tối ưu
    (concurrency cap ↑, batch timeout động, impersonate off, connect_timeout
    tách) — cùng hiệu năng với endpoint stream, chỉ khác cách trả kết quả.

    Semantic:
        - Rỗng / toàn dòng whitespace → trả `results=[]`, `total=0`, `live=0`.
        - Trùng dòng (dedupe) → chỉ probe 1 lần, trả 1 kết quả (thứ tự
          xuất hiện đầu).
        - Line có `{SID}` template → materialize 1 SID ngẫu nhiên cho lần
          test này. Kết quả CHỈ đại diện cho SID đó — SID khác có thể ra
          IP khác. Chấp nhận vì lần probe production sau đó tự rotate SID
          nếu fail.
        - Format rác → `reason="format"`, `ok=False`, `error="<mô tả>"`.
        - Vượt `_MAX_PROXIES_PER_BATCH` → cắt bớt, log warning.
        - Batch tổng chạy trong tối đa `_compute_batch_timeout(...)`: proxy
          chưa kịp probe khi timeout → `reason="ip"`, `ok=False`,
          `error="batch_timeout"`.
    """
    started_at = time.perf_counter()
    unique_lines, endpoint, timeout, concurrency = _prepare_batch_params(
        payload, proxy_pool
    )

    if not unique_lines:
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        return ProxyProbeBatchResponse(
            results=[], total=0, live=0, elapsed_ms=elapsed_ms
        )

    batch_timeout = _compute_batch_timeout(len(unique_lines), concurrency, timeout)
    _logger.info(
        "[probe-batch-api] start: %d proxies, endpoint=%s, timeout=%ss, "
        "concurrency=%d, batch_timeout=%.1fs",
        len(unique_lines),
        endpoint,
        timeout,
        concurrency,
        batch_timeout,
    )

    sem = asyncio.Semaphore(concurrency)
    results: list[ProxyProbeItemResult | None] = [None] * len(unique_lines)

    async def _probe_and_store(index: int, line: str) -> None:
        results[index] = await _probe_single_line(
            line, endpoint=endpoint, timeout=timeout, semaphore=sem
        )

    tasks = [
        asyncio.create_task(_probe_and_store(i, line))
        for i, line in enumerate(unique_lines)
    ]
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=batch_timeout,
        )
    except asyncio.TimeoutError:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        _logger.warning(
            "[probe-batch-api] batch timeout %.1fs — filling incomplete items",
            batch_timeout,
        )

    for i, item in enumerate(results):
        if item is not None:
            continue
        line = unique_lines[i]
        results[i] = ProxyProbeItemResult(
            proxy=line,
            proxy_masked=_mask_line(line),
            ok=False,
            reason="ip",
            latency_ms=int(batch_timeout * 1000),
            error="batch_timeout",
        )

    final_results: list[ProxyProbeItemResult] = [r for r in results if r is not None]
    live_count = sum(1 for r in final_results if r.ok)
    elapsed_ms = int((time.perf_counter() - started_at) * 1000)

    _logger.info(
        "[probe-batch-api] done: %d live / %d total in %dms",
        live_count,
        len(final_results),
        elapsed_ms,
    )

    return ProxyProbeBatchResponse(
        results=final_results,
        total=len(final_results),
        live=live_count,
        elapsed_ms=elapsed_ms,
    )


# ---------------------------------------------------------------------------
# Streaming endpoint — NDJSON push per-result
# ---------------------------------------------------------------------------


def _ndjson(msg: dict) -> str:
    """Serialize 1 message → 1 dòng NDJSON."""
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=False) + "\n"


@router.post("/probe-batch/stream")
async def probe_batch_stream(
    payload: ProxyProbeBatchRequest,
    request: Request,
    proxy_pool: ProxyPool = Depends(deps.get_proxy_pool),
) -> StreamingResponse:
    """Probe song song → stream từng result qua NDJSON.

    Wire format (`application/x-ndjson`, mỗi dòng 1 JSON message):
        `{"type":"start","total":N,"timeout":T,"concurrency":C,"batch_timeout":B}`
        `{"type":"result","index":I,"proxy":...,"proxy_masked":...,"ok":...,`
        ` "reason":...,"latency_ms":...,"error":...}`  ← N dòng, THỨ TỰ HOÀN THÀNH
        `{"type":"done","total":N,"live":L,"elapsed_ms":E,"timed_out":bool}`

    FE dùng `index` để map ngược về row trong danh sách input (backend giữ
    thứ tự input sau dedupe qua `_prepare_batch_params`).

    Client abort (đóng connection giữa chừng) → generator dừng emit và
    cancel toàn bộ probe task pending → không tốn CPU probe tiếp.
    """
    started_at = time.perf_counter()
    unique_lines, endpoint, timeout, concurrency = _prepare_batch_params(
        payload, proxy_pool
    )
    batch_timeout = _compute_batch_timeout(len(unique_lines), concurrency, timeout)

    async def _generator() -> AsyncIterator[str]:
        yield _ndjson(
            {
                "type": "start",
                "total": len(unique_lines),
                "endpoint": endpoint,
                "timeout": timeout,
                "concurrency": concurrency,
                "batch_timeout": batch_timeout,
            }
        )

        if not unique_lines:
            yield _ndjson(
                {
                    "type": "done",
                    "total": 0,
                    "live": 0,
                    "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
                    "timed_out": False,
                }
            )
            return

        _logger.info(
            "[probe-batch-stream] start: %d proxies, endpoint=%s, timeout=%ss, "
            "concurrency=%d, batch_timeout=%.1fs",
            len(unique_lines),
            endpoint,
            timeout,
            concurrency,
            batch_timeout,
        )

        sem = asyncio.Semaphore(concurrency)
        # Queue chứa (index, ProxyProbeItemResult) — worker push, generator pull
        # rồi emit ra wire. `maxsize=0` = unbounded (số proxy tối đa 500, mỗi
        # item nhỏ ~200 bytes → tổng ~100KB, chấp nhận được).
        queue: asyncio.Queue[tuple[int, ProxyProbeItemResult]] = asyncio.Queue()
        completed: set[int] = set()

        async def _worker(index: int, line: str) -> None:
            item = await _probe_single_line(
                line, endpoint=endpoint, timeout=timeout, semaphore=sem
            )
            await queue.put((index, item))

        tasks = [
            asyncio.create_task(_worker(i, line))
            for i, line in enumerate(unique_lines)
        ]

        live_count = 0
        timed_out = False
        deadline = time.perf_counter() + batch_timeout

        try:
            while len(completed) < len(unique_lines):
                # Client disconnect → dừng emit, cancel tasks.
                if await request.is_disconnected():
                    _logger.info(
                        "[probe-batch-stream] client disconnected, cancelling"
                    )
                    break

                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    timed_out = True
                    break

                try:
                    index, item = await asyncio.wait_for(
                        queue.get(), timeout=min(remaining, 1.0)
                    )
                except asyncio.TimeoutError:
                    # Tick 1s để check disconnect + deadline — không phải
                    # batch timeout (deadline check ở đầu vòng lặp).
                    continue

                completed.add(index)
                if item.ok:
                    live_count += 1
                yield _ndjson(
                    {
                        "type": "result",
                        "index": index,
                        "proxy": item.proxy,
                        "proxy_masked": item.proxy_masked,
                        "ok": item.ok,
                        "reason": item.reason,
                        "latency_ms": item.latency_ms,
                        "error": item.error,
                    }
                )

            # Emit placeholder cho task chưa xong (batch timeout hoặc client
            # disconnect). Client disconnect thì stream dừng luôn — không
            # emit thêm. Batch timeout thì emit đầy đủ để FE thấy proxy nào
            # bị cắt sớm.
            if timed_out:
                for i, line in enumerate(unique_lines):
                    if i in completed:
                        continue
                    placeholder = ProxyProbeItemResult(
                        proxy=line,
                        proxy_masked=_mask_line(line),
                        ok=False,
                        reason="ip",
                        latency_ms=int(batch_timeout * 1000),
                        error="batch_timeout",
                    )
                    yield _ndjson(
                        {
                            "type": "result",
                            "index": i,
                            "proxy": placeholder.proxy,
                            "proxy_masked": placeholder.proxy_masked,
                            "ok": placeholder.ok,
                            "reason": placeholder.reason,
                            "latency_ms": placeholder.latency_ms,
                            "error": placeholder.error,
                        }
                    )
                _logger.warning(
                    "[probe-batch-stream] batch timeout %.1fs — %d/%d done",
                    batch_timeout,
                    len(completed),
                    len(unique_lines),
                )

            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            yield _ndjson(
                {
                    "type": "done",
                    "total": len(unique_lines),
                    "live": live_count,
                    "elapsed_ms": elapsed_ms,
                    "timed_out": timed_out,
                }
            )
            _logger.info(
                "[probe-batch-stream] done: %d live / %d total in %dms "
                "(timed_out=%s)",
                live_count,
                len(unique_lines),
                elapsed_ms,
                timed_out,
            )
        finally:
            # Cleanup: cancel mọi task chưa xong để không leak sau khi
            # generator return (client disconnect / exception / normal end).
            for task in tasks:
                if not task.done():
                    task.cancel()
            # Đợi cleanup để tránh "Task was destroyed but it is pending".
            await asyncio.gather(*tasks, return_exceptions=True)

    # `Cache-Control: no-cache` + `X-Accel-Buffering: no` để proxy trước FE
    # (VD nginx / Uvicorn reverse proxy) KHÔNG buffer response — client
    # nhận từng dòng NDJSON ngay khi backend flush.
    return StreamingResponse(
        _generator(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
