/**
 * useSse — kết nối SSE stream `GET /api/events/stream` và dispatch event
 * `job_status` / `job_log` vào `useJobsStore` (Requirements 8.8, 12.5).
 *
 * Composable này dùng `fetch(...)` + `ReadableStream` reader tự parse cú
 * pháp `text/event-stream` theo đúng payload backend phát:
 *
 *     event: <type>\n
 *     data: <json>\n
 *     \n
 *
 * Các event tối thiểu cần dispatch (theo `core/sse.py` backend):
 *
 * - `job_status` — payload `{"job_id": str, "status": str, ...extra}`
 *   → `jobsStore.applyStatusEvent(payload)`
 * - `job_log` — payload `{"job_id": str, "message": str, ...extra}`
 *   → `jobsStore.applyLogEvent(payload)`
 *
 * Ngoài ra backend còn phát dòng comment `:\n\n` mỗi 15 giây làm keepalive
 * (chống proxy đóng idle connection). Comment SSE theo spec HTML5 bị client
 * bỏ qua — parser dưới đây skip mọi dòng bắt đầu bằng `:` để không tạo
 * event giả `applyStatusEvent(...)` / `applyLogEvent(...)`.
 *
 * Auto-reconnect: khi fetch fail (network error, 401, stream đóng bất
 * thường) và user KHÔNG chủ động `stop()`, composable chờ 3 giây rồi thử
 * lại — đơn giản, không exponential backoff (backend chạy local, latency
 * thấp, không cần jitter phức tạp).
 */

import { getCurrentScope, onScopeDispose, ref, type Ref } from 'vue'
import {
  useJobsStore,
  type JobLogEvent,
  type JobNotifiedEvent,
  type JobStatusEvent,
} from './useJobsStore'
import { useLiveQrGateStore } from './useLiveQrGate'
import { usePushGateStore } from './usePushGate'
import { useSettingsStore } from './useSettingsStore'

/** Endpoint SSE — cố định theo Requirement 8.8 (design.md phần "SSE một chiều đơn giản"). */
const SSE_URL = '/api/events/stream'

/** Delay giữa các lần thử reconnect (ms) — cố định 3s theo yêu cầu task. */
const RECONNECT_DELAY_MS = 3000

/**
 * Cap số event tối đa nằm chờ flush trong `eventQueue`.
 *
 * Lý do: khi tab bị hidden, `requestAnimationFrame` bị browser pause hoàn
 * toàn (Chrome/Firefox/Safari đều throttle > 1s hoặc dừng hẳn). Tuy nhiên
 * `fetch` SSE stream VẪN tiếp tục nhận event ở background — `eventQueue`
 * grow không giới hạn nếu backend log storm (VD nhiều job chạy song song).
 * Khi user quay lại tab → RAF fire → flush 1 batch khổng lồ → jank UI và
 * đỉnh RAM cao.
 *
 * Với cap này, khi hàng đợi vượt ngưỡng, event CŨ NHẤT bị drop (ring-buffer
 * FIFO). Trade-off chấp nhận: log realtime của phiên tab-hidden có thể mất
 * vài dòng đầu; state cuối vẫn đúng vì backend luôn broadcast trạng thái
 * MỚI NHẤT của mỗi job (job_status là idempotent snapshot, không cumulative
 * — miss dòng cũ không hỏng state). Log entries chi tiết đã persist SQLite
 * ở backend, user muốn xem đầy đủ chỉ cần `loadDetail` từ LogPanel.
 *
 * Giá trị ~5000 = 500 log/job × 10 job worst case cho phiên hidden dài.
 */
const MAX_EVENT_QUEUE_SIZE = 5000

/**
 * Fallback interval (ms) khi tab hidden — RAF bị pause nên cần setTimeout
 * để drain queue chậm, tránh grow đến cap và mất event.
 *
 * 1000ms cân bằng: đủ chậm để không waste CPU khi user không xem tab, đủ
 * nhanh để chi phí queue growth thấp (backend log storm ~50 event/s × 1s
 * = 50 event/lần drain — dưới cap rất nhiều).
 */
const HIDDEN_FLUSH_INTERVAL_MS = 1000

