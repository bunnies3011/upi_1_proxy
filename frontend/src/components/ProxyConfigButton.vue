<!--
  ProxyConfigButton — button trên topbar + modal cấu hình proxy pool riêng.

  Tham chiếu design: `gpt_signup_hybrid/web/static/reg_proxy.js` — modal có
  các phần:
  - Row list proxy (input host:port[:user:pass]).
  - Add row / Bulk paste (sub-modal textarea).
  - Remove per-row.
  - Rotation mode select (round_robin | least_used).
  - Advanced: max_leases_per_proxy, dead_threshold.
  - Save (write-through Settings Store).

  Khác với gpt_signup_hybrid (dùng API `/api/reg/proxy/pool` riêng),
  ideal_qr_tool đã có Settings_Store whitelist `proxy.list` +
  `proxy.rotation_mode` + `proxy.max_leases_per_proxy` + `proxy.dead_threshold`.
  Modal đọc/ghi qua `useSettingsStore().updateKey(...)` / `bulkUpdate(...)`
  — không thêm route mới ở backend.

  Nguyên tắc:
  - KHÔNG dùng localStorage (R11.5).
  - Commit-after-confirm: giá trị chỉ ghi vào store SAU khi API 200
    (`useSettingsStore.bulkUpdate` đã xử lý).
  - Fail-fast: bulk update dạng transaction ở backend — 1 key vi phạm
    thì cả batch rollback. FE hiển thị lỗi qua AppErrorNotifier
    (`settingsStore.error`).
-->
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  NBadge,
  NButton,
  NIcon,
  NInput,
  NInputNumber,
  NModal,
  NSelect,
  NSpace,
  NText,
  NTooltip,
  useDialog,
  useMessage,
} from 'naive-ui'
import type { SelectOption } from 'naive-ui'

import { useSettingsStore } from '../composables/useSettingsStore'
import {
  useProxyProbe,
  type ProxyProbeResult,
} from '../composables/useProxyProbe'
import {
  IconCircleCheck,
  IconCircleDot,
  IconCircleX,
  IconClipboardList,
  IconPlus,
  IconServer,
  IconSpinner,
  IconTrash,
  IconWifi,
} from '../icons'

const SETTING_KEYS = Object.freeze({
  LIST: 'proxy.list',
  ROTATION_MODE: 'proxy.rotation_mode',
  MAX_LEASES: 'proxy.max_leases_per_proxy',
  DEAD_THRESHOLD: 'proxy.dead_threshold',
  // TTL "quarantine" cho proxy vừa bị mark_dead — sau khoảng này pool tự
  // revive proxy (dead=False, consecutive_errors=0). 0 = tắt TTL (dead
  // vĩnh viễn). Default backend = 120s.
  DEAD_COOLDOWN: 'proxy.dead_cooldown_seconds',
  // Preflight probe knobs (2026-07, port từ gpt_signup_hybrid). Đọc/ghi
  // qua cùng `bulkUpdate` với 4 key trên để save 1 transaction.
  PROBE_ENABLED: 'proxy.probe_enabled',
  PROBE_ENDPOINT: 'proxy.probe_endpoint',
  PROBE_TIMEOUT: 'proxy.probe_timeout_seconds',
  PROBE_MAX_TRIES: 'proxy.probe_max_tries',
  PROBE_SID_RETRY: 'proxy.probe_sid_retry_per_line',
  PROBE_CONCURRENCY: 'proxy.probe_concurrency',
  FALLBACK_DIRECT: 'proxy.fallback_direct_on_exhausted',
} as const)

const ROTATION_OPTIONS: SelectOption[] = [
  { label: 'round_robin — rotate sequentially', value: 'round_robin' },
  { label: 'least_used — least used proxy', value: 'least_used' },
]

const settingsStore = useSettingsStore()
const message = useMessage()
// Dialog dùng cho confirm destructive action (Clear all). Provider có sẵn
// ở App.vue root nên KHÔNG cần wrap thêm NDialogProvider trong file này.
const dialog = useDialog()

const showModal = ref<boolean>(false)
const showPasteModal = ref<boolean>(false)
const pasteText = ref<string>('')
const saving = ref<boolean>(false)

// -----------------------------------------------------------------------------
// Draft state — snapshot khi mở modal, chỉnh sửa cục bộ, save 1 lần bulkUpdate.
// KHÔNG binding trực tiếp `settingsStore.settings[...]` để tránh side-effect
// trên các consumer khác trước khi user bấm Save.
// -----------------------------------------------------------------------------

interface ProxyRow {
  id: number
  value: string
}

let _rowSeq = 0
function makeRow(value: string): ProxyRow {
  return { id: ++_rowSeq, value }
}

const rows = ref<ProxyRow[]>([])
const rotationMode = ref<string>('round_robin')
const maxLeasesPerProxy = ref<number>(10)
const deadThreshold = ref<number>(3)
const deadCooldownSeconds = ref<number>(120)

