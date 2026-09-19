<!--
  SuccessOutputPanel — account THẬT SỰ thành công (đã lên ChatGPT PLUS).

  UX (yêu cầu 2026-07 refined):
    "Chỉ tài khoản Plus mới được coi là thành công". Job `qr_ready` chưa
    verify plan → KHÔNG hiển thị ở đây (dù đã tạo QR + thanh toán xong).
    User phải bấm nút "Check Plus" từng row hoặc "Check Plus All" trên
    JobList để verify entitlement live; kết quả `plan === 'plus'` sẽ đưa
    account vào panel này ngay lập tức (nguồn dữ liệu reactive).

  Persistence (fix 2026-07):
    `plan` giờ được persist vào SQLite (`jobs.plan` column) → panel này
    TỒN TẠI qua reload trang. Chỉ bị clear khi user "Clear all" (xóa
    row khỏi DB) hoặc rerun job (BE reset plan về NULL để state machine
    verify lại). Trước fix này, `planStates` là in-memory only → reload
    là rỗng.

  Nguồn dữ liệu:
  - `jobsStore.plusJobs` — computed lọc `qr_ready` AND
    (`job.plan === 'plus'` từ DB HOẶC `planStates.plan === 'plus'`
    transient in-flight). Tập trung 1 chỗ, không duplicate filter logic.
  - `job.account_line` — raw credential từ backend (persist trong SQLite
    qua `GET /api/jobs`). TỒN TẠI qua reload trang.

  Actions:
  - Copy 1 dòng: copy đúng account_line thô.
  - Copy all: nối multiline các account_line đã có (bỏ qua job không
    có dòng thô).
-->
<script setup lang="ts">
import { computed } from 'vue'
import { NButton, NEmpty, NIcon, NVirtualList, useMessage } from 'naive-ui'

import DashboardCard from './DashboardCard.vue'
import { useJobsStore } from '../composables/useJobsStore'
import { useSettingsStore } from '../composables/useSettingsStore'
import { IconCircleCheck, IconCopy, IconTrash } from '../icons'

const jobsStore = useJobsStore()
const settingsStore = useSettingsStore()
const message = useMessage()
const SETTING_INPUT_DRAFT = 'ui.input_draft'

const props = defineProps<{
  /**
   * Job đang được chọn (highlight) từ App state. Panel render class
   * `.row--active` khi khớp để user biết đang xem log/QR của account nào.
   */
  selectedJobId?: string | null
}>()

const emit = defineEmits<{
  (e: 'select', jobId: string): void
}>()

interface SuccessRow {
  job_id: string
  row_no: number
  credential: string
  is_raw: boolean
}

const ROW_ITEM_SIZE = 34

const successRows = computed<SuccessRow[]>(() => {
  const rows: SuccessRow[] = []
  // `plusJobs` do store cung cấp — chỉ chứa job đã VERIFY PLUS. Không
  // filter thêm ở đây để tránh 2 chỗ định nghĩa "thành công" khác nhau.
  for (const job of jobsStore.plusJobs) {
    // Backend expose `account_line` raw qua GET /api/jobs (đã persist DB
    // qua restart). Nếu vì lý do nào đó chưa có (VD SSE tạo entry trước
    // loadAll), fallback về `account_masked` để user vẫn thấy 1 gì đó.
    const raw = job.account_line
    rows.push({
      job_id: job.job_id,
      row_no: rows.length + 1,
      credential: raw && raw.length > 0 ? raw : job.account_masked,
      is_raw: !!raw && raw.length > 0,
    })
  }
  return rows
})

const rawCount = computed<number>(
  () => successRows.value.filter((r) => r.is_raw).length,
)

const plusAccountSet = computed<Set<string>>(() => {
  const set = new Set<string>()
  for (const row of successRows.value) {
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
    if (normalized.length > 0 && plusAccountSet.value.has(normalized)) {
      count += 1
    }
  }
  return count
})

