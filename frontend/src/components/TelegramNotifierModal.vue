<!--
  TelegramNotifierModal — Modal cấu hình bot Telegram cho CẢ 2 mode vận hành.

  Namespace `telegram.*` (khớp constraint đăng ký ở
  `backend/app/notifiers/telegram/__init__.py` sau khi tái cấu trúc):
    - telegram.bot_token                              (string, dùng chung)
    - telegram.mode                                   (enum push|pull)
    - telegram.polling_enabled                        (bool — bot lắng nghe)
    - telegram.push_mode.enabled                      (bool)
    - telegram.push_mode.send_photo                   (bool — kèm ảnh QR)
    - telegram.push_mode.chat_targets                 (list_object)
    - telegram.pull_mode.max_concurrent_jobs_per_user (int 1..20)
    - telegram.pull_mode.allowed_chat_ids             (list_str)

  Data flow:
    - Load: `useSettingsStore.loadAll('telegram')` → cache toàn bộ key
      `telegram.*` (prefix filter) vào store.
    - Edit thường: buffer nội bộ `drafts` (giống SettingsAccordion), lưu
      qua `useSettingsStore.bulkUpdate(drafts)` khi bấm "Save".
    - ĐỔI MODE (`telegram.mode`) KHÔNG đi qua bulkUpdate — backend TỪ CHỐI
      key này ở `/bulk` và `/{key}`. Phải gọi `POST /api/settings/telegram/mode`
      để chạy Mode_Switch_Guard (kiểm job Pull_Mode dở dang). Khi còn job
      dở dang + không force → backend trả 409 kèm `job_ids`; modal hỏi
      user xác nhận rồi gọi lại với `force=true` (đánh dấu các job đó FAIL).

  Endpoint riêng (không qua settings store):
    - POST /api/settings/telegram/mode          : đổi mode an toàn (Guard).
    - POST /api/notifications/telegram/test      : gửi text ping (Push_Mode).
    - POST /api/notifications/telegram/reset-cursor : reset round-robin cursor.
    - POST /api/notifications/telegram/batch-tally/reset : chốt kỳ Plus counter.
-->
<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import {
  NAlert,
  NButton,
  NIcon,
  NInput,
  NInputGroup,
  NInputNumber,
  NModal,
  NProgress,
  NRadioButton,
  NRadioGroup,
  NSpace,
  NSwitch,
  NText,
  useDialog,
  useMessage,
} from 'naive-ui'

import { useClientId } from '../composables/useClientId'
import { useLiveQrGateStore } from '../composables/useLiveQrGate'
import { usePushGateStore } from '../composables/usePushGate'
import { useSettingsStore } from '../composables/useSettingsStore'
import {
  IconEye,
  IconEyeOff,
  IconPlay,
  IconPlus,
  IconRepeat,
  IconSend,
  IconTrash,
} from '../icons'

// ---------------------------------------------------------------------------
// Whitelist keys — khớp constraint đã đăng ký ở
// `backend/app/notifiers/telegram/__init__.py`. Giữ 1 chỗ duy nhất, dễ
// update nếu backend đổi tên key.
// ---------------------------------------------------------------------------
const KEY_BOT_TOKEN = 'telegram.bot_token'
const KEY_MODE = 'telegram.mode'
const KEY_POLLING_ENABLED = 'telegram.polling_enabled'
const KEY_PUSH_ENABLED = 'telegram.push_mode.enabled'
const KEY_PUSH_SEND_PHOTO = 'telegram.push_mode.send_photo'
const KEY_PUSH_CHAT_TARGETS = 'telegram.push_mode.chat_targets'
const KEY_PUSH_SUCCESS_WAIT_ENABLED = 'telegram.push_mode.success_wait.enabled'
const KEY_PUSH_SUCCESS_WAIT_THRESHOLD = 'telegram.push_mode.success_wait.threshold'
const KEY_LIVE_QR_ENABLED = 'telegram.push_mode.live_qr.enabled'
const KEY_LIVE_QR_MAX_PER_CHAT = 'telegram.push_mode.live_qr.max_per_chat'
const KEY_PULL_MAX_CONCURRENT = 'telegram.pull_mode.max_concurrent_jobs_per_user'
const KEY_PULL_ALLOWED_CHAT_IDS = 'telegram.pull_mode.allowed_chat_ids'

// Endpoint riêng — reuse cùng fetch pattern như useSettingsStore.
const API_BASE = '/api'

// Mã lỗi ổn định backend trả khi Mode_Switch_Guard chặn đổi mode (xem
// `_MODE_SWITCH_BLOCKED_ERROR_CODE` ở routes_settings.py).
const MODE_SWITCH_BLOCKED_CODE = 'mode_switch_blocked'

interface ChatTarget {
  chat_id: string
  label: string
  enabled: boolean
}

const settingsStore = useSettingsStore()
const pushGateStore = usePushGateStore()
const liveQrGateStore = useLiveQrGateStore()
const clientId = useClientId()
const message = useMessage()
const dialog = useDialog()

const isOpen = ref<boolean>(false)
const showToken = ref<boolean>(false)
const saving = ref<boolean>(false)
const testing = ref<boolean>(false)
const resetting = ref<boolean>(false)
const closingPeriod = ref<boolean>(false)
const switchingMode = ref<boolean>(false)