// Preflight probe knobs — default khớp backend `ProbeConfig` để user thấy
// đúng "off-the-shelf behavior" ngay khi mở modal lần đầu (chưa từng save).
const probeEnabled = ref<boolean>(true)
const probeEndpoint = ref<string>('https://api64.ipify.org')
const probeTimeout = ref<number>(6)
const probeMaxTries = ref<number>(10)
const probeSidRetry = ref<number>(2)
const probeConcurrency = ref<number>(5)
const fallbackDirect = ref<boolean>(true)

// -----------------------------------------------------------------------------
// Test-all state — kết quả probe read-only cho draft. Key = raw line
// (proxy value đã strip), value = kết quả từ backend. Line có nhiều row
// trùng nội dung sẽ chia sẻ cùng 1 kết quả (backend dedupe trước khi probe).
// -----------------------------------------------------------------------------

const { probing, probeBatchStream } = useProxyProbe()
const probeResults = ref<Map<string, ProxyProbeResult>>(new Map())
const probeSummary = ref<{ live: number; total: number; elapsedMs: number } | null>(null)
// Progress realtime khi probe stream đang chạy — hiển thị trên summary bar
// dạng "42/100 live · 58 pending" để user biết đang đến đâu.
const probeProgress = ref<{
  total: number
  done: number
  live: number
} | null>(null)
// AbortController để user có thể huỷ probe giữa chừng (đóng modal / bấm lại).
let probeAbortController: AbortController | null = null

// Tổng số proxy non-empty trong draft — hiển thị summary trên toggle button.
const totalProxies = computed<number>(
  () => rows.value.filter((r) => r.value.trim().length > 0).length,
)

// Progress bar fill width (%) — tính từ `probeProgress`. Null khi không probing.
const probeProgressPercent = computed<number>(() => {
  if (probeProgress.value === null) return 0
  const total = Math.max(probeProgress.value.total, 1)
  return Math.round((probeProgress.value.done / total) * 100)
})

// Badge count trên topbar — luôn phản ánh giá trị đã persist trong Settings
// Store (không phải draft), để user thấy con số ổn định giữa các lần mở modal.
const persistedProxyCount = computed<number>(() => {
  const list = settingsStore.settings[SETTING_KEYS.LIST]
  return Array.isArray(list) ? list.length : 0
})

function hydrateFromStore(): void {
  const list = settingsStore.settings[SETTING_KEYS.LIST]
  const proxies = Array.isArray(list) ? (list as string[]) : []
  rows.value = proxies.length > 0 ? proxies.map((p) => makeRow(p)) : [makeRow('')]

  const mode = settingsStore.settings[SETTING_KEYS.ROTATION_MODE]
  rotationMode.value = typeof mode === 'string' ? mode : 'round_robin'

  const leases = settingsStore.settings[SETTING_KEYS.MAX_LEASES]
  maxLeasesPerProxy.value = typeof leases === 'number' ? leases : 10

  const dead = settingsStore.settings[SETTING_KEYS.DEAD_THRESHOLD]
  deadThreshold.value = typeof dead === 'number' ? dead : 3

  const cooldown = settingsStore.settings[SETTING_KEYS.DEAD_COOLDOWN]
  // `0` là giá trị hợp lệ (tắt TTL) → dùng typeof để không rơi vào default
  // fallback nhầm; default backend = 120s.
  deadCooldownSeconds.value = typeof cooldown === 'number' ? cooldown : 120

  // Preflight knobs — fallback `default` khi key chưa từng set qua UI.
  const en = settingsStore.settings[SETTING_KEYS.PROBE_ENABLED]
  probeEnabled.value = typeof en === 'boolean' ? en : true

  const ep = settingsStore.settings[SETTING_KEYS.PROBE_ENDPOINT]
  probeEndpoint.value =
    typeof ep === 'string' && ep.trim().length > 0
      ? ep
      : 'https://api64.ipify.org'

  const to = settingsStore.settings[SETTING_KEYS.PROBE_TIMEOUT]
  probeTimeout.value = typeof to === 'number' ? to : 6

  const mt = settingsStore.settings[SETTING_KEYS.PROBE_MAX_TRIES]
  probeMaxTries.value = typeof mt === 'number' ? mt : 10

  const sr = settingsStore.settings[SETTING_KEYS.PROBE_SID_RETRY]
  probeSidRetry.value = typeof sr === 'number' ? sr : 2

  const pc = settingsStore.settings[SETTING_KEYS.PROBE_CONCURRENCY]
  probeConcurrency.value = typeof pc === 'number' ? pc : 5

  const fd = settingsStore.settings[SETTING_KEYS.FALLBACK_DIRECT]
  fallbackDirect.value = typeof fd === 'boolean' ? fd : true
}

