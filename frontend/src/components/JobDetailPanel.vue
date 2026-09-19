<!--
  JobDetailPanel — chi tiết 1 IdealJob (log viewer + QR + actions).

  Requirements:
  - 12.3: cho phép user mở nhiều panel đồng thời (parent App.vue quyết định).
  - 12.6: ảnh QR container tối thiểu 240×240 pixel trên mobile (đủ để quét
    bằng camera điện thoại). Rule CSS `qr-image` min-width/min-height 240px.

  Root class BẮT BUỘC giữ: `.job-detail-panel` (test snapshot query).
  CSS BẮT BUỘC giữ: rule liên quan `.qr-image` với min-width/min-height ≥240px.

  Actions bar:
  - Copy Job ID
  - Copy payment link (khi qr_ready & có link)
  - Copy QR image (khi qr_ready)
  - Download PNG (khi qr_ready)
  - Close

  QR image (R14.5): fetch blob →
  `URL.createObjectURL(blob)` → `<img :src>`. Revoke khi unmount / URL đổi.

  Log color coding: heuristic phân loại severity từ keyword message (error /
  warn / info). Vì backend không gắn `level` vào từng log entry (design.md
  giữ log format tối giản), FE dựa vào regex keyword — nếu backend sau này
  thêm `extra.level` sẽ prefer field đó.
-->
<script setup lang="ts">
import {
  computed,
  nextTick,
  onMounted,
  onUnmounted,
  ref,
  watch,
  type Ref,
} from 'vue'
import {
  NAlert,
  NButton,
  NIcon,
  NSpace,
  NSpin,
  NTag,
  NText,
  useMessage,
} from 'naive-ui'

import {
  useJobsStore,
  type JobLogEntry,
  type JobStatus,
} from '../composables/useJobsStore'
import {
  IconCopy,
  IconDownload,
  IconQrcode,
  IconX,
} from '../icons'

interface Props {
  jobId: string
}

const props = defineProps<Props>()

const emit = defineEmits<{ (e: 'close'): void }>()

const jobsStore = useJobsStore()
const message = useMessage()

const job = computed(() => jobsStore.jobs.get(props.jobId))

const logContainer: Ref<HTMLDivElement | null> = ref(null)
const qrBlobUrl: Ref<string | null> = ref(null)
const qrError: Ref<string | null> = ref(null)
const qrLoading: Ref<boolean> = ref(false)

// ---------------------------------------------------------------------------
// QR image auth-fetch
// ---------------------------------------------------------------------------
const qrSourceUrl = computed<string | null>(() => {
  if (!job.value) return null
  if (job.value.status !== 'qr_ready') return null
  if (job.value.payment_method === 'gcash_direct') {
    if (!job.value.artifact_path) return null
  } else if (!job.value.artifact_path && !job.value.payment_link) {
    return null
  }
  return `/api/jobs/${encodeURIComponent(props.jobId)}/qr.png`
})

const hasViewableQr = computed<boolean>(() => qrSourceUrl.value !== null)

function revokeCurrentBlobUrl(): void {
  if (qrBlobUrl.value !== null) {
    URL.revokeObjectURL(qrBlobUrl.value)
    qrBlobUrl.value = null
  }
}

async function loadQrBlob(sourceUrl: string): Promise<void> {
  qrError.value = null
  qrLoading.value = true
  try {
    const res = await fetch(sourceUrl, {
      method: 'GET',
      headers: { Accept: 'image/png' },
    })
    if (!res.ok) {
      let errorCode = `HTTP ${res.status}`
      try {
        const body = (await res.clone().json()) as {
          detail?: { error_code?: string; message?: string }
          error_code?: string
          message?: string
        }
        const detail = body.detail
        const code =
          (detail && typeof detail === 'object' && typeof detail.error_code === 'string'
            ? detail.error_code
            : undefined) ??
          (typeof body.error_code === 'string' ? body.error_code : undefined)
        if (code) errorCode = code
      } catch {
        /* not JSON — keep HTTP status */
      }
      qrError.value = errorCode
      return
    }
    const blob = await res.blob()
    revokeCurrentBlobUrl()
    qrBlobUrl.value = URL.createObjectURL(blob)
  } catch (ex) {
    qrError.value = ex instanceof Error ? ex.message : String(ex)
  } finally {
    qrLoading.value = false
  }
}

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------
onMounted(async () => {
  try {
    await jobsStore.loadDetail(props.jobId)
  } catch {
    /* jobsStore.error đã set */
  }
})