// `drafts` buffer nội bộ — thay đổi KHÔNG persist tới backend cho tới khi
// bấm "Save". `telegram.mode` KHÔNG BAO GIỜ vào drafts (đổi tức thì qua
// endpoint riêng).
const drafts = reactive<Record<string, unknown>>({})

function currentValue<T>(key: string, fallback: T): T {
  if (key in drafts) return drafts[key] as T
  const stored = settingsStore.settings[key]
  return (stored === undefined ? fallback : stored) as T
}

// ---------------------------------------------------------------------------
// Operating mode — đọc từ store (không qua drafts). Đổi mode gọi endpoint
// Mode_Switch_Guard riêng, cập nhật store khi thành công.
// ---------------------------------------------------------------------------
const currentMode = computed<string>(() => {
  const stored = settingsStore.settings[KEY_MODE]
  return stored === 'pull' ? 'pull' : 'push'
})

function buildHeaders(): Record<string, string> {
  return {
    'X-Client-Id': clientId,
    'Content-Type': 'application/json',
  }
}

/**
 * Gọi POST /api/settings/telegram/mode. Trả về khi xong (thành công/huỷ).
 * 409 → hỏi user confirm rồi gọi lại với force=true.
 */
async function applyModeSwitch(newMode: string, force: boolean): Promise<void> {
  if (switchingMode.value) return
  if (newMode === currentMode.value && !force) return
  switchingMode.value = true
  try {
    const res = await fetch(`${API_BASE}/settings/telegram/mode`, {
      method: 'POST',
      headers: buildHeaders(),
      body: JSON.stringify({ mode: newMode, force }),
    })
    const body = await res.json()

    if (res.status === 200) {
      // Commit-after-confirm: cập nhật cache store theo giá trị đã persist.
      settingsStore.settings[KEY_MODE] = body.mode
      message.success(
        `Đã chuyển sang chế độ ${body.mode === 'pull' ? 'Pull (worker nhận job)' : 'Push (broadcast QR)'}`,
      )
      return
    }

    if (res.status === 409 && body?.error === MODE_SWITCH_BLOCKED_CODE) {
      const jobIds: string[] = Array.isArray(body.job_ids) ? body.job_ids : []
      dialog.warning({
        title: 'Còn job Pull_Mode đang dở dang',
        content:
          `Có ${jobIds.length} job đang được worker xử lý` +
          (jobIds.length ? `: ${jobIds.join(', ')}. ` : '. ') +
          'Đổi chế độ sẽ đánh dấu các job này THẤT BẠI (tính vào fail của worker). Tiếp tục?',
        positiveText: 'Vẫn đổi (force)',
        negativeText: 'Huỷ',
        onPositiveClick: () => {
          void applyModeSwitch(newMode, true)
        },
      })
      return
    }

    if (res.status === 400) {
      message.error(body?.reason ?? 'Giá trị mode không hợp lệ')
      return
    }
    message.error(`Đổi mode thất bại: HTTP ${res.status}`)
  } catch (err) {
    message.error(`Lỗi mạng: ${(err as Error).message}`)
  } finally {
    switchingMode.value = false
  }
}

function handleModeChange(value: string): void {
  void applyModeSwitch(value, false)
}

// ---------------------------------------------------------------------------
// Bindings draft-based cho các key còn lại
// ---------------------------------------------------------------------------
const pushEnabled = computed<boolean>({
  get: () => currentValue<boolean>(KEY_PUSH_ENABLED, false),
  set: (v) => { drafts[KEY_PUSH_ENABLED] = v },
})

// `send_photo` default `true` để giữ hành vi cũ khi user chưa từng
// tương tác với setting (VD DB cũ). Tắt = notifier gửi text-only qua
// `sendMessage`, không kèm ảnh QR — vẫn có đủ email masked + link
// thanh toán trong body message.
const pushSendPhoto = computed<boolean>({
  get: () => currentValue<boolean>(KEY_PUSH_SEND_PHOTO, true),
  set: (v) => { drafts[KEY_PUSH_SEND_PHOTO] = v },
})

// Push_Success_Gate (feature "success wait") — bật/tắt và ngưỡng N.
// Draft-based như các setting khác — apply khi bấm "Lưu". State runtime
// (counter/paused) ĐỌC TỪ pushGateStore (SSE realtime), KHÔNG lấy từ
// drafts. Mutex with Live QR Gate: enabling one clears the other in draft.
const successWaitEnabled = computed<boolean>({
  get: () => currentValue<boolean>(KEY_PUSH_SUCCESS_WAIT_ENABLED, false),
  set: (v) => {
    drafts[KEY_PUSH_SUCCESS_WAIT_ENABLED] = v
    if (v) drafts[KEY_LIVE_QR_ENABLED] = false
  },
})

const successWaitThreshold = computed<number>({
  get: () => currentValue<number>(KEY_PUSH_SUCCESS_WAIT_THRESHOLD, 10),
  set: (v) => { drafts[KEY_PUSH_SUCCESS_WAIT_THRESHOLD] = v },
})