// Khi modal mở lần đầu, hydrate draft từ store hiện tại. Watch reactive
// showModal để reset draft mỗi lần mở (user thoát không lưu → next open
// lấy lại state mới nhất).
watch(showModal, (isOpen) => {
  if (isOpen) {
    hydrateFromStore()
    // Reset kết quả probe cũ — mở modal lần sau không mang badge stale
    // của lần trước (dữ liệu proxy đã có thể thay đổi qua SSE / edit tab khác).
    probeResults.value = new Map()
    probeSummary.value = null
    probeProgress.value = null
  } else {
    // Đóng modal khi probe đang chạy → abort để backend cleanup task,
    // tránh tốn CPU probe tiếp cho kết quả không ai đọc.
    if (probeAbortController !== null) {
      probeAbortController.abort()
      probeAbortController = null
    }
  }
})

// -----------------------------------------------------------------------------
// Row manipulation
// -----------------------------------------------------------------------------

function handleAddRow(): void {
  rows.value.push(makeRow(''))
}

function handleRemoveRow(id: number): void {
  rows.value = rows.value.filter((r) => r.id !== id)
  if (rows.value.length === 0) rows.value.push(makeRow(''))
}

/**
 * Xoá toàn bộ proxy trong DRAFT — có confirm dialog vì destructive.
 *
 * Semantics:
 *   - Chỉ đụng draft state (rows/probeResults/probeSummary/probeProgress) —
 *     KHÔNG tự Save. User phải bấm Save để commit vào Settings_Store.
 *   - Nếu đang có probe chạy → abort trước, tránh callback stale ghi lên
 *     probeResults sau khi user đã clear.
 *   - Reset về 1 row rỗng (không phải 0 row) để UI luôn có ít nhất 1 input
 *     — khớp pattern `handleRemoveRow`.
 *   - Skip confirm khi draft đã rỗng (không có gì để xoá).
 */
function handleClearAll(): void {
  const nonEmpty = rows.value.filter((r) => r.value.trim().length > 0).length
  if (nonEmpty === 0) {
    message.info('Draft is already empty')
    return
  }
  dialog.warning({
    title: 'Clear all proxies?',
    content: `This will remove ${nonEmpty} proxy(s) from the draft. Click Save afterwards to persist. Not saving will keep the current pool intact.`,
    positiveText: 'Clear',
    negativeText: 'Cancel',
    onPositiveClick: () => {
      // Nếu có probe stream đang chạy → abort trước để callback không
      // ghi kết quả cũ vào probeResults sau reset.
      if (probeAbortController !== null) {
        probeAbortController.abort()
        probeAbortController = null
      }
      rows.value = [makeRow('')]
      probeResults.value = new Map()
      probeSummary.value = null
      probeProgress.value = null
      message.info(`Cleared ${nonEmpty} proxy(s) from draft`)
    },
  })
}

function handleOpenPaste(): void {
  pasteText.value = ''
  showPasteModal.value = true
}

function handleApplyPaste(): void {
  const lines = pasteText.value
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l.length > 0)

  const existing = new Set(
    rows.value.map((r) => r.value.trim()).filter((v) => v.length > 0),
  )
  // Loại bỏ row trống trước khi merge — user thường thêm bulk vào 1 row rỗng.
  const kept = rows.value.filter((r) => r.value.trim().length > 0)
  let added = 0
  for (const line of lines) {
    if (existing.has(line)) continue
    existing.add(line)
    kept.push(makeRow(line))
    added += 1
  }
  rows.value = kept.length > 0 ? kept : [makeRow('')]
  showPasteModal.value = false
  message.info(`Added ${added} proxy(s) (remember to click Save)`)
  // Bulk paste có thể thêm/thay đổi list → kết quả probe cũ không còn
  // đại diện. Không tự xoá per-line để user vẫn thấy line cũ đã test
  // trước đó; chỉ xoá summary (số tổng đã sai).
  probeSummary.value = null
}

// -----------------------------------------------------------------------------
// Test all — probe read-only draft danh sách proxy hiện tại. Backend
// dedupe + strip empty; FE match ngược kết quả về từng row qua raw value.
// -----------------------------------------------------------------------------