async function handleCopyOne(row: SuccessRow): Promise<void> {
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
  const lines = successRows.value
    .filter((r) => r.is_raw)
    .map((r) => r.credential)
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

async function handleRemovePlusFromInput(): Promise<void> {
  const draft = typeof settingsStore.settings[SETTING_INPUT_DRAFT] === 'string'
    ? (settingsStore.settings[SETTING_INPUT_DRAFT] as string)
    : ''
  if (removableFromInputCount.value === 0 || draft.length === 0) {
    message.info('No Plus account found in input')
    return
  }

  let removed = 0
  const nextDraft = draft
    .split('\n')
    .filter((line) => {
      const normalized = line.trim()
      if (normalized.length === 0) return true
      if (!plusAccountSet.value.has(normalized)) return true
      removed += 1
      return false
    })
    .join('\n')

  const ok = await settingsStore.updateKey(SETTING_INPUT_DRAFT, nextDraft)
  if (!ok) {
    message.error('Failed to remove Plus accounts from input')
    return
  }
  message.success(`Removed ${removed} Plus account(s) from input`)
}

function handleSelect(jobId: string): void {
  emit('select', jobId)
}

// Extract email prefix để hiển thị index-style tooltip (không mask lại).
function emailOf(cred: string): string {
  const first = cred.split('|', 1)[0] ?? ''
  return first.trim() || '(unknown)'
}
</script>

<template>
  <section class="success-output" aria-label="Successful accounts (Plus)">
    <DashboardCard
      :icon="IconCircleCheck"
      title="Successful accounts"
      :meta="`${successRows.length} Plus${rawCount < successRows.length ? ` · ${rawCount} with credential` : ''}`"
    >
      <template #extra>
        <n-button
          size="tiny"
          quaternary
          :disabled="removableFromInputCount === 0"
          class="success-output__remove-plus"
          :title="
            removableFromInputCount === 0
              ? 'No Plus account from Successful accounts is present in input'
              : `Remove ${removableFromInputCount} Plus account(s) from input`
          "
          @click="handleRemovePlusFromInput"
        >
          <template #icon>
            <n-icon :component="IconTrash" />
          </template>
          {{ removableFromInputCount === 0 ? 'Remove Plus' : `Remove Plus (${removableFromInputCount})` }}
        </n-button>
        <n-button
          size="tiny"
          quaternary
          type="success"
          :disabled="rawCount === 0"
          :title="rawCount === 0 ? 'No raw credential in this session' : `Copy ${rawCount} account(s)`"
          @click="handleCopyAll"
        >
          <template #icon>
            <n-icon :component="IconCopy" />
          </template>
          Copy all
        </n-button>
      </template>

      <div class="wrap">
        <NVirtualList
          v-if="successRows.length > 0"
          class="list"
          :items="successRows"
          :item-size="ROW_ITEM_SIZE"
          item-resizable
          key-field="job_id"
        >
          <template #default="{ item: row }: { item: SuccessRow }">
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
              type="success"
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
        <div v-else class="empty">
          <n-empty
            size="small"
            description="No Plus accounts yet — click 'Check Plus All' to verify qr_ready accounts"
          />
        </div>
      </div>
    </DashboardCard>
  </section>
</template>

<style scoped>
.success-output {
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
}

.list {
  height: 100%;
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
  background: rgba(34, 197, 94, 0.06);
}

.row:focus-visible {
  outline: 2px solid var(--n-primary-color, #22c55e);
  outline-offset: -2px;
}

.row:last-child {
  border-bottom: none;
}

/*
 * Active row: có border trái xanh 3px, background nhấn nhẹ. Dùng chung
 * pattern với JobList row--active để user nhận diện job đang xem log.
 */
.row--active {
  background: rgba(34, 197, 94, 0.12) !important;
}
.row--active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 3px;
  background: var(--accent-green, #22c55e);
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