// Live QR Gate — per-chat rolling cap on concurrent live QRs. Mutex with
// success wait (auto-clear). No Resume — free on plus/timeout/lifecycle.
const liveQrEnabled = computed<boolean>({
  get: () => currentValue<boolean>(KEY_LIVE_QR_ENABLED, false),
  set: (v) => {
    drafts[KEY_LIVE_QR_ENABLED] = v
    if (v) drafts[KEY_PUSH_SUCCESS_WAIT_ENABLED] = false
  },
})

const liveQrMaxPerChat = computed<number>({
  get: () => currentValue<number>(KEY_LIVE_QR_MAX_PER_CHAT, 5),
  set: (v) => { drafts[KEY_LIVE_QR_MAX_PER_CHAT] = v },
})

const botToken = computed<string>({
  get: () => currentValue<string>(KEY_BOT_TOKEN, ''),
  set: (v) => { drafts[KEY_BOT_TOKEN] = v },
})

const pollingEnabled = computed<boolean>({
  get: () => currentValue<boolean>(KEY_POLLING_ENABLED, false),
  set: (v) => { drafts[KEY_POLLING_ENABLED] = v },
})

const maxConcurrent = computed<number>({
  get: () => currentValue<number>(KEY_PULL_MAX_CONCURRENT, 1),
  set: (v) => { drafts[KEY_PULL_MAX_CONCURRENT] = v },
})

// `chat_targets` (Push_Mode) — list_object, clone khi copy từ store để
// mutation không đụng cache trước lúc lưu.
const chatTargets = computed<ChatTarget[]>({
  get: () => {
    const raw = currentValue<ChatTarget[]>(KEY_PUSH_CHAT_TARGETS, [])
    return Array.isArray(raw) ? raw.map((t) => ({ ...t })) : []
  },
  set: (v) => { drafts[KEY_PUSH_CHAT_TARGETS] = v },
})

// `allowed_chat_ids` (Pull_Mode) — list_str. Rỗng = cho phép mọi chat.
const allowedChatIds = computed<string[]>({
  get: () => {
    const raw = currentValue<string[]>(KEY_PULL_ALLOWED_CHAT_IDS, [])
    return Array.isArray(raw) ? raw.slice() : []
  },
  set: (v) => { drafts[KEY_PULL_ALLOWED_CHAT_IDS] = v },
})

function commitChatTargets(list: ChatTarget[]): void {
  const normalized: ChatTarget[] = list.map((t) => ({
    chat_id: (t.chat_id ?? '').toString().trim(),
    label: (t.label ?? '').toString().trim(),
    enabled: Boolean(t.enabled),
  }))
  drafts[KEY_PUSH_CHAT_TARGETS] = normalized
}

function addChatTarget(): void {
  commitChatTargets([
    ...chatTargets.value,
    { chat_id: '', label: '', enabled: true },
  ])
}

function removeChatTarget(index: number): void {
  const next = chatTargets.value.slice()
  next.splice(index, 1)
  commitChatTargets(next)
}

function updateChatTarget(index: number, patch: Partial<ChatTarget>): void {
  const next = chatTargets.value.slice()
  next[index] = { ...next[index], ...patch }
  commitChatTargets(next)
}

function addAllowedChat(): void {
  drafts[KEY_PULL_ALLOWED_CHAT_IDS] = [...allowedChatIds.value, '']
}

function removeAllowedChat(index: number): void {
  const next = allowedChatIds.value.slice()
  next.splice(index, 1)
  drafts[KEY_PULL_ALLOWED_CHAT_IDS] = next
}

