/**
 * usePushGate — Pinia store cho Push_Success_Gate.
 *
 * State runtime (do backend hold):
 *   - `enabled`: bật/tắt feature "success wait" (setting live).
 *   - `threshold`: ngưỡng success (setting live, 1..1000).
 *   - `counter`: số lần Telegram Push gửi thành công kể từ resume gần nhất.
 *   - `paused`: True khi `counter >= threshold` — scheduler đã dừng cấp
 *     slot cho job Push_Mode.
 *
 * Data flow:
 *   - `load()`: gọi `GET /api/notifications/telegram/push-gate` lấy snapshot.
 *   - SSE event `push_gate_updated`: replace snapshot (không merge từng field
 *     vì các field có ràng buộc lẫn nhau).
 *   - `resume()`: gọi `POST /api/notifications/telegram/push-gate/resume` →
 *     backend reset counter=0, paused=false, wake scheduler; response
 *     chứa snapshot mới → apply luôn (SSE event cũng sẽ tới sau nhưng
 *     apply ngay giúp UI phản hồi nhanh, tránh flicker).
 *
 * KHÔNG lưu vào localStorage (giống các store khác) — reload page thì
 * `load()` lại từ backend.
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

const API_BASE = '/api'

export interface PushGateSnapshot {
  enabled: boolean
  threshold: number
  counter: number
  paused: boolean
}

const DEFAULT_SNAPSHOT: PushGateSnapshot = {
  enabled: false,
  threshold: 10,
  counter: 0,
  paused: false,
}

export const usePushGateStore = defineStore('pushGate', () => {
  const snapshot = ref<PushGateSnapshot>({ ...DEFAULT_SNAPSHOT })
  const loading = ref<boolean>(false)
  const resuming = ref<boolean>(false)
  const error = ref<string | null>(null)

  /**
   * True khi cần hiển thị nút "Tiếp tục" — CHỈ dựa `paused`, không kèm
   * điều kiện `enabled`.
   *
   * Lý do: gate paused là state runtime độc lập với setting `enabled`.
   * Nếu user tắt setting `enabled=false` GIỮA lúc gate đang paused
   * (đã đủ N success trước đó), backend KHÔNG auto-reset counter/paused.
   * Scheduler vẫn skip job Push → nếu nút Tiếp tục cũng ẩn theo enabled,
   * user không có cách nào clear pause qua UI → job Push kẹt vĩnh viễn.
   *
   * Show nút miễn `paused=true`; sau khi user bấm, backend reset →
   * `paused=false` → nút biến mất tự nhiên.
   */
  const shouldShowResumeBanner = computed<boolean>(() => snapshot.value.paused)

  /** True khi cần hiển thị progress `counter/threshold` trong modal. */
  const shouldShowProgress = computed<boolean>(() => snapshot.value.enabled)

  async function load(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const res = await fetch(`${API_BASE}/notifications/telegram/push-gate`, {
        method: 'GET',
      })
      if (!res.ok) {
        // 503 khi push_gate chưa configured (backend chưa boot xong) —
        // không phải bug user, chỉ log rồi giữ default snapshot.
        if (res.status === 503) {
          console.warn('[pushGate] service not ready (503)')
          return
        }
        throw new Error(`HTTP ${res.status}`)
      }
      const body = (await res.json()) as PushGateSnapshot
      snapshot.value = normalise(body)
    } catch (ex) {
      error.value = ex instanceof Error ? ex.message : String(ex)
    } finally {
      loading.value = false
    }
  }

  async function resume(): Promise<void> {
    if (resuming.value) return
    resuming.value = true
    error.value = null
    try {
      const res = await fetch(
        `${API_BASE}/notifications/telegram/push-gate/resume`,
        { method: 'POST' },
      )
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`)
      }
      const body = (await res.json()) as PushGateSnapshot
      snapshot.value = normalise(body)
    } catch (ex) {
      error.value = ex instanceof Error ? ex.message : String(ex)
    } finally {
      resuming.value = false
    }
  }

  /**
   * Apply SSE event `push_gate_updated` — replace toàn bộ snapshot.
   *
   * KHÔNG merge từng field: các field có ràng buộc (VD `paused=true` thì
   * `counter >= threshold`), replace nguyên khối đảm bảo consistency.
   */
  function applyPushGateEvent(payload: unknown): void {
    if (typeof payload !== 'object' || payload === null) return
    snapshot.value = normalise(payload as Record<string, unknown>)
  }

  return {
    snapshot,
    loading,
    resuming,
    error,
    shouldShowResumeBanner,
    shouldShowProgress,
    load,
    resume,
    applyPushGateEvent,
  }
})

/**
 * Chuẩn hoá payload từ backend về shape ổn định.
 *
 * Backend luôn trả 4 field đúng type, nhưng defensive-normalise phòng
 * SSE payload bị corrupt (VD proxy log parse fail) — giữ default cho
 * field thiếu thay vì crash template.
 */
function normalise(raw: Record<string, unknown> | PushGateSnapshot): PushGateSnapshot {
  const source = raw as Record<string, unknown>
  const rawThreshold = source.threshold
  const threshold =
    typeof rawThreshold === 'number' && Number.isInteger(rawThreshold) && rawThreshold >= 1
      ? rawThreshold
      : DEFAULT_SNAPSHOT.threshold
  const rawCounter = source.counter
  const counter =
    typeof rawCounter === 'number' && Number.isInteger(rawCounter) && rawCounter >= 0
      ? rawCounter
      : 0
  return {
    enabled: typeof source.enabled === 'boolean' ? source.enabled : false,
    threshold,
    counter,
    paused: typeof source.paused === 'boolean' ? source.paused : false,
  }
}