async function handleTestAll(): Promise<void> {
  if (probing.value) return

  // Lấy danh sách proxy non-empty từ draft (không dedupe ở FE — backend
  // đã dedupe, giữ nguyên input để không gây confusion).
  const proxies = rows.value
    .map((r) => r.value.trim())
    .filter((v) => v.length > 0)

  if (proxies.length === 0) {
    message.warning('No proxies to test')
    return
  }

  // Reset state cũ trước khi trigger.
  probeResults.value = new Map()
  probeSummary.value = null
  probeProgress.value = { total: proxies.length, done: 0, live: 0 }

  // AbortController để đóng modal / re-trigger giữa chừng cancel được.
  probeAbortController = new AbortController()

  try {
    const output = await probeBatchStream(
      proxies,
      {
        // Dùng endpoint + timeout + concurrency đang gõ trong form để test
        // khớp với behavior sẽ áp dụng khi save. Nếu form bị clamp ngoài
        // range 3..30 / 1..100, backend sẽ tự clamp lại.
        endpoint: probeEndpoint.value.trim() || undefined,
        timeoutSeconds: probeTimeout.value,
        concurrency: probeConcurrency.value,
      },
      {
        onStart: (info) => {
          probeProgress.value = {
            total: info.total,
            done: 0,
            live: 0,
          }
        },
        onResult: (result) => {
          // Vue reactivity: Map instance đã đăng ký watcher, nhưng để chắc
          // template re-render khi set trên Map hiện có, tạo Map mới copy
          // từ cũ. Cost O(N) mỗi result — với N=100 = 10K ops tổng, chấp
          // nhận được cho preview UI.
          const next = new Map(probeResults.value)
          next.set(result.proxy, result)
          probeResults.value = next

          if (probeProgress.value !== null) {
            probeProgress.value = {
              total: probeProgress.value.total,
              done: probeProgress.value.done + 1,
              live: probeProgress.value.live + (result.ok ? 1 : 0),
            }
          }
        },
        onAbort: () => {
          probeProgress.value = null
        },
      },
      probeAbortController.signal,
    )

    probeSummary.value = {
      live: output.live,
      total: output.total,
      elapsedMs: output.elapsedMs,
    }
    probeProgress.value = null

    if (output.live === output.total && output.total > 0) {
      message.success(`All ${output.total} proxies live (${output.elapsedMs}ms)`)
    } else if (output.live === 0) {
      message.error(`0/${output.total} proxies live — check credential / endpoint`)
    } else {
      const suffix = output.timedOut ? ' — batch timeout, some incomplete' : ''
      message.info(
        `${output.live}/${output.total} proxies live (${output.elapsedMs}ms)${suffix}`,
      )
    }
  } catch (err) {
    // AbortError đã được useProxyProbe swallow (resolve với partial output).
    // Còn lại là lỗi thực sự (HTTP/network).
    const detail = err instanceof Error ? err.message : String(err)
    message.error(`Test proxy fail: ${detail}`)
    probeProgress.value = null
  } finally {
    probeAbortController = null
  }
}

/**
 * View-model status cho 1 row — plain string/number để template không phải
 * cast type (Vue template không narrow discriminated union theo `.kind`).
 *
 * `kind`:
 *   - `idle`: không hiển thị icon (placeholder chỗ trống giữ layout).
 *   - `probing`: đang có batch chạy → spinner.
 *   - `ok`: live → CircleCheck xanh.
 *   - `fail`: reason `auth`/`ip`/`format`/... → CircleX/CircleDot với màu
 *     theo mức độ (auth = đỏ, ip = vàng, format = xám).
 */
interface RowStatusView {
  kind: 'idle' | 'probing' | 'ok' | 'fail'
  /** CSS modifier cho `.proxy-row__status`. */
  cssModifier: string
  /** Text hiển thị trong tooltip. Rỗng khi `kind=idle` (không cần tooltip). */
  tooltip: string
  /**
   * `'check'` → IconCircleCheck; `'x'` → IconCircleX; `'dot'` → IconCircleDot;
   * `'spinner'` → IconSpinner; `null` khi idle (không render icon).
   */
  icon: 'check' | 'x' | 'dot' | 'spinner' | null
}

function getRowStatus(row: ProxyRow): RowStatusView {
  const value = row.value.trim()
  if (!value) {
    return { kind: 'idle', cssModifier: 'idle', tooltip: '', icon: null }
  }
  // Result THẮNG spinner: khi stream đang chạy, row nào đã có result sẽ
  // hiển thị icon result ngay (không chờ hết batch). Row chưa có result và
  // đang probing → spinner. Row không có result và không probing → idle.
  const result = probeResults.value.get(value)
  if (result) {
    // fall-through xuống block xử lý result bên dưới
  } else if (probing.value) {
    return { kind: 'probing', cssModifier: 'spin', tooltip: 'Probing...', icon: 'spinner' }
  } else {
    return { kind: 'idle', cssModifier: 'idle', tooltip: '', icon: null }
  }
  if (!result) {
    return { kind: 'idle', cssModifier: 'idle', tooltip: '', icon: null }
  }
  if (result.ok) {
    return {
      kind: 'ok',
      cssModifier: 'ok',
      tooltip: `Live · ${result.latency_ms}ms · ${result.proxy_masked}`,
      icon: 'check',
    }
  }
  const errorDetail = result.error || result.reason
  const cssModifier =
    result.reason === 'auth'
      ? 'auth'
      : result.reason === 'format'
        ? 'format'
        : 'ip'
  return {
    kind: 'fail',
    cssModifier,
    tooltip: `${errorDetail} · ${result.latency_ms}ms`,
    icon: result.reason === 'format' ? 'dot' : 'x',
  }
}