function updateAllowedChat(index: number, value: string): void {
  const next = allowedChatIds.value.slice()
  next[index] = value
  drafts[KEY_PULL_ALLOWED_CHAT_IDS] = next
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------
async function handleSave(): Promise<void> {
  if (saving.value || Object.keys(drafts).length === 0) return
  saving.value = true
  try {
    const payload: Record<string, unknown> = { ...drafts }
    // Filter chat_targets: bỏ entry chat_id rỗng trước khi save.
    if (KEY_PUSH_CHAT_TARGETS in payload) {
      const list = payload[KEY_PUSH_CHAT_TARGETS] as ChatTarget[]
      payload[KEY_PUSH_CHAT_TARGETS] = list.filter((t) => t.chat_id.trim().length > 0)
    }
    // Filter allowed_chat_ids: trim + bỏ chuỗi rỗng.
    if (KEY_PULL_ALLOWED_CHAT_IDS in payload) {
      const list = payload[KEY_PULL_ALLOWED_CHAT_IDS] as string[]
      payload[KEY_PULL_ALLOWED_CHAT_IDS] = list
        .map((c) => c.trim())
        .filter((c) => c.length > 0)
    }
    // Guard max_concurrent: n-input-number có thể emit `null` khi user
    // xoá trắng ô → backend từ chối (constraint int 1..20 → HTTP 400).
    // Clamp về khoảng hợp lệ để các thay đổi khác vẫn lưu được: null/
    // không phải số nguyên hoặc <1 → 1, >20 → 20.
    if (KEY_PULL_MAX_CONCURRENT in payload) {
      const raw = payload[KEY_PULL_MAX_CONCURRENT]
      let clamped: number
      if (typeof raw !== 'number' || !Number.isInteger(raw)) {
        clamped = 1
      } else if (raw < 1) {
        clamped = 1
      } else if (raw > 20) {
        clamped = 20
      } else {
        clamped = raw
      }
      payload[KEY_PULL_MAX_CONCURRENT] = clamped
    }
    // Cùng lý do như max_concurrent: n-input-number có thể emit `null`
    // khi user xoá trắng ô. Clamp về khoảng 1..1000 (khớp constraint
    // backend).
    if (KEY_PUSH_SUCCESS_WAIT_THRESHOLD in payload) {
      const raw = payload[KEY_PUSH_SUCCESS_WAIT_THRESHOLD]
      let clamped: number
      if (typeof raw !== 'number' || !Number.isInteger(raw)) {
        clamped = 10
      } else if (raw < 1) {
        clamped = 1
      } else if (raw > 1000) {
        clamped = 1000
      } else {
        clamped = raw
      }
      payload[KEY_PUSH_SUCCESS_WAIT_THRESHOLD] = clamped
    }
    if (KEY_LIVE_QR_MAX_PER_CHAT in payload) {
      const raw = payload[KEY_LIVE_QR_MAX_PER_CHAT]
      let clamped: number
      if (typeof raw !== 'number' || !Number.isInteger(raw)) {
        clamped = 5
      } else if (raw < 1) {
        clamped = 1
      } else if (raw > 50) {
        clamped = 50
      } else {
        clamped = raw
      }
      payload[KEY_LIVE_QR_MAX_PER_CHAT] = clamped
    }
    // Mutex: never leave both modes true in the same save payload.
    if (payload[KEY_LIVE_QR_ENABLED] === true) {
      payload[KEY_PUSH_SUCCESS_WAIT_ENABLED] = false
    } else if (payload[KEY_PUSH_SUCCESS_WAIT_ENABLED] === true) {
      payload[KEY_LIVE_QR_ENABLED] = false
    }
    const ok = await settingsStore.bulkUpdate(payload)
    if (ok) {
      for (const k of Object.keys(payload)) delete drafts[k]
      message.success('Đã lưu cấu hình Telegram')
    }
  } finally {
    saving.value = false
  }
}

async function handleTest(): Promise<void> {
  if (testing.value) return
  if (hasDrafts.value) {
    message.warning('Hãy lưu thay đổi trước khi gửi thử')
    return
  }
  testing.value = true
  try {
    const res = await fetch(`${API_BASE}/notifications/telegram/test`, {
      method: 'POST',
      headers: buildHeaders(),
    })
    const body = await res.json()
    if (res.status === 200) {
      const sent = Array.isArray(body.sent) ? body.sent.length : 0
      const failed = Array.isArray(body.failed) ? body.failed.length : 0
      if (failed === 0) {
        message.success(`Đã gửi thử tới ${sent} chat`)
      } else {
        const detail = (body.failed as Array<{ chat_id: string; error: string }>)
          .map((f) => `${f.chat_id}: ${f.error}`)
          .join('; ')
        message.warning(`Gửi ${sent}, lỗi ${failed}: ${detail}`)
      }
    } else if (res.status === 400) {
      message.error(body?.detail?.message ?? 'Cấu hình không hợp lệ')
    } else {
      message.error(`Gửi thử thất bại: HTTP ${res.status}`)
    }
  } catch (err) {
    message.error(`Lỗi mạng: ${(err as Error).message}`)
  } finally {
    testing.value = false
  }
}

async function handleResetCursor(): Promise<void> {
  if (resetting.value) return
  resetting.value = true
  try {
    const res = await fetch(`${API_BASE}/notifications/telegram/reset-cursor`, {
      method: 'POST',
      headers: buildHeaders(),
    })
    const body = await res.json()
    if (res.status === 200) {
      message.success(
        `Đã reset lượt (cursor ${body.previous_cursor} → ${body.current_cursor})`,
      )
    } else {
      message.error(`Reset thất bại: HTTP ${res.status}`)
    }
  } catch (err) {
    message.error(`Lỗi mạng: ${(err as Error).message}`)
  } finally {
    resetting.value = false
  }
}

/** Confirm then POST batch-tally/reset (web primary path for period close). */
function handleChotKy(): void {
  if (closingPeriod.value) return
  dialog.warning({
    title: 'Chốt kỳ & reset bộ đếm Plus',
    content: 'Chốt kỳ và reset bộ đếm Plus?',
    positiveText: 'Chốt kỳ',
    negativeText: 'Huỷ',
    onPositiveClick: () => {
      void doChotKy()
    },
  })
}

async function doChotKy(): Promise<void> {
  if (closingPeriod.value) return
  closingPeriod.value = true
  try {
    const res = await fetch(
      `${API_BASE}/notifications/telegram/batch-tally/reset`,
      { method: 'POST', headers: buildHeaders() },
    )
    const body = await res.json().catch(() => ({}))
    if (res.status !== 200) {
      message.error(`Chốt kỳ thất bại: HTTP ${res.status}`)
      return
    }
    const plusTotal = Number(body?.plus_total ?? 0)
    const expiredTotal = Number(body?.expired_total ?? 0)
    const skipped = Array.isArray(body?.skipped) ? body.skipped : []
    let text = `Đã chốt kỳ — Plus: ${plusTotal}, Hết hạn: ${expiredTotal}`
    if (skipped.length > 0) {
      text += ` (bỏ qua ${skipped.length} chat: ${skipped.join(', ')})`
    }
    message.success(text)
  } catch (err) {
    message.error(`Lỗi mạng: ${(err as Error).message}`)
  } finally {
    closingPeriod.value = false
  }
}

const hasDrafts = computed<boolean>(() => Object.keys(drafts).length > 0)
const draftCount = computed<number>(() => Object.keys(drafts).length)
const isPullMode = computed<boolean>(() => currentMode.value === 'pull')

const activeChatCount = computed<number>(
  () => chatTargets.value.filter((t) => t.enabled && t.chat_id.trim()).length,
)

// Load lần đầu khi user mở modal — tránh gọi API lãng phí lúc chưa tương tác.
watch(isOpen, async (opened) => {
  if (opened) {
    try {
      await settingsStore.loadAll('telegram')
    } catch { /* store.error đã set */ }
  }
})
</script>

<template>
  <span class="telegram-modal-launcher">
    <button
      type="button"
      class="launcher-btn"
      :aria-expanded="isOpen"
      :title="hasDrafts ? `${draftCount} thay đổi chưa lưu` : 'Cấu hình bot Telegram'"
      @click="isOpen = true"
    >
      <n-icon :component="IconSend" size="15" class="launcher-icon" />
      <span class="launcher-label">Telegram</span>
      <span
        class="launcher-mode"
        :class="{ 'launcher-mode--pull': isPullMode }"
      >
        {{ isPullMode ? 'PULL' : 'PUSH' }}
      </span>
      <span
        v-if="hasDrafts"
        class="launcher-badge"
        :aria-label="`${draftCount} thay đổi chưa lưu`"
      >
        {{ draftCount }}
      </span>
    </button>

    <n-modal
      v-model:show="isOpen"
      preset="card"
      title="Cấu hình bot Telegram"
      class="telegram-modal"
      :bordered="false"
      :segmented="{ content: 'soft', footer: 'soft' }"
      :closable="true"
      :mask-closable="false"
      :close-on-esc="true"
      style="width: 680px; max-width: 95vw"
    >
      <div class="modal-body">
        <n-alert
          v-if="settingsStore.error"
          type="error"
          size="small"
          :show-icon="false"
        >
          <template #header>
            <strong class="mono">{{ settingsStore.error.key }}</strong>
          </template>
          {{ settingsStore.error.reason }}
        </n-alert>

        <!-- ═══════════ Operating mode ═══════════ -->
        <fieldset class="group">
          <legend class="group__legend">Chế độ vận hành</legend>

          <div class="field field--inline">
            <label class="switch-label">
              Chuyển giữa Push (tự broadcast QR) và Pull (worker chủ động nhận job)
            </label>
            <n-radio-group
              :value="currentMode"
              size="small"
              :disabled="switchingMode"
              @update:value="handleModeChange"
            >
              <n-radio-button value="push">Push</n-radio-button>
              <n-radio-button value="pull">Pull</n-radio-button>
            </n-radio-group>
          </div>
          <p class="field-hint">
            Đổi mode có hiệu lực NGAY (không cần bấm Lưu). Nếu còn job Pull_Mode
            đang chạy, hệ thống sẽ hỏi xác nhận trước khi buộc dừng chúng.
          </p>
        </fieldset>

        <!-- ═══════════ Bot token (dùng chung) ═══════════ -->
        <fieldset class="group">
          <legend class="group__legend">Kết nối bot</legend>

          <div class="field">
            <label for="tg-bot-token">Bot Token</label>
            <n-input-group>
              <n-input
                id="tg-bot-token"
                v-model:value="botToken"
                :type="showToken ? 'text' : 'password'"
                placeholder="123456789:AAExampleTokenFromBotFather"
                class="mono"
                spellcheck="false"
              />
              <n-button
                :title="showToken ? 'Ẩn token' : 'Hiện token'"
                @click="showToken = !showToken"
              >
                <template #icon>
                  <n-icon :component="showToken ? IconEyeOff : IconEye" />
                </template>
              </n-button>
            </n-input-group>
            <p class="field-hint">
              Lấy token bằng cách chat với
              <code>@BotFather</code> → <code>/newbot</code>. Token được che
              trong log/SSE và chỉ lưu trong SQLite backend.
            </p>
          </div>

          <div class="field field--inline">
            <label class="switch-label">
              Bật bot lắng nghe lệnh/nút (polling) — bắt buộc cho Pull_Mode
            </label>
            <n-switch v-model:value="pollingEnabled" />
          </div>
        </fieldset>

        <!-- ═══════════ Pull mode settings ═══════════ -->
        <fieldset class="group" :class="{ 'group--muted': !isPullMode }">
          <legend class="group__legend">
            Cấu hình Pull_Mode
            <span class="legend-hint">
              (worker nhận job qua bot)
            </span>
          </legend>

          <div class="field field--inline">
            <label class="switch-label">
              Số job mỗi lượt "Nhận job" (worker phải xử lý hết mới nhận lượt mới)
            </label>
            <n-input-number
              v-model:value="maxConcurrent"
              size="small"
              :min="1"
              :max="20"
              style="width: 120px"
            />
          </div>

          <div class="field">
            <label>
              Danh sách chat được phép nhận job
              <span class="legend-hint">
                ({{ allowedChatIds.length === 0 ? 'trống = cho phép MỌI chat' : `${allowedChatIds.length} chat` }})
              </span>
            </label>

            <div v-if="allowedChatIds.length === 0" class="empty-state">
              Chưa giới hạn — mọi chat gọi /start đều nhận được job. Thêm chat_id
              để chỉ cho phép các chat cụ thể.
            </div>

            <div v-else class="chat-list">
              <div
                v-for="(chatId, idx) in allowedChatIds"
                :key="idx"
                class="allowed-row"
              >
                <n-input
                  :value="chatId"
                  size="small"
                  placeholder="chat_id (vd -1001234567890 hoặc 123456789)"
                  class="chat-id mono"
                  spellcheck="false"
                  @update:value="(v: string) => updateAllowedChat(idx, v)"
                />
                <n-button
                  size="small"
                  quaternary
                  circle
                  type="error"
                  title="Xoá"
                  @click="removeAllowedChat(idx)"
                >
                  <template #icon>
                    <n-icon :component="IconTrash" />
                  </template>
                </n-button>
              </div>
            </div>

            <div class="chat-list-actions">
              <n-button size="small" @click="addAllowedChat">
                <template #icon>
                  <n-icon :component="IconPlus" />
                </template>
                Thêm chat
              </n-button>
            </div>
          </div>
        </fieldset>

        <!-- ═══════════ Push mode settings ═══════════ -->
        <fieldset class="group" :class="{ 'group--muted': isPullMode }">
          <legend class="group__legend">
            Cấu hình Push_Mode
            <span class="legend-hint">
              (tự gửi QR khi job xong)
            </span>
          </legend>

          <div class="field field--inline">
            <label class="switch-label">
              Bật thông báo (chỉ khi job thành công có QR)
            </label>
            <n-switch v-model:value="pushEnabled" />
          </div>

          <div class="field field--inline">
            <label class="switch-label">
              Gửi kèm ảnh QR
              <span class="legend-hint">
                (tắt = chỉ gửi text + link thanh toán)
              </span>
            </label>
            <n-switch v-model:value="pushSendPhoto" :disabled="!pushEnabled" />
          </div>

          <!--
            Live QR Gate — cap concurrent live QRs per chat. Mutex with
            success wait (one mode only). Free slot auto-wakes — no Resume.
          -->
          <div class="field field--inline">
            <label class="switch-label">
              Live QR Gate
              <span class="legend-hint">
                (giới hạn QR đang sống / chat — tự mở khi Plus hoặc hết 5 phút)
              </span>
            </label>
            <n-switch
              v-model:value="liveQrEnabled"
              :disabled="!pushEnabled"
            />
          </div>

          <div class="field field--inline" :class="{ 'field--muted': !liveQrEnabled }">
            <label class="switch-label">
              Tối đa QR sống / chat
            </label>
            <n-input-number
              v-model:value="liveQrMaxPerChat"
              size="small"
              :min="1"
              :max="50"
              :disabled="!pushEnabled || !liveQrEnabled"
              style="width: 120px"
            />
          </div>

          <div
            v-if="liveQrEnabled"
            class="field success-wait-progress"
          >
            <div class="success-wait-progress__row">
              <span class="success-wait-progress__label">
                Live
                <strong class="mono">{{ liveQrGateStore.snapshot.live }}</strong>
                /
                <strong class="mono">{{ liveQrGateStore.snapshot.capacity }}</strong>
                (max {{ liveQrGateStore.snapshot.max_per_chat }}/chat)
                <span v-if="liveQrGateStore.snapshot.blocked"> — đầy (chờ slot)</span>
              </span>
            </div>
            <div
              v-if="Object.keys(liveQrGateStore.snapshot.per_chat).length > 0"
              class="legend-hint"
              style="margin-top: 4px"
            >
              Per chat:
              <span
                v-for="(count, chatId) in liveQrGateStore.snapshot.per_chat"
                :key="chatId"
                class="mono"
                style="margin-right: 8px"
              >
                {{ chatId }}: {{ count }}/{{ liveQrGateStore.snapshot.max_per_chat }}
              </span>
            </div>
          </div>

          <!--
            Success wait — sau mỗi N lần gửi Push_Mode thành công, scheduler
            dừng cấp slot cho job Push_Mode chờ user bấm "Tiếp tục" (nút to
            ở JobList header). Mutex với Live QR Gate. Setting draft-based,
            state runtime đọc realtime từ pushGateStore.
          -->
          <div class="field field--inline">
            <label class="switch-label">
              Chờ theo lượt thành công
              <span class="legend-hint">
                (đủ N success → tạm dừng chờ user tiếp tục; mutex với Live Gate)
              </span>
            </label>
            <n-switch
              v-model:value="successWaitEnabled"
              :disabled="!pushEnabled"
            />
          </div>

          <div class="field field--inline" :class="{ 'field--muted': !successWaitEnabled }">
            <label class="switch-label">
              Số success mỗi lượt
            </label>
            <n-input-number
              v-model:value="successWaitThreshold"
              size="small"
              :min="1"
              :max="1000"
              :disabled="!pushEnabled || !successWaitEnabled"
              style="width: 120px"
            />
          </div>

          <div
            v-if="successWaitEnabled"
            class="field success-wait-progress"
          >
            <div class="success-wait-progress__row">
              <span class="success-wait-progress__label">
                Đã gửi
                <strong class="mono">{{ pushGateStore.snapshot.counter }}</strong>
                /
                <strong class="mono">{{ pushGateStore.snapshot.threshold }}</strong>
                success (kể từ lần "Tiếp tục" gần nhất)
              </span>
              <n-button
                v-if="pushGateStore.snapshot.paused"
                size="small"
                type="primary"
                :loading="pushGateStore.resuming"
                @click="pushGateStore.resume"
              >
                <template #icon>
                  <n-icon :component="IconPlay" />
                </template>
                Tiếp tục
              </n-button>
            </div>
            <n-progress
              type="line"
              :percentage="Math.min(
                100,
                Math.round(
                  (pushGateStore.snapshot.counter / Math.max(1, pushGateStore.snapshot.threshold)) * 100,
                ),
              )"
              :status="pushGateStore.snapshot.paused ? 'warning' : 'default'"
              :show-indicator="false"
              :height="6"
              style="margin-top: 6px"
            />
          </div>

          <div class="field">
            <label>
              Danh sách chat
              <span class="legend-hint">
                ({{ activeChatCount }}/{{ chatTargets.length }} bật · round-robin)
              </span>
            </label>

            <div v-if="chatTargets.length === 0" class="empty-state">
              Chưa có chat nào. Bấm "Thêm chat" để bắt đầu.
            </div>

            <div v-else class="chat-list">
              <div
                v-for="(target, idx) in chatTargets"
                :key="idx"
                class="chat-row"
                :class="{ 'chat-row--disabled': !target.enabled }"
              >
                <n-switch
                  :value="target.enabled"
                  size="small"
                  :title="target.enabled ? 'Đang gửi tới chat này' : 'Tạm tắt'"
                  @update:value="(v: boolean) => updateChatTarget(idx, { enabled: v })"
                />

                <n-input
                  :value="target.label"
                  size="small"
                  placeholder="Tên gợi nhớ (vd Sếp, Nhóm ops)"
                  class="chat-label"
                  @update:value="(v: string) => updateChatTarget(idx, { label: v })"
                />

                <n-input
                  :value="target.chat_id"
                  size="small"
                  placeholder="chat_id (vd -1001234567890)"
                  class="chat-id mono"
                  spellcheck="false"
                  @update:value="(v: string) => updateChatTarget(idx, { chat_id: v })"
                />

                <n-button
                  size="small"
                  quaternary
                  circle
                  type="error"
                  title="Xoá chat"
                  @click="removeChatTarget(idx)"
                >
                  <template #icon>
                    <n-icon :component="IconTrash" />
                  </template>
                </n-button>
              </div>
            </div>

            <div class="chat-list-actions">
              <n-button size="small" @click="addChatTarget">
                <template #icon>
                  <n-icon :component="IconPlus" />
                </template>
                Thêm chat
              </n-button>

              <n-space :size="6">
                <n-button
                  size="small"
                  :loading="resetting"
                  title="Reset để chat kế tiếp là entry đầu trong danh sách bật"
                  @click="handleResetCursor"
                >
                  <template #icon>
                    <n-icon :component="IconRepeat" />
                  </template>
                  Reset lượt
                </n-button>
                <n-button
                  size="small"
                  type="warning"
                  ghost
                  :loading="closingPeriod"
                  :disabled="closingPeriod"
                  title="Chốt kỳ: gửi receipt + reset bộ đếm Plus (đường chính; /chotky cần polling + admin_user_ids)"
                  data-testid="chotky-button"
                  @click="handleChotKy"
                >
                  Chốt kỳ & reset bộ đếm Plus
                </n-button>
                <n-button
                  size="small"
                  type="primary"
                  ghost
                  :loading="testing"
                  :disabled="hasDrafts"
                  :title="hasDrafts ? 'Lưu thay đổi trước khi gửi thử' : 'Gửi text thử tới tất cả chat đang bật'"
                  @click="handleTest"
                >
                  <template #icon>
                    <n-icon :component="IconSend" />
                  </template>
                  Gửi thử
                </n-button>
              </n-space>
            </div>
          </div>
        </fieldset>

        <!-- ═══════════ How to get chat_id ═══════════ -->
        <n-alert type="info" size="small" :show-icon="false">
          <template #header>
            <strong>Cách lấy chat_id</strong>
          </template>
          <ul class="hint-list">
            <li>
              Chat riêng: nhắn
              <code>@userinfobot</code> hoặc <code>@RawDataBot</code>.
            </li>
          </ul>
        </n-alert>
      </div>

      <template #footer>
        <div class="footer">
          <n-text depth="3" style="font-size: 12px">
            <span v-if="hasDrafts">
              {{ draftCount }} thay đổi chưa lưu
            </span>
            <span v-else>Không có thay đổi</span>
          </n-text>
          <n-space :size="8">
            <n-button size="small" quaternary @click="isOpen = false">
              Đóng
            </n-button>
            <n-button
              type="primary"
              size="small"
              :loading="saving"
              :disabled="!hasDrafts || settingsStore.loading"
              @click="handleSave"
            >
              Lưu
            </n-button>
          </n-space>
        </div>
      </template>
    </n-modal>
  </span>
