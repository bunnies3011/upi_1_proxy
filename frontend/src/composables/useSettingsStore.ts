/**
 * useSettingsStore — Pinia store cache Settings hiện tại + gọi API ghi
 * (Requirement 11.5, 12.7).
 *
 * Nguyên tắc "commit-after-confirm" (Requirement 12.7):
 * KHÔNG cập nhật `settings` state trước khi API trả 200. Nếu API trả 400
 * (`settings_validation_error`), store CHỈ ghi vào `error` và giữ nguyên
 * `settings` — Frontend_App do đó "giữ nguyên giá trị hiển thị trước đó"
 * một cách tự nhiên (component bind vào `settings[key]`, chưa bao giờ
 * thấy giá trị bị từ chối). Đây là mô hình đơn giản hơn optimistic-rollback:
 * không có state trung gian cần revert nếu request fail giữa chừng.
 *
 * KHÔNG dùng `localStorage` cho cache (Requirement 11.5): cache chỉ tồn
 * tại trong runtime; reload page → gọi lại `loadAll()`.
 */

import { ref, type Ref } from 'vue'
import { defineStore } from 'pinia'

import { useClientId } from './useClientId'

// Backend mount routes tại `/api/*` (xem `backend/app/api/routes_settings.py`).
// Đường dẫn tương đối — Vite dev server proxy hoặc production reverse proxy
// định tuyến sang FastAPI.
const API_BASE = '/api'

// Body ổn định do backend trả khi ghi Settings vi phạm whitelist/type
// constraint (xem `_settings_validation_response` trong routes_settings.py).
// Chuỗi `error` cố định — KHÔNG đổi mà không cập nhật consumer.
const SETTINGS_VALIDATION_ERROR_CODE = 'settings_validation_error'

export interface SettingsValidationError {
  /** Key vi phạm (đối với `bulkUpdate`, là key đầu tiên bị `SettingsRepository.bulk_set` từ chối). */
  key: string
  /** Mô tả cụ thể lý do từ chối (whitelist miss / type sai / enum miss / range sai). */
  reason: string
}

interface ValidationErrorBody {
  error?: string
  key?: string
  reason?: string
}

interface SettingsListResponse {
  settings: Record<string, unknown>
}

interface SettingUpdateResponse {
  key: string
  value: unknown
}

interface SettingsBulkResponse {
  applied: Record<string, unknown>
}

/**
 * Chuẩn hoá lỗi validate 400 sang shape `SettingsValidationError` ổn định.
 *
 * Backend luôn trả `{"error": "settings_validation_error", "key", "reason"}`
 * nhưng ta vẫn phòng thủ nếu body malformed — không để `undefined` lọt vào
 * state, tránh component hiển thị "undefined" trên UI.
 */
function normaliseValidationError(
  body: ValidationErrorBody,
  fallbackKey: string,
): SettingsValidationError {
  return {
    key: typeof body.key === 'string' && body.key.length > 0 ? body.key : fallbackKey,
    reason:
      typeof body.reason === 'string' && body.reason.length > 0
        ? body.reason
        : 'unknown validation error',
  }
}