function statusIconComponent(icon: RowStatusView['icon']): typeof IconCircleCheck | null {
  if (icon === 'check') return IconCircleCheck
  if (icon === 'x') return IconCircleX
  if (icon === 'dot') return IconCircleDot
  if (icon === 'spinner') return IconSpinner
  return null
}

// -----------------------------------------------------------------------------
// Save — bulkUpdate 4 key trong 1 transaction.
// -----------------------------------------------------------------------------

async function handleSave(): Promise<void> {
  if (saving.value) return
  saving.value = true

  const proxies = rows.value
    .map((r) => r.value.trim())
    .filter((v) => v.length > 0)
  // Dedupe (giữ thứ tự) — backend cũng lưu dạng list, user không cần biết
  // trước khi save.
  const seen = new Set<string>()
  const deduped: string[] = []
  for (const p of proxies) {
    if (!seen.has(p)) {
      seen.add(p)
      deduped.push(p)
    }
  }

  try {
    const ok = await settingsStore.bulkUpdate({
      [SETTING_KEYS.LIST]: deduped,
      [SETTING_KEYS.ROTATION_MODE]: rotationMode.value,
      [SETTING_KEYS.MAX_LEASES]: maxLeasesPerProxy.value,
      [SETTING_KEYS.DEAD_THRESHOLD]: deadThreshold.value,
      [SETTING_KEYS.DEAD_COOLDOWN]: deadCooldownSeconds.value,
      // Preflight probe knobs — save cùng transaction để bulkUpdate atomic.
      [SETTING_KEYS.PROBE_ENABLED]: probeEnabled.value,
      [SETTING_KEYS.PROBE_ENDPOINT]: probeEndpoint.value.trim(),
      [SETTING_KEYS.PROBE_TIMEOUT]: probeTimeout.value,
      [SETTING_KEYS.PROBE_MAX_TRIES]: probeMaxTries.value,
      [SETTING_KEYS.PROBE_SID_RETRY]: probeSidRetry.value,
      [SETTING_KEYS.PROBE_CONCURRENCY]: probeConcurrency.value,
      [SETTING_KEYS.FALLBACK_DIRECT]: fallbackDirect.value,
    })
    if (ok) {
      message.success(`Saved ${deduped.length} proxy(s)`)
      showModal.value = false
    }
    // Nếu ok === false, settingsStore.error đã set → AppErrorNotifier toast.
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="proxy-config">
    <button
      type="button"
      class="proxy-config__toggle"
      :aria-expanded="showModal"
      :title="`Configure proxy · ${persistedProxyCount} proxy(s) saved`"
      @click="showModal = true"
    >
      <n-icon :component="IconServer" size="15" class="toggle-icon" />
      <span class="toggle-label">Proxy</span>
      <span
        v-if="persistedProxyCount > 0"
        class="toggle-badge"
        :aria-label="`${persistedProxyCount} proxy`"
      >
        {{ persistedProxyCount }}
      </span>
    </button>

    <n-modal
      v-model:show="showModal"
      preset="card"
      title="Configure proxy pool"
      class="proxy-modal"
      :bordered="false"
      :segmented="{ content: 'soft', footer: 'soft' }"
      :closable="true"
      :mask-closable="false"
      :close-on-esc="true"
      style="width: 720px; max-width: 95vw"
    >
      <div class="proxy-modal__body">
        <div class="proxy-modal__toolbar">
          <n-space :size="8">
            <n-button size="small" @click="handleAddRow">
              <template #icon>
                <n-icon :component="IconPlus" />
              </template>
              Add proxy
            </n-button>
            <n-button size="small" quaternary @click="handleOpenPaste">
              <template #icon>
                <n-icon :component="IconClipboardList" />
              </template>
              Bulk paste
            </n-button>
            <n-button
              size="small"
              quaternary
              :loading="probing"
              :disabled="totalProxies === 0"
              :title="
                totalProxies === 0
                  ? 'Add a proxy before testing'
                  : `Test ${totalProxies} proxy(s) (preview only, not saved)`
              "
              @click="handleTestAll"
            >
              <template #icon>
                <n-icon :component="IconWifi" />
              </template>
              Test all
            </n-button>
            <n-button
              size="small"
              quaternary
              type="error"
              :disabled="totalProxies === 0"
              :title="
                totalProxies === 0
                  ? 'Draft is empty'
                  : `Remove all ${totalProxies} proxy(s) from draft (needs Save to persist)`
              "
              @click="handleClearAll"
            >
              <template #icon>
                <n-icon :component="IconTrash" />
              </template>
              Clear all
            </n-button>
          </n-space>
          <n-badge
            :value="totalProxies"
            type="info"
            show-zero
            :max="999"
            :title="`${totalProxies} proxy(s) in draft`"
          />
        </div>

        <!--
          Progress bar realtime — hiện KHI đang probing (probeProgress != null).
          Cập nhật mỗi lần onResult callback bắn: `done` tăng, `live` tăng
          nếu ok. Ngưỡng < 100% có màu vàng, đạt 100% chuyển màu xanh trong
          nháy mắt trước khi probeSummary thay thế.
        -->
        <div v-if="probeProgress !== null" class="probe-summary probe-summary--progress">
          <span
            class="probe-summary__badge"
            :class="{
              'probe-summary__badge--ok':
                probeProgress.done === probeProgress.total &&
                probeProgress.live === probeProgress.total,
              'probe-summary__badge--partial':
                probeProgress.done < probeProgress.total ||
                (probeProgress.live > 0 && probeProgress.live < probeProgress.total),
              'probe-summary__badge--none':
                probeProgress.done === probeProgress.total && probeProgress.live === 0,
            }"
          >
            {{ probeProgress.live }} / {{ probeProgress.total }} live
          </span>
          <span class="probe-progress__bar" :title="`${probeProgress.done}/${probeProgress.total} probed`">
            <span
              class="probe-progress__bar-fill"
              :style="{ width: `${probeProgressPercent}%` }"
            />
          </span>
          <n-text depth="3" style="font-size: 11.5px">
            {{ probeProgress.done }} / {{ probeProgress.total }} probed
          </n-text>
        </div>

        <!--
          Summary bar — hiện SAU khi Test all xong (probeSummary != null).
          `probeSummary` reset khi mở lại modal hoặc khi paste bulk (list
          thay đổi, số cũ vô nghĩa).
        -->
        <div v-else-if="probeSummary !== null" class="probe-summary">
          <span
            class="probe-summary__badge"
            :class="{
              'probe-summary__badge--ok': probeSummary.live === probeSummary.total,
              'probe-summary__badge--partial':
                probeSummary.live > 0 && probeSummary.live < probeSummary.total,
              'probe-summary__badge--none': probeSummary.live === 0,
            }"
          >
            {{ probeSummary.live }} / {{ probeSummary.total }} live
          </span>
          <n-text depth="3" style="font-size: 11.5px">
            in {{ probeSummary.elapsedMs }}ms · endpoint
            <code class="mono">{{ probeEndpoint }}</code>
          </n-text>
        </div>

        <div class="proxy-modal__rows">
          <div v-for="(row, idx) in rows" :key="row.id" class="proxy-row">
            <span class="proxy-row__idx mono">{{ idx + 1 }}</span>
            <n-input
              v-model:value="row.value"
              size="small"
              class="proxy-row__input mono"
              placeholder="http://user:pass@host:port  or  host:port:user-{SID}:pass"
              spellcheck="false"
            />
            <!--
              Status icon từ kết quả Test all. `idle` → placeholder trống
              (giữ layout ổn định, không nhảy chỗ khi test).
            -->
            <span
              v-if="getRowStatus(row).icon === null"
              class="proxy-row__status proxy-row__status--idle"
            />
            <n-tooltip v-else trigger="hover" placement="top">
              <template #trigger>
                <n-icon
                  :component="statusIconComponent(getRowStatus(row).icon)!"
                  size="16"
                  class="proxy-row__status"
                  :class="`proxy-row__status--${getRowStatus(row).cssModifier}`"
                />
              </template>
              {{ getRowStatus(row).tooltip }}
            </n-tooltip>
            <n-button
              size="tiny"
              quaternary
              circle
              type="error"
              title="Remove row"
              @click="handleRemoveRow(row.id)"
            >
              <template #icon>
                <n-icon :component="IconTrash" />
              </template>
            </n-button>
          </div>
        </div>

        <!-- Advanced settings — rotation mode + lease/dead threshold. -->
        <fieldset class="proxy-modal__adv">
          <legend>Advanced</legend>
          <div class="grid-4">
            <div class="field">
              <label>Rotation mode</label>
              <n-select
                v-model:value="rotationMode"
                :options="ROTATION_OPTIONS"
                size="small"
              />
            </div>
            <div class="field">
              <label>Concurrent leases / proxy</label>
              <n-input-number
                v-model:value="maxLeasesPerProxy"
                :min="1"
                :max="100"
                size="small"
                :show-button="false"
              />
            </div>
            <div class="field">
              <label>Dead threshold</label>
              <n-input-number
                v-model:value="deadThreshold"
                :min="1"
                :max="20"
                size="small"
                :show-button="false"
              />
            </div>
            <div class="field">
              <label>Dead cooldown (seconds)</label>
              <n-input-number
                v-model:value="deadCooldownSeconds"
                :min="0"
                :max="3600"
                size="small"
                :show-button="false"
                placeholder="120"
              />
              <!--
                Tooltip nội tuyến: proxy bị mark_dead sẽ tự sống lại
                sau N giây. `0` = tắt TTL (dead vĩnh viễn cho tới khi
                user bấm mark_alive thủ công). Default backend = 120s.
              -->
              <small class="field__hint">
                Auto-revive after N seconds. `0` = never (manual only).
              </small>
            </div>
          </div>
        </fieldset>

        <!--
          Preflight probe — check proxy live TRƯỚC khi job chạy.
          - `probe_enabled` off → dùng path acquire cũ (không probe).
          - `probe_endpoint` → target probe. Default ipify (không rate-limit,
            không fingerprint-gate).
          - `fallback_direct_on_exhausted` → khi hết proxy live, chạy Direct
            (rủi ro lộ IP thật) thay vì fail job.
        -->
        <fieldset class="proxy-modal__adv">
          <legend>Preflight probe</legend>
          <div class="grid-2-toggle">
            <label class="checkbox-row">
              <input v-model="probeEnabled" type="checkbox" />
              <span>Enable preflight probe (recommended)</span>
            </label>
            <label class="checkbox-row">
              <input v-model="fallbackDirect" type="checkbox" />
              <span>Fallback to Direct when no proxy is live</span>
            </label>
          </div>
          <div class="grid-2" style="margin-top: 8px">
            <div class="field field--wide">
              <label>Probe endpoint</label>
              <n-input
                v-model:value="probeEndpoint"
                size="small"
                placeholder="https://api64.ipify.org"
                spellcheck="false"
                class="mono"
                :disabled="!probeEnabled"
              />
            </div>
            <div class="field">
              <label>Probe timeout (seconds)</label>
              <n-input-number
                v-model:value="probeTimeout"
                :min="3"
                :max="30"
                size="small"
                :show-button="false"
                :disabled="!probeEnabled"
              />
            </div>
          </div>
          <div class="grid-3" style="margin-top: 8px">
            <div class="field">
              <label>Max tries / job</label>
              <n-input-number
                v-model:value="probeMaxTries"
                :min="1"
                :max="20"
                size="small"
                :show-button="false"
                :disabled="!probeEnabled"
              />
            </div>
            <div class="field">
              <label>SID retry / line</label>
              <n-input-number
                v-model:value="probeSidRetry"
                :min="0"
                :max="10"
                size="small"
                :show-button="false"
                :disabled="!probeEnabled"
              />
            </div>
            <div class="field">
              <label>Probe concurrency</label>
              <n-input-number
                v-model:value="probeConcurrency"
                :min="1"
                :max="100"
                size="small"
                :show-button="false"
                :disabled="!probeEnabled"
              />
            </div>
          </div>
        </fieldset>

        <p class="proxy-modal__hint">
          <n-text depth="3" style="font-size: 11px">
            Use
            <code class="mono">{SID}</code>
            in the template to insert a random session id per lease (e.g.
            <code class="mono">user-{SID}:pass@host:port</code>).
          </n-text>
        </p>
      </div>

      <template #footer>
        <div class="proxy-modal__footer">
          <n-text depth="3" style="font-size: 12px">
            {{ totalProxies }} proxy(s) · rotation:
            <strong>{{ rotationMode }}</strong>
          </n-text>
          <n-space :size="8">
            <n-button size="small" quaternary @click="showModal = false">
              Close
            </n-button>
            <n-button
              type="primary"
              size="small"
              :loading="saving"
              @click="handleSave"
            >
              Save
            </n-button>
          </n-space>
        </div>
      </template>
    </n-modal>

    <!-- Sub-modal Bulk paste -->
    <n-modal
      v-model:show="showPasteModal"
      preset="card"
      title="Paste proxy list"
      style="width: 520px; max-width: 90vw"
      :closable="true"
      :mask-closable="true"
      :close-on-esc="true"
    >
      <n-input
        v-model:value="pasteText"
        type="textarea"
        class="mono"
        :autosize="{ minRows: 8, maxRows: 16 }"
        placeholder="One proxy per line. Example:
http://user:pass@1.2.3.4:8080
http://user-{SID}:pass@1.2.3.4:8080"
        spellcheck="false"
      />
      <template #footer>
        <div class="paste-footer">
          <n-button size="small" quaternary @click="showPasteModal = false">
            Cancel
          </n-button>
          <n-button type="primary" size="small" @click="handleApplyPaste">
            Apply
          </n-button>
        </div>
      </template>
    </n-modal>
  </div>
</template>

<style scoped>
.proxy-config {
  display: inline-flex;
  align-items: center;
}

.proxy-config__toggle {
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

.proxy-config__toggle:hover {
  background: var(--surface-3);
  border-color: var(--brand-primary);
}

.proxy-config__toggle:focus-visible {
  outline: 2px solid var(--brand-primary);
  outline-offset: 2px;
}

.toggle-icon {
  color: var(--brand-primary);
  flex-shrink: 0;
}

.toggle-label {
  white-space: nowrap;
}

.toggle-badge {
  position: absolute;
  top: -6px;
  right: -6px;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--accent-blue, #3b82f6);
  color: #ffffff;
  font-size: 10px;
  font-weight: 700;
  line-height: 18px;
  text-align: center;
  border: 2px solid var(--surface-1);
  font-variant-numeric: tabular-nums;
}

.proxy-modal__body {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-height: 65vh;
}

.proxy-modal__toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.proxy-modal__rows {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 0;
  border-top: 1px dashed var(--n-divider-color);
  border-bottom: 1px dashed var(--n-divider-color);
  max-height: 320px;
  overflow-y: auto;
}

.proxy-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.proxy-row__idx {
  flex: 0 0 24px;
  color: var(--n-text-color-3);
  font-size: 11px;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.proxy-row__input {
  flex: 1 1 auto;
  min-width: 0;
}

.proxy-row__status {
  flex: 0 0 20px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--n-text-color-3);
}

.proxy-row__status--idle {
  /* Placeholder giữ chỗ, không icon, không màu — để layout row không nhảy
     khi Test all bật/tắt trạng thái. */
  width: 20px;
  height: 16px;
}

.proxy-row__status--ok {
  color: var(--n-success-color, #18a058);
}

.proxy-row__status--ip {
  color: var(--n-warning-color, #f0a020);
}

.proxy-row__status--auth {
  color: var(--n-error-color, #d03050);
}

.proxy-row__status--format {
  color: var(--n-text-color-3);
}

.proxy-row__status--spin {
  color: var(--brand-primary, #3b82f6);
  animation: proxy-spin 0.9s linear infinite;
}

@keyframes proxy-spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

.probe-summary {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  background: var(--n-action-color);
  border-radius: 6px;
  font-size: 12px;
}

.probe-summary__badge {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 999px;
  font-weight: 600;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  background: var(--n-tag-color);
  color: var(--n-text-color);
}

.probe-summary__badge--ok {
  background: rgba(24, 160, 88, 0.16);
  color: var(--n-success-color, #18a058);
}

.probe-summary__badge--partial {
  background: rgba(240, 160, 32, 0.16);
  color: var(--n-warning-color, #f0a020);
}

.probe-summary__badge--none {
  background: rgba(208, 48, 80, 0.16);
  color: var(--n-error-color, #d03050);
}

.probe-summary--progress {
  /* Progress mode có thêm bar chiếm không gian giữa badge và text — dùng
     flex 1 để bar tự giãn theo width container. */
}

.probe-progress__bar {
  flex: 1 1 auto;
  min-width: 60px;
  height: 6px;
  background: var(--n-tag-color);
  border-radius: 3px;
  overflow: hidden;
  position: relative;
}

.probe-progress__bar-fill {
  display: block;
  height: 100%;
  background: var(--brand-primary, #3b82f6);
  transition: width 0.15s ease-out;
}

.probe-summary code {
  padding: 1px 4px;
  background: var(--n-color);
  border-radius: 3px;
  font-size: 10.5px;
}

.proxy-modal__adv {
  border: 1px solid var(--n-border-color);
  border-radius: 8px;
  padding: 10px 14px 12px;
  margin: 0;
}

.proxy-modal__adv legend {
  padding: 2px 8px;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.6px;
  color: var(--n-text-color-2);
}

.grid-3 {
  display: grid;
  grid-template-columns: 1.5fr 1fr 1fr;
  gap: 10px;
}

/*
 * `grid-4` cho hàng Advanced (Rotation + Max leases + Dead threshold +
 * Dead cooldown). Chia 2 dòng × 2 cột cho dễ đọc, tránh 4 field bị ép
 * hẹp trong modal 720px — Rotation-select và Dead cooldown hint có chỗ
 * thở, không bị wrap label.
 */
.grid-4 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.grid-2 {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: 10px;
}

.grid-2-toggle {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.field--wide {
  grid-column: span 1;
}

.checkbox-row {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--n-text-color-2);
  cursor: pointer;
  user-select: none;
}

.checkbox-row input[type='checkbox'] {
  width: 14px;
  height: 14px;
  cursor: pointer;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.field label {
  font-size: 11px;
  font-weight: 500;
  color: var(--n-text-color-2);
}

.field__hint {
  font-size: 10px;
  color: var(--n-text-color-3);
  line-height: 1.3;
  margin-top: 2px;
}

.proxy-modal__hint {
  margin: 0;
}

.proxy-modal__hint code {
  padding: 1px 4px;
  background: var(--n-action-color);
  color: var(--n-text-color);
  border-radius: 3px;
  font-size: 10.5px;
}

.proxy-modal__footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
}

.paste-footer {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}

.mono {
  font-family: var(--n-font-family-mono);
}

.mono :deep(input),
.mono :deep(textarea) {
  font-family: var(--n-font-family-mono);
  font-size: 11.5px;
}

@media (max-width: 640px) {
  .grid-3,
  .grid-4,
  .grid-2,
  .grid-2-toggle {
    grid-template-columns: 1fr;
  }
}
</style>
