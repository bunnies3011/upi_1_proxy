<!--
  LogPanel — log viewer độc lập, hiển thị log của job được chọn từ JobList.

  Grid area `log` trong layout 2×3 (đặt dưới INPUT, kế bên JOBS).

  Auto-scroll cuối khi có log mới + toggle auto-scroll. Empty state khi
  chưa chọn job.

  Virtualized rendering (fix memory/CPU leak — Chrome "Aw Snap" OOM khi
  job log dài): dùng `NVirtualList` (naive-ui, re-export `vueuc`) để chỉ
  render DOM node của các dòng log ĐANG NẰM TRONG VIEWPORT + buffer, thay
  vì render toàn bộ mảng `logs` (tối đa 500 dòng/job sau fix cap ở
  `useJobsStore.applyLogEvent`, nhưng vẫn có thể là 500 x N job đã mở qua
  → virtualize để chi phí DOM luôn O(viewport) bất kể log dài bao nhiêu).

  Root class BẮT BUỘC giữ: `.log-panel`.
-->
<script setup lang="ts">
import { computed, nextTick, ref, watch, type Ref } from 'vue'
import { NButton, NIcon, NSwitch, NText, NVirtualList, useMessage } from 'naive-ui'
import type { VirtualListInst } from 'naive-ui'

import { useJobsStore, type JobLogEntry } from '../composables/useJobsStore'
import { IconCopy } from '../icons'

/** Chiều cao cố định 1 dòng log (px) — khớp `.log-entry` CSS (line-height
 * 1.6 * font-size 11.5px + padding 1px trên/dưới ≈ 22px). Virtual list
 * yêu cầu itemSize cố định để tính toán viewport mà không cần đo DOM. */
const LOG_ITEM_SIZE = 22

const props = defineProps<{
  jobId: string | null
}>()

const jobsStore = useJobsStore()
const message = useMessage()

const virtualListRef: Ref<VirtualListInst | null> = ref(null)
const autoScroll = ref(true)

const job = computed(() =>
  props.jobId ? jobsStore.jobs.get(props.jobId) : undefined,
)
const logs = computed<JobLogEntry[]>(() => job.value?.logs ?? [])

/**
 * `NVirtualList` cần `items` là array object có field `keyField` duy nhất
 * (mặc định `key`). Log entry gốc không có id ổn định (chỉ `ts` — có thể
 * trùng nếu 2 log cùng giây) → derive key từ index kết hợp ts, đủ ổn định
 * cho phiên hiển thị hiện tại (không cần persist qua re-mount).
 */
interface VirtualLogItem {
  key: string
  entry: JobLogEntry
}

const virtualItems = computed<VirtualLogItem[]>(() =>
  logs.value.map((entry, idx) => ({ key: `${entry.ts}-${idx}`, entry })),
)

function scrollToBottom(): void {
  const inst = virtualListRef.value
  if (inst === null || virtualItems.value.length === 0) return
  inst.scrollTo({ index: virtualItems.value.length - 1 })
}

// Auto scroll to bottom when new log arrives.
watch(
  () => logs.value.length,
  async () => {
    if (!autoScroll.value) return
    await nextTick()
    scrollToBottom()
  },
)

// Load detail khi jobId đổi để có logs đầy đủ (JobList row click).
watch(
  () => props.jobId,
  async (newId) => {
    if (newId) {
      try {
        await jobsStore.loadDetail(newId)
      } catch { /* store.error đã set */ }
      if (autoScroll.value) {
        await nextTick()
        scrollToBottom()
      }
    }
  },
)

function formatTs(tsSeconds: number): string {
  const d = new Date(tsSeconds * 1000)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  return `${hh}:${mm}:${ss}`
}

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

async function handleCopyAll(): Promise<void> {
  if (logs.value.length === 0) return
  const text = logs.value
    .map((e) => `${formatTs(e.ts)} ${e.message}`)
    .join('\n')
  try {
    await navigator.clipboard.writeText(text)
    message.success(`Copied ${logs.value.length} log line(s)`)
  } catch (ex) {
    message.error(`Copy failed: ${ex instanceof Error ? ex.message : String(ex)}`)
  }
}
</script>

