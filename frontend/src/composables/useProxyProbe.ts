/**
 * useProxyProbe — gọi `POST /api/proxy/probe-batch[/stream]` để test danh
 * sách proxy draft trong `ProxyConfigButton`.
 *
 * Cung cấp 2 mode:
 *   - `probeBatch()` — 1-shot JSON (backward compat, chờ hết batch mới trả
 *     Promise). Dùng cho test tự động hoặc caller không cần realtime.
 *   - `probeBatchStream()` — NDJSON stream, callback từng result ngay khi
 *     có. UI dùng path này để hiển thị progress live.
 *
 * Read-only ở backend: không đụng `ProxyPool` state (không mark_dead /
 * không mark_alive). Kết quả chỉ dùng cho UI preview trước khi user Lưu.
 *
 * KHÔNG dùng Pinia — state cục bộ của modal, không cần share cross-component.
 * Tách thành composable để test dễ (mock fetch) + tách concern khỏi
 * `ProxyConfigButton.vue` (component chỉ lo UI, không tự dựng fetch).
 */

import { ref, type Ref } from 'vue'

import { useClientId } from './useClientId'

const API_BASE = '/api'

/** Enum reason khớp `PROBE_REASON_*` + `format` từ backend. */
export type ProbeReason = 'ok' | 'auth' | 'ip' | 'format' | 'batch_timeout' | string

/** Kết quả probe 1 proxy — khớp `ProxyProbeItemResult` ở backend. */
export interface ProxyProbeResult {
  /** RAW line user đã gửi lên — dùng để map ngược về row trong UI. */
  proxy: string
  /** URL đã mask credential (an toàn cho display). */
  proxy_masked: string
  ok: boolean
  reason: ProbeReason
  latency_ms: number
  /** `null` khi `ok=true`. */
  error: string | null
}

interface ProxyProbeBatchResponse {
  results: ProxyProbeResult[]
  total: number
  live: number
  elapsed_ms: number
}

export interface ProxyProbeBatchOutput {
  results: ProxyProbeResult[]
  total: number
  live: number
  elapsedMs: number
  /** True nếu backend cắt batch do vượt `batch_timeout`. Chỉ set qua stream. */
  timedOut?: boolean
}

export interface ProxyProbeRequestOptions {
  /** Endpoint probe (default backend: https://api64.ipify.org). */
  endpoint?: string
  /** Timeout per-probe (giây). Backend clamp 3..30. */
  timeoutSeconds?: number
  /** Số probe song song. Backend clamp 1..100. */
  concurrency?: number
}

/** Callback trong quá trình streaming probe. Tất cả optional. */
export interface ProxyProbeStreamCallbacks {
  /** Emit khi backend confirm start — biết trước total để hiển thị progress. */
  onStart?: (info: {
    total: number
    endpoint: string
    timeout: number
    concurrency: number
    batchTimeout: number
  }) => void
  /**
   * Emit từng result ngay khi backend probe xong 1 proxy. `index` là vị trí
   * trong danh sách input SAU dedupe (backend giữ thứ tự input) — có thể
   * dùng để map về row nếu FE cũng dedupe theo cùng logic, HOẶC dùng
   * `result.proxy` (raw line) để lookup.
   */
  onResult?: (result: ProxyProbeResult, index: number) => void
  /** Client cancel via `AbortController.abort()` — cleanup UI state. */
  onAbort?: () => void
}

