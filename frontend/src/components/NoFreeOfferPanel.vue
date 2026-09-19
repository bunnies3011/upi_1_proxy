<!-- Permanent rejects. These accounts are excluded from rerun. -->
<script setup lang="ts">
import { computed } from 'vue'
import { NButton, NEmpty, NIcon, NVirtualList, useMessage } from 'naive-ui'

import DashboardCard from './DashboardCard.vue'
import { useJobsStore } from '../composables/useJobsStore'
import { useSettingsStore } from '../composables/useSettingsStore'
import { IconAlertTriangle, IconCopy, IconTrash } from '../icons'

const jobsStore = useJobsStore()
const settingsStore = useSettingsStore()
const message = useMessage()
const SETTING_INPUT_DRAFT = 'ui.input_draft'

const props = defineProps<{
  selectedJobId?: string | null
}>()

const emit = defineEmits<{
  (e: 'select', jobId: string): void
}>()

interface NoFreeOfferRow {
  job_id: string
  row_no: number
  credential: string
  is_raw: boolean
  reason: string
}

const ROW_ITEM_SIZE = 40

function reasonLabel(job: ReturnType<typeof useJobsStore>['noFreeOfferJobs'][number]): string {
  if (job.error_code === 'login_failed') return 'deactivated'
  return 'no free offer'
}

const rows = computed<NoFreeOfferRow[]>(() =>
  jobsStore.noFreeOfferJobs.map((job, idx) => {
    const raw = job.account_line
    const hasRaw = !!raw && raw.length > 0
    return {
      job_id: job.job_id,
      row_no: idx + 1,
      credential: hasRaw ? raw : job.account_masked,
      is_raw: hasRaw,
      reason: reasonLabel(job),
    }
  }),
)

const rawCount = computed<number>(() => rows.value.filter((r) => r.is_raw).length)

const noFreeOfferAccountSet = computed<Set<string>>(() => {
  const set = new Set<string>()
  for (const row of rows.value) {
    if (row.is_raw) {
      set.add(row.credential.trim())
    }
  }
  return set
})

const removableFromInputCount = computed<number>(() => {
  const draft = typeof settingsStore.settings[SETTING_INPUT_DRAFT] === 'string'
    ? (settingsStore.settings[SETTING_INPUT_DRAFT] as string)
    : ''
  let count = 0
  for (const line of draft.split('\n')) {
    const normalized = line.trim()
    if (normalized.length > 0 && noFreeOfferAccountSet.value.has(normalized)) {
      count += 1
    }
  }
  return count
})

function rawLines(): string[] {
  return rows.value.filter((r) => r.is_raw).map((r) => r.credential)
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
    message.success(`Copied ${lines.length} excluded account(s)`)
  } catch (ex) {
    message.error(`Copy failed: ${ex instanceof Error ? ex.message : String(ex)}`)
  }
}

async function handleRemoveNoFreeOfferFromInput(): Promise<void> {
  const draft = typeof settingsStore.settings[SETTING_INPUT_DRAFT] === 'string'
    ? (settingsStore.settings[SETTING_INPUT_DRAFT] as string)
    : ''
  if (removableFromInputCount.value === 0 || draft.length === 0) {
    message.info('No excluded account found in input')
    return
  }

  let removed = 0
  const nextDraft = draft
    .split('\n')
    .filter((line) => {
      const normalized = line.trim()
      if (normalized.length === 0) return true
      if (!noFreeOfferAccountSet.value.has(normalized)) return true
      removed += 1
      return false
    })
    .join('\n')

  const ok = await settingsStore.updateKey(SETTING_INPUT_DRAFT, nextDraft)
  if (!ok) {
    message.error('Failed to remove excluded accounts from input')
    return
  }
  message.success(`Removed ${removed} excluded account(s) from input`)
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
  <section class="no-free-output" aria-label="No free offer accounts">
    <DashboardCard
      :icon="IconAlertTriangle"
      title="No free offer"
      :meta="`${rows.length} excluded`"
    >
      <template #extra>
        <div class="extra-actions">
          <n-button
            size="tiny"
            quaternary
            type="error"
            :disabled="removableFromInputCount === 0"
            :title="
              removableFromInputCount === 0
                ? 'No excluded account is present in input'
                : `Remove ${removableFromInputCount} excluded account(s) from input`
            "
            data-testid="no-free-remove-input"
            @click="handleRemoveNoFreeOfferFromInput"
          >
            <template #icon>
              <n-icon :component="IconTrash" />
            </template>
            {{
              removableFromInputCount === 0
                ? 'Remove'
                : `Remove (${removableFromInputCount})`
            }}
          </n-button>
          <n-button
            size="tiny"
            quaternary
            type="error"
            :disabled="rawCount === 0"
            :title="rawCount === 0 ? 'No raw credential in this session' : `Copy ${rawCount} excluded account(s)`"
            data-testid="no-free-copy-all"
            @click="handleCopyAll"
          >
            <template #icon>
              <n-icon :component="IconCopy" />
            </template>
            Copy all
          </n-button>
        </div>
      </template>

      <div class="wrap">
        <NVirtualList
          v-if="rows.length > 0"
          class="list"
          :items="rows"
          :item-size="ROW_ITEM_SIZE"
          item-resizable
          key-field="job_id"
        >
          <template #default="{ item: row }: { item: NoFreeOfferRow }">
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
              <span class="row__reason" :title="row.reason">{{ row.reason }}</span>
            </div>
          </template>
        </NVirtualList>
        <div v-else class="empty" data-testid="no-free-empty">
          <n-empty size="small" description="No permanent rejects" />
        </div>
      </div>
    </DashboardCard>
  </section>
</template>

<style scoped>
.no-free-output {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  box-sizing: border-box;
}

.wrap {
  flex: 1 1 auto;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.extra-actions {
  display: inline-flex;
  align-items: center;
  gap: 2px;
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
  background: rgba(239, 68, 68, 0.08);
}

.row:focus-visible {
  outline: 2px solid var(--n-error-color, #ef4444);
  outline-offset: -2px;
}

.row:last-child {
  border-bottom: none;
}

.row--active {
  background: rgba(239, 68, 68, 0.12) !important;
}
.row--active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 3px;
  background: var(--n-error-color, #ef4444);
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

.row__reason {
  flex: 0 0 auto;
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 10.5px;
  color: var(--n-error-color, #ef4444);
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