export const useSettingsStore = defineStore('settings', () => {
  // Cache Settings hiện tại — key → value (kiểu tuỳ theo TypeConstraint của
  // từng key ở backend, ví dụ `int`, `list-of-string`, `bool`...).
  const settings: Ref<Record<string, unknown>> = ref<Record<string, unknown>>({})

  // Cờ "đang gọi API" để component có thể disable input / hiện spinner.
  const loading: Ref<boolean> = ref<boolean>(false)

  // Lỗi validation gần nhất — `null` khi request thành công hoặc chưa gọi.
  const error: Ref<SettingsValidationError | null> = ref<SettingsValidationError | null>(null)

  const clientId = useClientId()

  /**
   * Build header cho fetch: gộp `X-Client-Id` (nhận diện tab hiện tại — để
   * server broadcast SSE lại có `source_client_id`) và `Content-Type` (nếu
   * request có body JSON). Tránh gửi `Content-Type` cho GET để không kích
   * hoạt CORS preflight thừa.
   */
  function buildHeaders(withJsonBody: boolean): Record<string, string> {
    const headers: Record<string, string> = {
      'X-Client-Id': clientId,
    }
    if (withJsonBody) {
      headers['Content-Type'] = 'application/json'
    }
    return headers
  }

  /**
   * Áp dụng update từ SSE `setting_updated` event vào cache local.
   *
   * Chống feedback loop: nếu `sourceClientId` = client_id của tab hiện tại,
   * bỏ qua (client vừa PUT thì cache đã update bởi `updateKey` — không cần
   * ghi đè lại, tránh cursor jump nếu binding vào textarea).
   */
  function applySettingUpdatedEvent(
    key: string,
    value: unknown,
    sourceClientId: string | null | undefined,
  ): void {
    if (typeof sourceClientId === 'string' && sourceClientId === clientId) {
      return
    }
    settings.value = { ...settings.value, [key]: value }
  }

  /**
   * Nạp toàn bộ (hoặc theo `prefix`) Settings từ backend, thay thế cache
   * cũ bằng snapshot mới.
   *
   * Fail-fast: mọi HTTP status khác 200 đều throw để caller (component)
   * có thể hiển thị lỗi tổng quát. Riêng lỗi validate 400 KHÔNG áp dụng
   * cho GET nên không cần map sang `error` state.
   */
  async function loadAll(prefix?: string): Promise<void> {
    const query = prefix ? `?prefix=${encodeURIComponent(prefix)}` : ''
    loading.value = true
    try {
      const res = await fetch(`${API_BASE}/settings${query}`, {
        method: 'GET',
        headers: buildHeaders(false),
      })
      if (!res.ok) {
        throw new Error(`Failed to load settings: HTTP ${res.status}`)
      }
      const body = (await res.json()) as SettingsListResponse
      // Thay thế toàn bộ cache — không merge, để đồng bộ với backend
      // (key đã bị xoá ở backend cũng biến mất ở FE).
      settings.value = { ...body.settings }
    } finally {
      loading.value = false
    }
  }

  /**
   * Ghi 1 key qua `PUT /api/settings/{key}`.
   *
   * Return `true` khi backend xác nhận 200 (cache đã cập nhật theo giá trị
   * canonical đọc lại từ store), `false` khi backend trả 400 với
   * `settings_validation_error` (cache KHÔNG đổi, `error` state đã set).
   * Mọi HTTP status khác → throw để caller xử lý (lỗi mạng, 401, 5xx...).
   */
  async function updateKey(key: string, value: unknown): Promise<boolean> {
    loading.value = true
    error.value = null
    try {
      const res = await fetch(`${API_BASE}/settings/${encodeURIComponent(key)}`, {
        method: 'PUT',
        headers: buildHeaders(true),
        body: JSON.stringify({ value }),
      })

      if (res.status === 200) {
        const body = (await res.json()) as SettingUpdateResponse
        // Commit sau khi confirm: dùng value canonical do backend đọc lại
        // từ store (`SettingsRepository.get`) — có thể khác payload thô
        // nếu repository normalise trong tương lai.
        settings.value = { ...settings.value, [body.key]: body.value }
        return true
      }

      if (res.status === 400) {
        const body = (await res.json()) as ValidationErrorBody
        if (body.error === SETTINGS_VALIDATION_ERROR_CODE) {
          error.value = normaliseValidationError(body, key)
          // KHÔNG cập nhật settings — component tiếp tục hiển thị giá trị cũ.
          return false
        }
        // 400 nhưng không phải validate error → fail-fast.
        throw new Error(`Failed to update setting ${key}: HTTP 400 (unexpected body)`)
      }

      throw new Error(`Failed to update setting ${key}: HTTP ${res.status}`)
    } finally {
      loading.value = false
    }
  }

  /**
   * Ghi nhiều key trong 1 transaction qua `POST /api/settings/bulk`.
   *
   * Atomic ở backend (Requirement 11.2): nếu 1 item vi phạm, KHÔNG có item
   * nào được ghi — do đó khi 400, store cũng không cập nhật item nào.
   * Return `true`/`false` giống `updateKey`.
   */
  async function bulkUpdate(items: Record<string, unknown>): Promise<boolean> {
    loading.value = true
    error.value = null
    try {
      const res = await fetch(`${API_BASE}/settings/bulk`, {
        method: 'POST',
        headers: buildHeaders(true),
        body: JSON.stringify({ items }),
      })

      if (res.status === 200) {
        const body = (await res.json()) as SettingsBulkResponse
        // Merge (không replace) — bulkUpdate có thể chỉ ghi 1 subset,
        // các key khác trong cache vẫn giữ nguyên.
        settings.value = { ...settings.value, ...body.applied }
        return true
      }

      if (res.status === 400) {
        const body = (await res.json()) as ValidationErrorBody
        if (body.error === SETTINGS_VALIDATION_ERROR_CODE) {
          // Fallback key = key đầu tiên trong items (nếu backend không trả
          // key cụ thể vì lý do nào đó); đủ tốt cho hiển thị lỗi UI.
          const firstKey = Object.keys(items)[0] ?? ''
          error.value = normaliseValidationError(body, firstKey)
          return false
        }
        throw new Error('Failed to bulk update settings: HTTP 400 (unexpected body)')
      }

      throw new Error(`Failed to bulk update settings: HTTP ${res.status}`)
    } finally {
      loading.value = false
    }
  }

  /** Reset `error` state — component gọi khi user đóng thông báo lỗi. */
  function clearError(): void {
    error.value = null
  }

  return {
    settings,
    loading,
    error,
    loadAll,
    updateKey,
    bulkUpdate,
    clearError,
    applySettingUpdatedEvent,
  }
})

// Export const để test hoặc code khác cần reference (không phải để dùng bừa).
export const SETTINGS_VALIDATION_ERROR = SETTINGS_VALIDATION_ERROR_CODE