watch(
  qrSourceUrl,
  async (newUrl, oldUrl) => {
    if (newUrl === oldUrl) return
    if (newUrl === null) {
      revokeCurrentBlobUrl()
      qrError.value = null
      return
    }
    await loadQrBlob(newUrl)
  },
  { immediate: true },
)

watch(
  () => job.value?.logs?.length ?? 0,
  async () => {
    await nextTick()
    const el = logContainer.value
    if (el !== null) el.scrollTop = el.scrollHeight
  },
)

onUnmounted(() => {
  revokeCurrentBlobUrl()
})

// ---------------------------------------------------------------------------
// Log presentation
// ---------------------------------------------------------------------------
const logs = computed<JobLogEntry[]>(() => job.value?.logs ?? [])

/**
 * Số notify Telegram thành công — dùng cho section header (`n/total
 * thành công`). Fail entry vẫn được hiển thị trong list nhưng không
 * tính vào tử số.
 */
const telegramSuccessCount = computed<number>(() => {
  const entries = job.value?.telegram_notifications ?? []
  return entries.reduce((n, e) => n + (e.success ? 1 : 0), 0)
})

function formatTs(tsSeconds: number): string {
  const d = new Date(tsSeconds * 1000)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  return `${hh}:${mm}:${ss}`
}

/**
 * Phân loại severity của 1 log entry để color-code UI. Ưu tiên
 * `extra.level` nếu backend gắn (forward-compatible); fallback keyword
 * heuristic. KHÔNG throw — worst case trả 'info' để render mặc định.
 */
function logKind(entry: JobLogEntry): 'error' | 'warning' | 'info' | 'success' {
  const level = entry.extra?.level
  if (typeof level === 'string') {
    const l = level.toLowerCase()
    if (l === 'error' || l === 'critical' || l === 'fatal') return 'error'
    if (l === 'warning' || l === 'warn') return 'warning'
    if (l === 'success') return 'success'
  }
  const msg = (entry.message || '').toLowerCase()
  if (/\berror\b|\bfail(ed|ure)?\b|\bexception\b|not_found|invalid|forbidden|denied|timeout|exhausted/.test(msg)) {
    return 'error'
  }
  if (/\bwarn(ing)?\b|retry|stall|slow|reconnect/.test(msg)) {
    return 'warning'
  }
  if (/\b(qr_ready|success|completed|ready|resolved|ok)\b/.test(msg)) {
    return 'success'
  }
  return 'info'
}

// ---------------------------------------------------------------------------
// Status tag mapping
// ---------------------------------------------------------------------------
type TagType = 'default' | 'info' | 'success' | 'warning' | 'error'
function statusTagType(s: JobStatus | undefined): TagType {
  switch (s) {
    case 'pending': return 'default'
    case 'running': return 'info'
    case 'qr_ready': return 'success'
    case 'error': return 'error'
    case 'stopped': return 'warning'
    default: return 'default'
  }
}