export interface UseSseReturn {
  /** True khi fetch stream đã mở và đang đọc; false khi chưa mở, hoặc đã đóng/lỗi. */
  connected: Ref<boolean>
  /** Message lỗi gần nhất (null khi không có lỗi hoặc user chủ động stop). */
  error: Ref<string | null>
  /** Mở kết nối SSE. Idempotent — gọi nhiều lần không tạo thêm nhiều connection. */
  start: () => void
  /** Đóng kết nối SSE, hủy mọi reconnect timer đang chờ. */
  stop: () => void
}

/**
 * Parse 1 event block đã được cắt theo `\n\n`.
 *
 * Trả về `null` nếu block là keepalive/comment/không có `data:` — caller
 * phải bỏ qua, KHÔNG dispatch để tránh gọi `applyStatusEvent({})` giả.
 *
 * Tuân thủ SSE parsing rules tối thiểu (HTML5 spec — mục "Interpreting an
 * event stream"): field-name là chuỗi trước dấu `:` đầu tiên, value là
 * phần còn lại; nếu value bắt đầu bằng 1 space thì bỏ 1 space đó; các
 * dòng bắt đầu bằng `:` là comment, bỏ qua; nhiều dòng `data:` trong 1
 * event được nối bằng `\n` (backend hiện tại chỉ phát 1 dòng data,
 * nhưng parser vẫn support đa dòng để forward-compatible).
 */
function parseEventBlock(block: string): { eventType: string; data: string } | null {
  let eventType = 'message' // SSE default nếu không có field `event:` (spec HTML5).
  const dataLines: string[] = []

  for (const rawLine of block.split('\n')) {
    if (rawLine.length === 0) {
      continue
    }
    if (rawLine.startsWith(':')) {
      // Comment (keepalive `:\n\n` của backend, hoặc bất kỳ comment nào khác).
      continue
    }

    const colonIdx = rawLine.indexOf(':')
    if (colonIdx === -1) {
      // Theo spec, dòng không có `:` được xử lý như field-name với value rỗng.
      // Backend hiện tại không phát dạng này — bỏ qua an toàn.
      continue
    }

    const field = rawLine.slice(0, colonIdx)
    let value = rawLine.slice(colonIdx + 1)
    // SSE spec: nếu ký tự đầu của value là space thì loại bỏ đúng 1 space.
    if (value.startsWith(' ')) {
      value = value.slice(1)
    }

    if (field === 'event') {
      eventType = value
    } else if (field === 'data') {
      dataLines.push(value)
    }
    // Bỏ qua `id`, `retry` — backend không dùng, và composable này quản lý
    // reconnect delay riêng nên không tôn trọng `retry:` từ server.
  }

  if (dataLines.length === 0) {
    return null
  }

  return { eventType, data: dataLines.join('\n') }
}

