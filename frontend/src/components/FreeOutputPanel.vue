<!--
  FreeOutputPanel — accounts that are NOT Plus (any status).

  Operator exports raw account_line rows to re-run non-Plus accounts as a
  new batch. Data source is `jobsStore.freeJobs` (plan predicate only —
  no status gate). Rows without raw credentials still render (masked) but
  are excluded from Copy-all / Download, matching SuccessOutputPanel.
-->
<script setup lang="ts">
import { computed } from 'vue'
import { NButton, NEmpty, NIcon, NVirtualList, useMessage } from 'naive-ui'

import DashboardCard from './DashboardCard.vue'
import { useJobsStore } from '../composables/useJobsStore'
import { IconAlertTriangle, IconCopy, IconDownload } from '../icons'

const jobsStore = useJobsStore()
const message = useMessage()

const props = defineProps<{
  selectedJobId?: string | null
}>()

const emit = defineEmits<{
  (e: 'select', jobId: string): void
}>()

interface FreeRow {
  job_id: string
  row_no: number
  credential: string
  is_raw: boolean
  status: string
}

const ROW_ITEM_SIZE = 34

const freeRows = computed<FreeRow[]>(() => {
  const rows: FreeRow[] = []
  for (const job of jobsStore.freeJobs) {
    const raw = job.account_line
    const hasRaw = !!raw && raw.length > 0
    rows.push({
      job_id: job.job_id,
      row_no: rows.length + 1,
      credential: hasRaw ? raw : job.account_masked,
      is_raw: hasRaw,
      status: job.status,
    })
  }
  return rows
})

const rawCount = computed<number>(
  () => freeRows.value.filter((r) => r.is_raw).length,
)

/** Free rows still pending/running — export allowed, banner only. */
const inFlightCount = computed<number>(
  () =>
    freeRows.value.filter(
      (r) => r.status === 'pending' || r.status === 'running',
    ).length,
)

function rawLines(): string[] {
  return freeRows.value.filter((r) => r.is_raw).map((r) => r.credential)
}

async function handleCopyOne(row: FreeRow): Promise<void> {
  if (typeof navigator === 'undefined' || !navigator.clipboard) {
    message.warning('Browser does not support clipboard')
    return
  }
  try {
    await navigator.clipboard.writeText(row.credential)
    message.success(
      row.is_raw ? 'Copied credential' : 'Copied email (masked)',
    )
  } catch (ex) {
    message.error(`Copy failed: ${ex instanceof Error ? ex.message : String(ex)}`)
  }
}

async function handleCopyAll(): Promise<void> {
  if (typeof navigator === 'undefined' || !navigator.clipboard) return
  const lines = rawLines()
  if (lines.length === 0) {
    message.info('No account with raw credential in this session yet')
    return
  }
  try {
    await navigator.clipboard.writeText(lines.join('\n'))
    message.success(`Copied ${lines.length} account(s)`)
  } catch (ex) {
    message.error(`Copy failed: ${ex instanceof Error ? ex.message : String(ex)}`)
  }
}

function handleDownload(): void {
  const lines = rawLines()
  if (lines.length === 0) {
    message.info('No account with raw credential in this session yet')
    return
  }
  try {
    const blob = new Blob([lines.join('\n') + '\n'], {
      type: 'text/plain;charset=utf-8',
    })
    const objectUrl = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = objectUrl
    a.download = `free-accounts-${lines.length}.txt`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
    message.success(`Downloaded ${lines.length} account(s)`)
  } catch (ex) {
    message.error(
      `Download failed: ${ex instanceof Error ? ex.message : String(ex)}`,
    )
  }
}

function handleSelect(jobId: string): void {
  emit('select', jobId)
}

function emailOf(cred: string): string {
  const first = cred.split('|', 1)[0] ?? ''
  return first.trim() || '(unknown)'
}
</script>