export function useProxyProbe() {
  const probing: Ref<boolean> = ref<boolean>(false)
  const lastError: Ref<string | null> = ref<string | null>(null)

  const clientId = useClientId()

  /**
   * Probe batch proxy — trả kết quả per proxy theo THỨ TỰ input (backend
   * đã dedupe + strip empty). Nếu backend fail HTTP (5xx / network), throw
   * để caller catch; validation lỗi (400) không expected ở endpoint này
   * vì Pydantic model chỉ có primitive fields (không có whitelist).
   *
   * KHÔNG có progress callback — nếu cần realtime, dùng `probeBatchStream`.
   */
  async function probeBatch(
    proxies: string[],
    options: ProxyProbeRequestOptions = {},
  ): Promise<ProxyProbeBatchOutput> {
    if (probing.value) {
      throw new Error('Probe already running — wait for it to finish before triggering a new one')
    }
    probing.value = true
    lastError.value = null

    try {
      const body: Record<string, unknown> = { proxies }
      if (options.endpoint !== undefined) body.endpoint = options.endpoint
      if (options.timeoutSeconds !== undefined) body.timeout_seconds = options.timeoutSeconds
      if (options.concurrency !== undefined) body.concurrency = options.concurrency

      const res = await fetch(`${API_BASE}/proxy/probe-batch`, {
        method: 'POST',
        headers: {
          'X-Client-Id': clientId,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(body),
      })

      if (!res.ok) {
        const text = await res.text().catch(() => '')
        const msg = `HTTP ${res.status}${text ? `: ${text.slice(0, 200)}` : ''}`
        lastError.value = msg
        throw new Error(msg)
      }

      const parsed = (await res.json()) as ProxyProbeBatchResponse
      return {
        results: parsed.results,
        total: parsed.total,
        live: parsed.live,
        elapsedMs: parsed.elapsed_ms,
      }
    } finally {
      probing.value = false
    }
  }

  /**
   * Probe batch qua NDJSON stream — callback từng result ngay khi có.
   *
   * Wire format (mỗi dòng 1 JSON):
   *   - `{type:"start", total, endpoint, timeout, concurrency, batch_timeout}`
   *   - `{type:"result", index, proxy, proxy_masked, ok, reason, latency_ms, error}`
   *   - `{type:"done", total, live, elapsed_ms, timed_out}`
   *
   * Client abort: truyền `signal` từ `AbortController`, `signal.abort()` sẽ
   * đóng fetch connection → backend detect qua `request.is_disconnected()`
   * → cancel probe task pending → giải phóng CPU.
   *
   * Returns Promise resolve với snapshot cuối (giống `probeBatch`), reject
   * nếu HTTP status ≠ 200 hoặc stream disconnect trước khi có `done`.
   */
  async function probeBatchStream(
    proxies: string[],
    options: ProxyProbeRequestOptions = {},
    callbacks: ProxyProbeStreamCallbacks = {},
    signal?: AbortSignal,
  ): Promise<ProxyProbeBatchOutput> {
    if (probing.value) {
      throw new Error('Probe already running — wait for it to finish before triggering a new one')
    }
    probing.value = true
    lastError.value = null

    const collected: ProxyProbeResult[] = []
    let live = 0
    let elapsedMs = 0
    let total = 0
    let timedOut = false

    try {
      const body: Record<string, unknown> = { proxies }
      if (options.endpoint !== undefined) body.endpoint = options.endpoint
      if (options.timeoutSeconds !== undefined) body.timeout_seconds = options.timeoutSeconds
      if (options.concurrency !== undefined) body.concurrency = options.concurrency

      const res = await fetch(`${API_BASE}/proxy/probe-batch/stream`, {
        method: 'POST',
        headers: {
          'X-Client-Id': clientId,
          'Content-Type': 'application/json',
          Accept: 'application/x-ndjson',
        },
        body: JSON.stringify(body),
        signal,
      })

      if (!res.ok) {
        const text = await res.text().catch(() => '')
        const msg = `HTTP ${res.status}${text ? `: ${text.slice(0, 200)}` : ''}`
        lastError.value = msg
        throw new Error(msg)
      }

      if (!res.body) {
        throw new Error('Stream response has no body')
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder('utf-8')
      let buffer = ''

      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        // Split NDJSON theo `\n`. Phần dư (chưa có newline) giữ trong buffer.
        let newlineIdx: number
        while ((newlineIdx = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, newlineIdx).trim()
          buffer = buffer.slice(newlineIdx + 1)
          if (!line) continue

          let msg: {
            type: string
            [key: string]: unknown
          }
          try {
            msg = JSON.parse(line) as typeof msg
          } catch (parseErr) {
            // Server không nên emit line lỗi format — log và tiếp tục
            // parse các line sau (fail-soft).
            // eslint-disable-next-line no-console
            console.warn('[useProxyProbe] failed to parse NDJSON line', line, parseErr)
            continue
          }

          if (msg.type === 'start') {
            total = (msg.total as number) ?? 0
            callbacks.onStart?.({
              total,
              endpoint: (msg.endpoint as string) ?? '',
              timeout: (msg.timeout as number) ?? 0,
              concurrency: (msg.concurrency as number) ?? 0,
              batchTimeout: (msg.batch_timeout as number) ?? 0,
            })
          } else if (msg.type === 'result') {
            const result: ProxyProbeResult = {
              proxy: msg.proxy as string,
              proxy_masked: msg.proxy_masked as string,
              ok: msg.ok as boolean,
              reason: msg.reason as ProbeReason,
              latency_ms: msg.latency_ms as number,
              error: (msg.error as string | null) ?? null,
            }
            const index = (msg.index as number) ?? collected.length
            collected.push(result)
            if (result.ok) live += 1
            callbacks.onResult?.(result, index)
          } else if (msg.type === 'done') {
            total = (msg.total as number) ?? total
            live = (msg.live as number) ?? live
            elapsedMs = (msg.elapsed_ms as number) ?? 0
            timedOut = Boolean(msg.timed_out)
          }
        }
      }

      return {
        results: collected,
        total,
        live,
        elapsedMs,
        timedOut,
      }
    } catch (err) {
      // AbortError = user chủ động cancel — không phải lỗi hệ thống, gọi
      // callback riêng để UI reset trạng thái nhẹ nhàng.
      if (err instanceof DOMException && err.name === 'AbortError') {
        callbacks.onAbort?.()
        return {
          results: collected,
          total,
          live,
          elapsedMs,
          timedOut,
        }
      }
      const msg = err instanceof Error ? err.message : String(err)
      lastError.value = msg
      throw err
    } finally {
      probing.value = false
    }
  }

  return {
    probing,
    lastError,
    probeBatch,
    probeBatchStream,
  }
}