export function useSse(): UseSseReturn {
  const connected = ref<boolean>(false)
  const error = ref<string | null>(null)

  const jobsStore = useJobsStore()
  const settingsStore = useSettingsStore()
  const pushGateStore = usePushGateStore()
  const liveQrGateStore = useLiveQrGateStore()

  /** Controller để hủy fetch hiện tại khi `stop()` hoặc bắt đầu reconnect. */
  let abortController: AbortController | null = null
  /** Timer chờ reconnect (null nếu không có lần reconnect nào đang được lên lịch). */
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  /** True khi user chủ động gọi `stop()` — chặn auto-reconnect cho tới khi `start()` lại. */
  let manuallyStopped = false
  /** True khi đang có 1 connection loop chạy — dùng để `start()` idempotent. */
  let running = false

  /**
   * Queue event chờ dispatch, flush theo `requestAnimationFrame` để coalesce
   * nhiều SSE mutation vào 1 paint frame (~16ms). Trước fix này, mỗi event
   * gọi ngay `jobsStore.applyXxxEvent` → `jobs.value.set` → trigger toàn bộ
   * computed (visibleJobs O(N) + sort, stats, counts, filtered, displayIndex,
   * plusJobs). Với backend bắn 20-50 log/s × N job đang chạy, chi phí re-eval
   * chồng chất → allocation storm khiến GC không kịp reclaim → RAM tăng đều
   * theo thời gian sử dụng.
   *
   * Batch qua RAF → nhiều event trong cùng frame chỉ tạo 1 lần re-eval của
   * mỗi computed downstream (Vue lazy-invalidates: computed chỉ tính lại khi
   * template read; nhiều set trong 1 tick chỉ dirty 1 lần).
   */
  const eventQueue: Array<{ eventType: string; data: string }> = []
  /** Handle RAF khi tab visible, hoặc setTimeout khi tab hidden — mutually exclusive. */
  let rafHandle: number | null = null
  /** Handle setTimeout drain-loop khi tab hidden. Riêng biệt với rafHandle
   *  để tránh 2 flush schedule song song (RAF resume + timeout cùng fire). */
  let hiddenFlushTimer: ReturnType<typeof setTimeout> | null = null
  /** Đếm số event đã drop do queue vượt cap — log 1 lần khi có drop mới. */
  let droppedEventCount = 0

  /** True khi document ở trạng thái `hidden` (browser API). SSR/test không
   *  có `document` → mặc định false để giữ hành vi cũ. */
  const isDocumentHidden = (): boolean => {
    return typeof document !== 'undefined' && document.visibilityState === 'hidden'
  }

  const scheduleFlush = (): void => {
    if (rafHandle !== null || hiddenFlushTimer !== null) return
    // Tab hidden → RAF bị browser pause → phải dùng setTimeout để drain
    // queue đều đặn, tránh event tích tụ đến cap. Khi tab visible trở lại,
    // `handleVisibilityChange` gọi flush tức thì rồi resume RAF path.
    if (isDocumentHidden()) {
      hiddenFlushTimer = setTimeout(() => {
        hiddenFlushTimer = null
        flushEventQueue()
      }, HIDDEN_FLUSH_INTERVAL_MS)
      return
    }
    if (typeof requestAnimationFrame === 'function') {
      rafHandle = requestAnimationFrame(flushEventQueue)
    } else {
      // JSDOM / SSR fallback — dùng setTimeout để test vẫn chạy được.
      rafHandle = window.setTimeout(() => flushEventQueue(), 16) as unknown as number
    }
  }

  const flushEventQueue = (): void => {
    rafHandle = null
    if (eventQueue.length === 0) return
    // Splice full → clone snapshot rồi reset. Nếu dispatch bên dưới enqueue
    // thêm event mới (edge case race), event mới đã có scheduleFlush riêng.
    const batch = eventQueue.splice(0, eventQueue.length)
    for (const { eventType, data } of batch) {
      dispatchEventNow(eventType, data)
    }
  }

  const cancelFlush = (): void => {
    if (rafHandle !== null) {
      if (typeof cancelAnimationFrame === 'function') {
        cancelAnimationFrame(rafHandle)
      } else {
        window.clearTimeout(rafHandle)
      }
      rafHandle = null
    }
    if (hiddenFlushTimer !== null) {
      clearTimeout(hiddenFlushTimer)
      hiddenFlushTimer = null
    }
    eventQueue.length = 0
    droppedEventCount = 0
  }

  /**
   * Khi document chuyển từ hidden → visible: hủy timer drain-slow và flush
   * ngay lập tức để UI đồng bộ trạng thái mới nhất mà không đợi thêm 1s.
   * Khi visible → hidden: hủy RAF pending (browser sẽ không fire nữa) và
   * schedule lại qua setTimeout nếu còn event trong queue.
   */
  const handleVisibilityChange = (): void => {
    if (isDocumentHidden()) {
      // Chuyển sang hidden — hủy RAF (browser sẽ pause anyway) rồi
      // schedule lại qua path setTimeout nếu còn event.
      if (rafHandle !== null) {
        if (typeof cancelAnimationFrame === 'function') {
          cancelAnimationFrame(rafHandle)
        } else {
          window.clearTimeout(rafHandle)
        }
        rafHandle = null
      }
      if (eventQueue.length > 0) {
        scheduleFlush()
      }
      return
    }
    // Chuyển sang visible — hủy timer drain-slow, flush ngay để UI catch-up
    // trạng thái mới nhất (nếu tab bị ẩn lâu, backend đã broadcast nhiều
    // status change trong lúc đó).
    if (hiddenFlushTimer !== null) {
      clearTimeout(hiddenFlushTimer)
      hiddenFlushTimer = null
    }
    if (eventQueue.length > 0) {
      flushEventQueue()
    }
  }

  const clearReconnectTimer = (): void => {
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
  }

  const scheduleReconnect = (): void => {
    if (manuallyStopped) {
      return
    }
    clearReconnectTimer()
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      void openConnection()
    }, RECONNECT_DELAY_MS)
  }

  /**
   * Enqueue event vào buffer — parse/dispatch thật sự sẽ chạy trong RAF
   * callback (`flushEventQueue`). KHÔNG parse JSON ở đây để giữ enqueue
   * cheap; parse chỉ chạy khi thực sự flush (tránh parse job đã bị stop
   * cancel trước khi flush — hiếm nhưng free).
   *
   * Cap `MAX_EVENT_QUEUE_SIZE`: khi queue vượt ngưỡng (tab hidden lâu +
   * backend log storm), drop event CŨ NHẤT — FIFO ring-buffer. Log 1 dòng
   * cảnh báo mỗi khi có drop mới để dev thấy trong console. `job_status`
   * là idempotent snapshot (server luôn broadcast state hiện tại), miss
   * dòng cũ không hỏng state cuối; log entries chi tiết đã persist SQLite
   * ở BE, user cần xem đầy đủ chỉ cần `loadDetail` từ LogPanel.
   */
  const enqueueEvent = (eventType: string, dataJson: string): void => {
    eventQueue.push({ eventType, data: dataJson })
    if (eventQueue.length > MAX_EVENT_QUEUE_SIZE) {
      const overflow = eventQueue.length - MAX_EVENT_QUEUE_SIZE
      eventQueue.splice(0, overflow)
      droppedEventCount += overflow
      // Log throttled — mỗi 1000 event drop mới in 1 dòng để không spam
      // console khi hidden lâu. Modulo 1000 để không mất milestone.
      if (droppedEventCount % 1000 < overflow) {
        console.warn(
          `[useSse] event queue overflow — dropped ${droppedEventCount} oldest event(s) since tab last visible`,
        )
      }
    }
    scheduleFlush()
  }

  const dispatchEventNow = (eventType: string, dataJson: string): void => {
    let payload: unknown
    try {
      payload = JSON.parse(dataJson)
    } catch (parseErr) {
      // Backend luôn phát JSON hợp lệ (json.dumps). Log-and-drop 1 event
      // hỏng thay vì kill toàn stream — SSE là best-effort observability.
      console.error('[useSse] failed to parse event data as JSON:', parseErr, dataJson)
      return
    }

    if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) {
      // Payload không phải object → không đúng contract backend, bỏ qua.
      return
    }

    const record = payload as Record<string, unknown>

    if (eventType === 'job_status') {
      jobsStore.applyStatusEvent(record as unknown as JobStatusEvent)
    } else if (eventType === 'job_log') {
      jobsStore.applyLogEvent(record as unknown as JobLogEvent)
    } else if (eventType === 'job_notified') {
      jobsStore.applyNotifiedEvent(record as unknown as JobNotifiedEvent)
    } else if (eventType === 'setting_updated') {
      const key = record.key
      if (typeof key === 'string') {
        settingsStore.applySettingUpdatedEvent(
          key,
          record.value,
          typeof record.source_client_id === 'string'
            ? record.source_client_id
            : null,
        )
      }
    } else if (eventType === 'push_gate_updated') {
      pushGateStore.applyPushGateEvent(record)
    } else if (eventType === 'live_qr_gate_updated') {
      liveQrGateStore.applyLiveQrGateEvent(record)
    }
    // Event type khác (`message` default, hoặc type mới trong tương lai):
    // bỏ qua để forward-compatible — không throw, không log noise.
  }

  const openConnection = async (): Promise<void> => {
    if (manuallyStopped) {
      return
    }
    if (running) {
      // Đã có 1 loop khác đang chạy — không mở thêm connection song song.
      return
    }
    running = true

    // Đảm bảo không còn AbortController cũ dangling (an toàn — cleanup phòng thủ).
    if (abortController !== null) {
      abortController.abort()
    }
    const controller = new AbortController()
    abortController = controller

    error.value = null

    try {
      const response = await fetch(SSE_URL, {
        method: 'GET',
        headers: {
          Accept: 'text/event-stream',
        },
        signal: controller.signal,
        // SSE không được cache — Cache-Control 'no-store' phía client để chắc chắn.
        cache: 'no-store',
      })

      if (!response.ok) {
        throw new Error(`SSE fetch failed with HTTP ${response.status}`)
      }
      if (response.body === null) {
        throw new Error('SSE response has no readable body stream')
      }

      connected.value = true

      const reader = response.body.getReader()
      const decoder = new TextDecoder('utf-8')
      let buffer = ''

      // Vòng lặp đọc chunk & tách event block theo delimiter `\n\n`.
      // Backend phát cứng `\n\n` (xem `core.sse.format_sse_event`), nên
      // client chỉ cần tách LF-LF, không cần normalize CRLF.
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { done, value } = await reader.read()
        if (done) {
          // Server đóng stream không rõ lý do → coi như lỗi để trigger reconnect.
          throw new Error('SSE stream closed by server')
        }
        buffer += decoder.decode(value, { stream: true })

        let sepIdx = buffer.indexOf('\n\n')
        while (sepIdx !== -1) {
          const block = buffer.slice(0, sepIdx)
          buffer = buffer.slice(sepIdx + 2)
          const parsed = parseEventBlock(block)
          if (parsed !== null) {
            enqueueEvent(parsed.eventType, parsed.data)
          }
          sepIdx = buffer.indexOf('\n\n')
        }
      }
    } catch (fetchErr) {
      // Nếu user chủ động stop, AbortError là expected → không set error/reconnect.
      if (
        manuallyStopped ||
        (fetchErr instanceof DOMException && fetchErr.name === 'AbortError')
      ) {
        return
      }
      const message = fetchErr instanceof Error ? fetchErr.message : String(fetchErr)
      error.value = message
      scheduleReconnect()
    } finally {
      connected.value = false
      running = false
      // Chỉ clear reference khi controller hiện tại chính là controller đã abort/hoàn tất.
      // Nếu `stop()` đã thay abortController = null thì không đụng lại.
      if (abortController === controller) {
        abortController = null
      }
    }
  }

  const start = (): void => {
    manuallyStopped = false
    clearReconnectTimer()
    if (running) {
      // Đã đang chạy → start() là no-op (idempotent).
      return
    }
    void openConnection()
  }

  const stop = (): void => {
    manuallyStopped = true
    clearReconnectTimer()
    cancelFlush()
    if (abortController !== null) {
      abortController.abort()
      abortController = null
    }
    connected.value = false
  }

  // Đăng ký listener `visibilitychange` để chuyển qua path setTimeout khi
  // tab hidden (RAF bị browser pause) và flush ngay khi tab visible trở
  // lại. Guard `typeof document !== 'undefined'` cho SSR/JSDOM không có
  // Document API. Listener PHẢI được gỡ trong `stop()` (qua
  // `onScopeDispose`) để không dangling khi component unmount.
  const visibilityListenerAttached =
    typeof document !== 'undefined' &&
    typeof document.addEventListener === 'function'
  if (visibilityListenerAttached) {
    document.addEventListener('visibilitychange', handleVisibilityChange)
  }

  // Tự động cleanup khi effect scope (component / setup) unmount, tránh
  // leak fetch stream nếu user quên gọi `stop()` thủ công. `onScopeDispose`
  // không throw khi gọi ngoài scope — chỉ warning — nên guard bằng
  // `getCurrentScope()` để giữ composable dùng được cả ngoài Vue scope
  // (ví dụ trong test unit chạy plain).
  if (getCurrentScope() !== undefined) {
    onScopeDispose(() => {
      stop()
      if (visibilityListenerAttached) {
        document.removeEventListener('visibilitychange', handleVisibilityChange)
      }
    })
  }

  return { connected, error, start, stop }
}