<template>
  <section class="free-output" aria-label="Accounts to re-run (Free)">
    <DashboardCard
      :icon="IconAlertTriangle"
      title="Accounts to re-run (Free)"
      :meta="`${freeRows.length} Free${rawCount < freeRows.length ? ` · ${rawCount} with credential` : ''}`"
    >
      <template #extra>
        <div class="extra-actions">
          <n-button
            size="tiny"
            quaternary
            type="warning"
            :disabled="rawCount === 0"
            :title="rawCount === 0 ? 'No raw credential in this session' : `Copy ${rawCount} account(s)`"
            data-testid="free-copy-all"
            @click="handleCopyAll"
          >
            <template #icon>
              <n-icon :component="IconCopy" />
            </template>
            Copy all
          </n-button>
          <n-button
            size="tiny"
            quaternary
            type="warning"
            :disabled="rawCount === 0"
            :title="rawCount === 0 ? 'No raw credential in this session' : `Download ${rawCount} account(s) as .txt`"
            data-testid="free-download"
            @click="handleDownload"
          >
            <template #icon>
              <n-icon :component="IconDownload" />
            </template>
            Download
          </n-button>
        </div>
      </template>

      <div class="wrap">
        <div
          v-if="inFlightCount > 0"
          class="inflight-banner"
          role="status"
          data-testid="free-inflight-banner"
        >
          <n-icon :component="IconAlertTriangle" size="14" class="inflight-banner__icon" />
          <span>
            {{ inFlightCount }} account(s) still running — prefer "Check Plus All"
            before export
          </span>
        </div>

        <NVirtualList
          v-if="freeRows.length > 0"
          class="list"
          :items="freeRows"
          :item-size="ROW_ITEM_SIZE"
          item-resizable
          key-field="job_id"
        >
          <template #default="{ item: row }: { item: FreeRow }">
          <div
            :key="row.job_id"
            class="row"
            :class="{ 'row--active': row.job_id === props.selectedJobId }"
            role="button"
            :tabindex="0"
            :title="`Open details for ${emailOf(row.credential)}`"
            @click="handleSelect(row.job_id)"
            @keydown.enter="handleSelect(row.job_id)"
            @keydown.space.prevent="handleSelect(row.job_id)"
          >
            <span class="row__idx mono">#{{ row.row_no }}</span>
            <span
              class="row__credential mono"
              :class="{ 'row__credential--masked': !row.is_raw }"
              :title="row.credential"
            >{{ row.credential }}</span>
            <n-button
              size="tiny"
              quaternary
              circle
              type="warning"
              title="Copy this row"
              @click.stop="handleCopyOne(row)"
            >
              <template #icon>
                <n-icon :component="IconCopy" />
              </template>
            </n-button>
          </div>
          </template>
        </NVirtualList>
        <div v-else class="empty" data-testid="free-empty">
          <n-empty
            size="small"
            description="No free accounts to re-run"
          />
        </div>
      </div>
    </DashboardCard>
  </section>
</template>

<style scoped>
.free-output {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  box-sizing: border-box;
}

.extra-actions {
  display: inline-flex;
  align-items: center;
  gap: 2px;
}

.wrap {
  flex: 1 1 auto;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.inflight-banner {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin: 8px 12px 4px;
  padding: 8px 10px;
  border-radius: 6px;
  font-size: 11.5px;
  line-height: 1.35;
  color: var(--n-warning-color, #f0a020);
  background: rgba(245, 158, 11, 0.12);
  border: 1px solid rgba(245, 158, 11, 0.35);
}

.inflight-banner__icon {
  flex: 0 0 auto;
  margin-top: 1px;
}

.list {
  flex: 1 1 auto;
  min-height: 0;
}
.list :deep(.v-vl) {
  height: 100%;
}

.row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  border-bottom: 1px solid var(--n-divider-color);
  cursor: pointer;
  transition: background 0.1s ease;
  position: relative;
}

.row:hover {
  background: rgba(245, 158, 11, 0.06);
}

.row:focus-visible {
  outline: 2px solid var(--n-warning-color, #f0a020);
  outline-offset: -2px;
}

.row:last-child {
  border-bottom: none;
}

.row--active {
  background: rgba(245, 158, 11, 0.12) !important;
}
.row--active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 3px;
  background: var(--accent-yellow, #f59e0b);
}

.row__idx {
  flex: 0 0 auto;
  min-width: 24px;
  font-size: 10.5px;
  color: var(--n-text-color-3);
  text-align: right;
}

.row__credential {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 11.5px;
  color: var(--n-text-color);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  user-select: all;
}

.row__credential--masked {
  color: var(--n-text-color-3);
  font-style: italic;
}

.empty {
  padding: 20px 12px;
  display: flex;
  justify-content: center;
}

.mono {
  font-family: var(--n-font-family-mono);
}
</style>