function statusLabel(s: JobStatus | undefined): string {
  switch (s) {
    case 'pending': return 'pending'
    case 'running': return 'running'
    case 'qr_ready': return 'QR ready'
    case 'error': return 'error'
    case 'stopped': return 'stopped'
    default: return 'unknown'
  }
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------
async function copyText(text: string, okMsg: string): Promise<void> {
  if (typeof navigator === 'undefined' || !navigator.clipboard) {
    message.warning('Browser does not support clipboard')
    return
  }
  try {
    await navigator.clipboard.writeText(text)
    message.success(okMsg)
  } catch (ex) {
    message.error(`Copy failed: ${ex instanceof Error ? ex.message : String(ex)}`)
  }
}

async function handleCopyJobId(): Promise<void> {
  await copyText(props.jobId, 'Copied Job ID')
}

async function handleCopyLink(): Promise<void> {
  const link = job.value?.payment_link
  if (!link) return
  await copyText(link, 'Copied payment link')
}

async function handleCopyQr(): Promise<void> {
  if (
    typeof navigator === 'undefined' ||
    !navigator.clipboard ||
    typeof ClipboardItem === 'undefined' ||
    typeof navigator.clipboard.write !== 'function'
  ) {
    message.warning('Browser does not support copying images')
    return
  }
  if (!qrBlobUrl.value) {
    message.warning('QR not ready yet')
    return
  }
  try {
    // Refetch blob thay vì reuse blob URL — an toàn cross-origin và có
    // MIME type chính xác cho ClipboardItem.
    const src = qrSourceUrl.value
    if (!src) return
    const res = await fetch(src, {
      method: 'GET',
      headers: { Accept: 'image/png' },
    })
    if (!res.ok) {
      message.error(`Failed to load QR (HTTP ${res.status})`)
      return
    }
    const blob = await res.blob()
    const item = new ClipboardItem({ [blob.type || 'image/png']: blob })
    await navigator.clipboard.write([item])
    message.success('Copied QR to clipboard')
  } catch (ex) {
    message.error(`Failed to copy QR: ${ex instanceof Error ? ex.message : String(ex)}`)
  }
}

/**
 * Download PNG file về máy user. Dùng `<a download>` với blob URL —
 * KHÔNG dùng `qrBlobUrl` hiện tại vì tên file dựa vào job_id cần đặt tại
 * moment download (URL.createObjectURL của blob dùng lại được nhưng
 * revoke ngay sau click để không leak).
 */
async function handleDownload(): Promise<void> {
  const src = qrSourceUrl.value
  if (!src) return
  try {
    const res = await fetch(src, {
      method: 'GET',
      headers: { Accept: 'image/png' },
    })
    if (!res.ok) {
      message.error(`Failed to load QR (HTTP ${res.status})`)
      return
    }
    const blob = await res.blob()
    const objectUrl = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = objectUrl
    a.download = `ideal-qr-${props.jobId}.png`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    // Revoke sau tick để browser kịp trigger download.
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
    message.success('Downloaded QR PNG')
  } catch (ex) {
    message.error(`Download failed: ${ex instanceof Error ? ex.message : String(ex)}`)
  }
}
</script>

<template>
  <section class="job-detail-panel" :aria-label="`Job details ${jobId}`">
    <header class="panel-header">
      <div class="panel-title">
        <n-icon :component="IconQrcode" size="16" class="panel-title__icon" />
        <span class="panel-title__label">Job</span>
        <code
          class="panel-title__id mono"
          title="Click to copy Job ID"
          @click="handleCopyJobId"
        >{{ jobId }}</code>
        <n-tag
          :type="statusTagType(job?.status)"
          size="small"
          round
          :bordered="false"
        >
          {{ statusLabel(job?.status) }}
        </n-tag>
        <n-tag
          v-if="job?.account_masked"
          size="small"
          round
          :bordered="false"
          class="account-tag mono"
        >
          {{ job.account_masked }}
        </n-tag>
      </div>
      <n-space :size="4" :wrap="false">
        <n-button
          v-if="job?.status === 'qr_ready' && job.payment_link"
          size="small"
          quaternary
          type="success"
          title="Copy payment link"
          @click="handleCopyLink"
        >
          <template #icon>
            <n-icon :component="IconCopy" />
          </template>
          Copy link
        </n-button>
        <n-button
          v-if="hasViewableQr"
          size="small"
          quaternary
          type="success"
          title="Copy QR image to clipboard"
          @click="handleCopyQr"
        >
          <template #icon>
            <n-icon :component="IconQrcode" />
          </template>
          Copy QR
        </n-button>
        <n-button
          v-if="hasViewableQr"
          size="small"
          quaternary
          type="info"
          title="Download PNG file"
          @click="handleDownload"
        >
          <template #icon>
            <n-icon :component="IconDownload" />
          </template>
          Download PNG
        </n-button>
        <n-button
          size="small"
          quaternary
          circle
          aria-label="Close panel"
          @click="emit('close')"
        >
          <template #icon>
            <n-icon :component="IconX" />
          </template>
        </n-button>
      </n-space>
    </header>

    <n-alert
      v-if="job?.error_code"
      type="error"
      size="small"
      :show-icon="false"
      class="error-box"
    >
      <div class="error-body">
        <code class="mono error-code">{{ job.error_code }}</code>
        <span v-if="job.error_message" class="error-msg">
          {{ job.error_message }}
        </span>
      </div>
    </n-alert>

    <!--
      Section "Đã gửi Telegram" — hiển thị đầy đủ history notify cho job
      này. Trong JobList chỉ có badge count + tooltip 5 entry gần nhất; ở
      đây user thấy TOÀN BỘ (thứ tự thời gian ASC, entry cũ nhất trước).
      Ẩn khi rỗng để tránh section trống chiếm chỗ.
    -->
    <div
      v-if="(job?.telegram_notifications?.length ?? 0) > 0"
      class="telegram-section"
    >
      <div class="section-head">
        <span class="section-title">Telegram sent</span>
        <n-text
          depth="3"
          style="font-size: 10.5px; font-variant-numeric: tabular-nums"
        >
          {{ telegramSuccessCount }}/{{ job!.telegram_notifications!.length }}
          succeeded
        </n-text>
      </div>
      <ul class="telegram-list">
        <li
          v-for="(entry, idx) in job!.telegram_notifications!"
          :key="`${entry.chat_id}-${entry.sent_at}-${idx}`"
          class="telegram-item"
          :data-success="entry.success ? 'true' : 'false'"
        >
          <span class="telegram-item__status">
            {{ entry.success ? '✓' : '✗' }}
          </span>
          <span class="telegram-item__label">
            {{ entry.chat_label || entry.chat_id }}
          </span>
          <span
            v-if="entry.chat_label"
            class="telegram-item__id mono muted"
          >
            {{ entry.chat_id }}
          </span>
          <span class="telegram-item__ts mono muted">
            {{ new Date(entry.sent_at * 1000).toLocaleString() }}
          </span>
          <span
            v-if="(entry.attempts ?? 1) > 1"
            class="telegram-item__attempts mono muted"
            :title="`Đã thử gửi ${entry.attempts} lần`"
          >
            ×{{ entry.attempts }}
          </span>
          <span
            v-if="!entry.success && entry.error"
            class="telegram-item__error"
            :title="entry.error.description"
          >
            {{
              entry.error.error_code
                ? `[${entry.error.error_code}] ${entry.error.description}`
                : entry.error.description
            }}
          </span>
        </li>
      </ul>
    </div>

    <div class="panel-body">
      <div class="log-section">
        <div class="section-head">
          <span class="section-title">Log</span>
          <n-text depth="3" style="font-size: 10.5px; font-variant-numeric: tabular-nums">
            {{ logs.length }} line(s)
          </n-text>
        </div>
        <div
          ref="logContainer"
          class="log-viewer"
          role="log"
          aria-live="polite"
        >
          <div
            v-for="(entry, idx) in logs"
            :key="`${entry.ts}-${idx}`"
            class="log-entry"
            :data-kind="logKind(entry)"
          >
            <span class="log-ts">{{ formatTs(entry.ts) }}</span>
            <span class="log-dot" :aria-hidden="true" />
            <span class="log-message">{{ entry.message }}</span>
          </div>
          <div v-if="logs.length === 0" class="log-empty">
            No logs yet.
          </div>
        </div>
      </div>

      <div class="qr-section">
        <div class="section-head">
          <span class="section-title">QR code</span>
          <n-text
            v-if="job?.status === 'qr_ready'"
            depth="3"
            style="font-size: 10.5px"
          >
            Scan with iDEAL / bank app
          </n-text>
        </div>
        <div class="qr-container">
          <img
            v-if="qrBlobUrl"
            :src="qrBlobUrl"
            alt="iDEAL payment QR code"
            class="qr-image"
          />
          <div v-else-if="qrLoading" class="qr-placeholder">
            <n-spin size="small" />
            <span>Loading QR…</span>
          </div>
          <div v-else-if="qrError" class="qr-error" role="alert">
            <span>Failed to load QR:</span>
            <code class="mono">{{ qrError }}</code>
          </div>
          <div v-else class="qr-placeholder">
            <n-icon :component="IconQrcode" size="32" class="qr-placeholder__icon" />
            <span>Not ready yet</span>
            <span v-if="job?.status" class="qr-placeholder-status mono">
              status: {{ job.status }}
            </span>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
/*
 * Panel — card độc lập trên nền main; nhiều panel có thể hiển thị đồng
 * thời (R12.3). Border + subtle shadow + gradient border tinh tế.
 */
.job-detail-panel {
  border: 1px solid var(--n-border-color, #22304a);
  border-radius: 8px;
  background: var(--n-card-color, #111823);
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.35);
  min-width: 0;
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.panel-title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.45px;
}

.panel-title__icon {
  color: var(--n-primary-color);
  flex-shrink: 0;
}

.panel-title__label {
  color: var(--n-text-color-3);
}

.panel-title__id {
  padding: 2px 8px;
  border-radius: 4px;
  background: var(--n-action-color);
  color: var(--n-text-color-2);
  font-size: 10.5px;
  text-transform: none;
  letter-spacing: 0;
  cursor: copy;
  word-break: break-all;
  max-width: 100%;
  border: 1px solid var(--n-border-color);
  transition: background 0.15s ease;
}

.panel-title__id:hover {
  background: var(--n-hover-color);
  color: var(--n-text-color);
}

.account-tag {
  font-size: 10.5px !important;
  text-transform: none !important;
  letter-spacing: 0 !important;
}

.error-box {
  font-size: 12px;
}

.error-body {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.error-code {
  padding: 1px 6px;
  border-radius: 4px;
  background: rgba(239, 68, 68, 0.2);
  border: 1px solid rgba(239, 68, 68, 0.4);
  color: #fca5a5;
  font-size: 11px;
}

.error-msg {
  color: var(--n-text-color-2);
  word-break: break-word;
}

/*
 * Section "Đã gửi Telegram" — hiển thị history đầy đủ. Bố cục dạng list
 * flat, mỗi entry 1 dòng: icon success/fail + label + chat_id + thời
 * gian + error (nếu fail). Font 12px, giống error-box.
 */
.telegram-section {
  font-size: 12px;
  padding: 6px 8px;
  border-radius: 6px;
  background: rgba(24, 160, 88, 0.08);
  border: 1px solid rgba(24, 160, 88, 0.2);
}
.telegram-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.telegram-item {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding: 2px 0;
  font-size: 12px;
}
.telegram-item[data-success='false'] {
  color: var(--n-text-color-2);
}
.telegram-item__status {
  font-weight: 700;
  min-width: 12px;
  text-align: center;
}
.telegram-item[data-success='true'] .telegram-item__status {
  color: var(--n-color-success, #18a058);
}
.telegram-item[data-success='false'] .telegram-item__status {
  color: var(--n-color-warning, #f0a020);
}
.telegram-item__label {
  font-weight: 500;
  color: var(--n-text-color);
}
.telegram-item__id {
  font-size: 11px;
}
.telegram-item__ts {
  margin-left: auto;
  font-size: 11px;
}
.telegram-item__attempts {
  font-size: 11px;
}
.telegram-item__error {
  flex-basis: 100%;
  color: var(--n-color-warning, #f0a020);
  font-size: 11px;
  padding-left: 18px;
  word-break: break-word;
}
.muted {
  color: var(--n-text-color-3, #aaa);
}

.panel-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin: 0 0 6px;
}

.section-title {
  font-size: 11px;
  font-weight: 600;
  color: var(--n-text-color-3);
  text-transform: uppercase;
  letter-spacing: 0.45px;
}

/*
 * Log viewer — dark terminal-style. Color-coded severity qua data-kind
 * (info/warning/error/success) từ heuristic keyword.
 */
.log-viewer {
  max-height: 300px;
  min-height: 120px;
  overflow-y: auto;
  overflow-x: auto;
  font-family: var(--n-font-family-mono);
  font-size: 11.5px;
  background: #0a0f16;
  color: #c9d1d9;
  border: 1px solid #1c2739;
  border-radius: 6px;
  padding: 8px 10px;
  line-height: 1.6;
}

.log-entry {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 1px 0;
  white-space: pre-wrap;
  word-break: break-word;
}

.log-ts {
  color: #6e7681;
  flex-shrink: 0;
  font-variant-numeric: tabular-nums;
}

.log-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #4b5563;
  flex-shrink: 0;
  transform: translateY(-1px);
}

.log-message {
  color: #c9d1d9;
  min-width: 0;
  flex: 1 1 auto;
}

.log-entry[data-kind='info'] .log-dot { background: #60a5fa; }
.log-entry[data-kind='warning'] .log-dot { background: #fbbf24; }
.log-entry[data-kind='warning'] .log-message { color: #fde68a; }
.log-entry[data-kind='error'] .log-dot { background: #f87171; }
.log-entry[data-kind='error'] .log-message { color: #fca5a5; }
.log-entry[data-kind='success'] .log-dot { background: #4ade80; }
.log-entry[data-kind='success'] .log-message { color: #86efac; }

.log-empty {
  color: #6b7280;
  font-style: italic;
  text-align: center;
  padding: 20px 0;
}

/*
 * QR container. R12.6 — ảnh QR container tối thiểu 240×240 trên mobile để
 * quét được bằng camera điện thoại.
 */
.qr-container {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 280px;
  background: #f6f8fa;
  border: 1px solid var(--n-border-color, #d0d7de);
  border-radius: 6px;
  padding: 12px;
  position: relative;
  overflow: hidden;
}

.qr-container::before {
  /* Corner accent — 4 dấu chấm góc tinh tế */
  content: '';
  position: absolute;
  inset: 6px;
  border-radius: 4px;
  background: radial-gradient(circle at 0% 0%, #22c55e 3px, transparent 4px),
              radial-gradient(circle at 100% 0%, #22c55e 3px, transparent 4px),
              radial-gradient(circle at 0% 100%, #22c55e 3px, transparent 4px),
              radial-gradient(circle at 100% 100%, #22c55e 3px, transparent 4px);
  pointer-events: none;
  opacity: 0.6;
}

.qr-image {
  min-width: 240px;
  min-height: 240px;
  max-width: 320px;
  max-height: 320px;
  width: auto;
  height: auto;
  display: block;
  image-rendering: pixelated;
  background: #ffffff;
  border-radius: 4px;
  position: relative;
  z-index: 1;
}

.qr-placeholder,
.qr-error {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  color: #57606a;
  font-size: 12px;
  text-align: center;
  padding: 20px;
  position: relative;
  z-index: 1;
}

.qr-placeholder__icon {
  color: #b1b8c2;
}

.qr-error {
  color: #b91c1c;
}

.qr-placeholder-status {
  color: var(--n-text-color-3);
  font-size: 10.5px;
}

@media (min-width: 1024px) {
  .panel-body {
    flex-direction: row;
    align-items: flex-start;
  }
  .log-section {
    flex: 1 1 auto;
    min-width: 0;
  }
  .qr-section {
    flex: 0 0 auto;
    width: 320px;
  }
  .log-viewer {
    max-height: 360px;
  }
}

.mono {
  font-family: var(--n-font-family-mono);
}
</style>