<template>
  <section class="log-panel">
    <header class="panel-head">
      <div class="head-left">
        <span class="panel-title">LOG</span>
        <span v-if="job" class="job-id-tag">{{ job.job_id.slice(0, 8) }}</span>
      </div>
      <div class="head-right">
        <NSwitch v-model:value="autoScroll" size="small">
          <template #checked>auto</template>
          <template #unchecked>auto</template>
        </NSwitch>
        <NButton
          size="tiny"
          quaternary
          :disabled="logs.length === 0"
          @click="handleCopyAll"
        >
          <template #icon>
            <NIcon :component="IconCopy" />
          </template>
        </NButton>
      </div>
    </header>

    <div v-if="!props.jobId" class="log-empty log-empty--nojob">
      <NText depth="3">No job selected</NText>
      <NText depth="3" style="font-size: 11px">
        Click a row in the Jobs table to inspect its log.
      </NText>
    </div>
    <div v-else-if="logs.length === 0" class="log-empty">
      <NText depth="3">No logs yet.</NText>
    </div>
    <div v-else class="log-viewer" role="log" aria-live="polite">
      <NVirtualList
        ref="virtualListRef"
        :items="virtualItems"
        :item-size="LOG_ITEM_SIZE"
        key-field="key"
      >
        <template #default="{ item }: { item: VirtualLogItem }">
          <div
            class="log-entry"
            :style="{ height: `${LOG_ITEM_SIZE}px` }"
            :data-kind="logKind(item.entry)"
          >
            <span class="log-ts">{{ formatTs(item.entry.ts) }}</span>
            <span class="log-dot" :aria-hidden="true" />
            <span class="log-message">{{ item.entry.message }}</span>
          </div>
        </template>
      </NVirtualList>
    </div>
  </section>
</template>

<style scoped>
.log-panel {
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--surface-1);
  display: flex;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
  overflow: hidden;
  transition: background var(--transition-base), border-color var(--transition-base);
}

.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 9px 14px;
  border-bottom: 1px solid var(--divider);
  flex-shrink: 0;
  background: var(--surface-1);
}

.head-left,
.head-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.panel-title {
  font-size: 11px;
  font-weight: 700;
  color: var(--text-3);
  text-transform: uppercase;
  letter-spacing: 0.6px;
}

.job-id-tag {
  padding: 2px 8px;
  border-radius: 4px;
  background: var(--surface-2);
  color: var(--text-2);
  font-size: 10.5px;
  font-family: var(--n-font-family-mono);
  border: 1px solid var(--border-color);
}

.log-viewer {
  flex: 1 1 auto;
  overflow: hidden;
  font-family: var(--n-font-family-mono);
  font-size: 11.5px;
  background: var(--log-bg);
  color: var(--log-text);
  padding: 8px 12px;
  line-height: 1.6;
  min-height: 0;
  transition: background var(--transition-base), color var(--transition-base);
}

/* NVirtualList render root cần full height của `.log-viewer` để tính
 * viewport đúng — `:deep()` vì NVirtualList không nhận class trực tiếp. */
.log-viewer :deep(.v-vl) {
  height: 100%;
}

.log-empty {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 24px;
  color: var(--text-3);
  min-height: 120px;
  text-align: center;
}

.log-empty--nojob {
  background: var(--surface-1);
}

.log-entry {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 1px 0;
  white-space: pre-wrap;
  word-break: break-word;
  overflow: hidden;
}

.log-ts {
  color: var(--log-timestamp);
  flex-shrink: 0;
  font-variant-numeric: tabular-nums;
}

.log-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--text-3);
  flex-shrink: 0;
  transform: translateY(-1px);
}

.log-message {
  color: var(--log-text);
  min-width: 0;
  flex: 1 1 auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.log-entry[data-kind='info']    .log-dot     { background: var(--accent-blue); }
.log-entry[data-kind='warning'] .log-dot     { background: var(--accent-yellow); }
.log-entry[data-kind='warning'] .log-message { color: var(--accent-yellow); }
.log-entry[data-kind='error']   .log-dot     { background: var(--accent-red); }
.log-entry[data-kind='error']   .log-message { color: var(--accent-red); }
.log-entry[data-kind='success'] .log-dot     { background: var(--accent-green); }
.log-entry[data-kind='success'] .log-message { color: var(--accent-green); }
</style>