</template>

<style scoped>
.telegram-modal-launcher {
  display: inline-flex;
}

.launcher-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 30px;
  padding: 0 12px;
  font-family: inherit;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-1);
  background: var(--surface-2);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  cursor: pointer;
  transition: background var(--transition-fast), border-color var(--transition-fast);
  position: relative;
}

.launcher-btn:hover {
  background: var(--surface-3);
  border-color: var(--brand-primary);
}

.launcher-btn:focus-visible {
  outline: 2px solid var(--brand-primary);
  outline-offset: 2px;
}

.launcher-icon {
  color: var(--brand-primary);
  flex-shrink: 0;
}

.launcher-label {
  white-space: nowrap;
}

.launcher-mode {
  font-size: 9.5px;
  font-weight: 700;
  letter-spacing: 0.5px;
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--surface-3);
  color: var(--n-text-color-3);
}

.launcher-mode--pull {
  background: var(--brand-primary);
  color: #fff;
}

.launcher-badge {
  position: absolute;
  top: -6px;
  right: -6px;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--accent-yellow);
  color: #1a1200;
  font-size: 10px;
  font-weight: 700;
  line-height: 18px;
  text-align: center;
  border: 2px solid var(--surface-1);
  font-variant-numeric: tabular-nums;
}

.modal-body {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.group {
  border: 1px solid var(--border-color);
  border-radius: 8px;
  padding: 14px 16px 16px;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  background: var(--surface-1);
  transition: opacity var(--transition-fast);
}

.group--muted {
  opacity: 0.6;
}

.group__legend {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 2px 10px;
  font-size: 11px;
  font-weight: 700;
  color: var(--text-1);
  text-transform: uppercase;
  letter-spacing: 0.6px;
  background: var(--surface-2);
  border: 1px solid var(--border-color);
  border-radius: 999px;
}

.legend-hint {
  font-size: 10.5px;
  font-weight: 500;
  text-transform: none;
  letter-spacing: 0;
  color: var(--n-text-color-3);
}

.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.field label {
  font-size: 11.5px;
  font-weight: 500;
  color: var(--n-text-color-2);
}

.field--muted {
  opacity: 0.55;
}

.success-wait-progress {
  padding: 8px 10px;
  background: var(--surface-2);
  border-radius: 6px;
  border: 1px solid var(--border-color-soft);
}

.success-wait-progress__row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.success-wait-progress__label {
  font-size: 12px;
  color: var(--n-text-color-2);
}

.success-wait-progress__label strong {
  font-weight: 700;
  color: var(--text-1);
  padding: 0 2px;
}

.field--inline {
  flex-direction: row;
  align-items: center;
  justify-content: space-between;
}

.switch-label {
  font-size: 12px !important;
  color: var(--n-text-color) !important;
  font-weight: 500;
}

.field-hint {
  margin: 4px 0 0;
  font-size: 11px;
  line-height: 1.5;
  color: var(--n-text-color-3);
}

.field-hint code {
  font-family: var(--n-font-family-mono);
  font-size: 10.5px;
  padding: 0 3px;
  background: var(--surface-2);
  border-radius: 3px;
}

.chat-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.chat-row {
  display: grid;
  grid-template-columns: auto 1fr 1.4fr auto;
  gap: 8px;
  align-items: center;
  padding: 6px 8px;
  border-radius: 6px;
  background: var(--surface-0);
  border: 1px solid var(--border-color-soft);
  transition: opacity var(--transition-fast);
}

.allowed-row {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 8px;
  align-items: center;
  padding: 6px 8px;
  border-radius: 6px;
  background: var(--surface-0);
  border: 1px solid var(--border-color-soft);
}

.chat-row--disabled {
  opacity: 0.55;
}

.chat-row--disabled .chat-label :deep(input),
.chat-row--disabled .chat-id :deep(input) {
  text-decoration: line-through;
  text-decoration-color: var(--text-3);
}

.chat-list-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 4px;
}

.empty-state {
  padding: 14px;
  text-align: center;
  font-size: 11.5px;
  color: var(--n-text-color-3);
  background: var(--surface-2);
  border-radius: 6px;
  border: 1px dashed var(--border-color);
}

.hint-list {
  margin: 4px 0 0 16px;
  padding: 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--n-text-color-2);
}

.hint-list code {
  font-family: var(--n-font-family-mono);
  font-size: 10.5px;
  padding: 0 3px;
  background: var(--surface-2);
  border-radius: 3px;
}

.footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
}

.telegram-modal :deep(.n-card__content) {
  max-height: 75vh;
  overflow-y: auto;
}

.mono :deep(textarea),
.mono :deep(input) {
  font-family: var(--n-font-family-mono);
  font-size: 11.5px;
}
</style>
