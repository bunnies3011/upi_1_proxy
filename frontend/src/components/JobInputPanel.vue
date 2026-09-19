<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NIcon, NInput, useMessage } from 'naive-ui'
import DashboardCard from './DashboardCard.vue'
import { useJobsStore } from '../composables/useJobsStore'
import { useSettingsStore } from '../composables/useSettingsStore'
import { IconClipboardList, IconPlay, IconPlus, IconServer, IconTrash } from '../icons'

const MAX_LINES = 5000

const PAYMENT_METHOD_OPTIONS = [
  { label: 'iDEAL', value: 'ideal' },
  { label: 'UPI', value: 'upi' },
  { label: 'UPI no CDK', value: 'upi_nocdk' },
  { label: 'UPI direct', value: 'upi_direct' },
  { label: 'Kakao direct', value: 'kakao_direct' },
  { label: 'GCash direct', value: 'gcash_direct' },
  { label: 'MoMo check', value: 'momo_check' },
] as const

type PaymentMethod = (typeof PAYMENT_METHOD_OPTIONS)[number]['value']

const PAYMENT_METHOD_MAX_CONCURRENT_KEYS: Record<PaymentMethod, string> = {
  ideal: 'ideal.max_concurrent',
  upi: 'upi.max_concurrent',
  upi_nocdk: 'oaipay.max_concurrent',
  upi_direct: 'upi_direct.max_concurrent',
  kakao_direct: 'kakao_direct.max_concurrent',
  gcash_direct: 'gcash_direct.max_concurrent',
  momo_check: 'momo_check.max_concurrent',
}

const SETTING_INPUT_DRAFT = 'ui.input_draft'
const PAYMENT_METHOD_STORAGE_KEY = 'ideal_qr.payment_method'
const AUTOSAVE_DEBOUNCE_MS = 800

const jobsStore = useJobsStore()
const settingsStore = useSettingsStore()
const message = useMessage()

const proxyCount = computed<number>(() => {
  const list = settingsStore.settings['proxy.list']
  return Array.isArray(list) ? list.length : 0
})

const rotationModeLabel = computed<string>(() => {
  const mode = settingsStore.settings['proxy.rotation_mode']
  if (mode === 'round_robin') return 'round-robin'
  if (mode === 'least_used') return 'least-used'
  return 'unknown'
})

const maxConcurrent = computed<number>(() => {
  const key = PAYMENT_METHOD_MAX_CONCURRENT_KEYS[paymentMethod.value]
  const v = settingsStore.settings[key]
  return typeof v === 'number' ? v : 5
})

const rawInput = ref<string>('')
const paymentMethod = ref<PaymentMethod>(readStoredPaymentMethod())

let applyingFromRemote = false
let saveTimer: ReturnType<typeof setTimeout> | null = null

const lines = computed<string[]>(() =>
  rawInput.value
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.length > 0),
)

const lineCount = computed<number>(() => lines.value.length)
const exceedsLimit = computed<boolean>(() => lineCount.value > MAX_LINES)
const disabled = computed<boolean>(
  () => lineCount.value === 0 || exceedsLimit.value || jobsStore.loading,
)

function isPaymentMethod(value: unknown): value is PaymentMethod {
  return PAYMENT_METHOD_OPTIONS.some((option) => option.value === value)
}

function readStoredPaymentMethod(): PaymentMethod {
  if (typeof window === 'undefined') return 'ideal'
  try {
    const stored = window.localStorage.getItem(PAYMENT_METHOD_STORAGE_KEY)
    return isPaymentMethod(stored) ? stored : 'ideal'
  } catch {
    return 'ideal'
  }
}

function setPaymentMethod(value: PaymentMethod): void {
  paymentMethod.value = value
}

async function handleSubmit(start = true): Promise<void> {
  if (disabled.value) return
  try {
    const result = await jobsStore.submitBatch(lines.value, paymentMethod.value, { start })
    const createdCount = result.created.length
    const alreadyExistsCount = result.skipped.filter(
      (item) => item.reason === 'job_already_exists' || item.reason === 'job_already_active',
    ).length
    const invalidCount = result.skipped.length - alreadyExistsCount
    const verb = start ? 'Created' : 'Added'
    const suffix = start ? '' : ' (held - press Start to run)'

    if (createdCount > 0 && alreadyExistsCount > 0) {
      message.success(
        `${verb} ${createdCount} new job(s)${suffix}, skipped ${alreadyExistsCount} email(s) with existing job` +
          (invalidCount > 0 ? `, ${invalidCount} invalid line(s)` : ''),
      )
    } else if (createdCount > 0) {
      message.success(
        `${verb} ${createdCount} new job(s)${suffix}` +
          (invalidCount > 0 ? ` (skipped ${invalidCount} invalid line(s))` : ''),
      )
    } else if (alreadyExistsCount > 0 && invalidCount === 0) {
      message.info(`No new jobs - all ${alreadyExistsCount} email(s) already have a job in the list`)
    } else if (invalidCount > 0 && alreadyExistsCount === 0) {
      message.warning(`${invalidCount} invalid line(s), no job created`)
    } else if (alreadyExistsCount > 0 && invalidCount > 0) {
      message.warning(`No job created (${alreadyExistsCount} already exist, ${invalidCount} invalid)`)
    }
  } catch {
    // Error is already exposed by the store.
  }
}

function handleClear(): void {
  rawInput.value = ''
}

function handleKeydown(event: KeyboardEvent): void {
  if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
    event.preventDefault()
    void handleSubmit(true)
  }
}

function initialDraftFromStore(): string {
  const value = settingsStore.settings[SETTING_INPUT_DRAFT]
  return typeof value === 'string' ? value : ''
}

async function saveDraftNow(value: string): Promise<void> {
  try {
    await settingsStore.updateKey(SETTING_INPUT_DRAFT, value)
  } catch {
    // Global error notifier handles this.
  }
}

watch(rawInput, (value) => {
  if (applyingFromRemote) return
  if (saveTimer !== null) clearTimeout(saveTimer)
  saveTimer = setTimeout(() => {
    void saveDraftNow(value)
  }, AUTOSAVE_DEBOUNCE_MS)
})

watch(paymentMethod, (value) => {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(PAYMENT_METHOD_STORAGE_KEY, value)
  } catch {
    // Ignore unavailable storage; the selected mode still works for this tab.
  }
})

watch(
  () => settingsStore.settings[SETTING_INPUT_DRAFT],
  (newValue) => {
    if (typeof newValue !== 'string') return
    if (newValue === rawInput.value) return
    applyingFromRemote = true
    rawInput.value = newValue
    queueMicrotask(() => {
      applyingFromRemote = false
    })
  },
)

onMounted(() => {
  const initial = initialDraftFromStore()
  if (initial && rawInput.value === '') {
    applyingFromRemote = true
    rawInput.value = initial
    queueMicrotask(() => {
      applyingFromRemote = false
    })
  }
})

onBeforeUnmount(() => {
  if (saveTimer !== null) {
    clearTimeout(saveTimer)
    saveTimer = null
  }
})
</script>

<template>
  <section class="job-input-panel" aria-label="Enter accounts to create jobs">
    <DashboardCard
      title="Enter accounts"
      :icon="IconClipboardList"
      meta="email|password|totp_secret"
    >
      <div class="body">
        <n-input
          v-model:value="rawInput"
          type="textarea"
          :autosize="false"
          class="textarea mono"
          placeholder="user1@icloud.com|password1|TOTPSECRET1
user2@icloud.com|access_token_xyz"
          spellcheck="false"
          @keydown="handleKeydown"
        />

        <div class="status-bar">
          <span class="chip chip--proxy" :title="`${proxyCount} proxy - ${rotationModeLabel}`">
            <n-icon :component="IconServer" size="12" />
            <span class="chip__num">{{ proxyCount }}</span>
            <span class="chip__label">proxy - {{ rotationModeLabel }}</span>
          </span>
          <span class="chip" :title="`Max ${maxConcurrent} concurrent job(s)`">
            <span class="chip__num">{{ maxConcurrent }}</span>
            <span class="chip__label">concurrent</span>
          </span>
          <span class="status-bar__spacer" />
          <span class="counter mono">
            <strong :class="{ 'counter--over': exceedsLimit }">{{ lineCount }}</strong>
            <span class="counter__sep">/</span>
            <span class="counter__max">{{ MAX_LINES }}</span>
          </span>
        </div>

        <div class="actions">
          <div
            class="actions__method"
            role="tablist"
            aria-label="Payment method"
            :title="`Payment rail - sent as payment_method (${paymentMethod})`"
          >
            <n-button
              v-for="option in PAYMENT_METHOD_OPTIONS"
              :key="option.value"
              size="small"
              class="actions__method-button"
              :class="{ 'actions__method-button--active': paymentMethod === option.value }"
              :type="paymentMethod === option.value ? 'primary' : 'default'"
              @click="setPaymentMethod(option.value)"
            >
              {{ option.label }}
            </n-button>
          </div>

          <n-button
            type="primary"
            size="small"
            :loading="jobsStore.loading"
            :disabled="disabled"
            :title="
              exceedsLimit
                ? `Exceeds ${MAX_LINES} lines - reduce before creating`
                : lineCount === 0
                ? 'Paste at least 1 account line'
                : 'Create and start job(s) immediately'
            "
            class="actions__run"
            @click="() => handleSubmit(true)"
          >
            <template #icon>
              <n-icon :component="IconPlay" />
            </template>
            {{ lineCount === 0 ? 'Run' : `Run (${lineCount})` }}
          </n-button>

          <n-button
            size="small"
            :loading="jobsStore.loading"
            :disabled="disabled"
            :title="
              exceedsLimit
                ? `Exceeds ${MAX_LINES} lines`
                : lineCount === 0
                ? 'Paste at least 1 account line'
                : 'Add job(s) as held - press Start later'
            "
            class="actions__add"
            @click="() => handleSubmit(false)"
          >
            <template #icon>
              <n-icon :component="IconPlus" />
            </template>
            {{ lineCount === 0 ? 'Add' : `Add (${lineCount})` }}
          </n-button>

          <n-button
            size="small"
            quaternary
            :disabled="rawInput.length === 0"
            class="actions__clear"
            @click="handleClear"
          >
            <template #icon>
              <n-icon :component="IconTrash" />
            </template>
            Clear input
          </n-button>
        </div>

        <n-alert
          v-if="exceedsLimit"
          type="warning"
          size="small"
          :show-icon="false"
          class="warning"
        >
          Exceeds {{ MAX_LINES }} lines. Reduce before creating jobs.
        </n-alert>
      </div>
    </DashboardCard>
  </section>
</template>

<style scoped>
.job-input-panel {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
  box-sizing: border-box;
}

.body {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 14px 14px;
  min-height: 0;
  flex: 1 1 auto;
  overflow: hidden;
}

.body .textarea {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  overflow: hidden;
  isolation: isolate;
}

.body .textarea :deep(.n-input) {
  height: 100%;
  min-height: 0;
}

.body .textarea :deep(.n-input-wrapper),
.body .textarea :deep(.n-input__textarea),
.body .textarea :deep(.n-input__textarea-el) {
  height: 100%;
  min-height: 0;
  resize: none;
  overflow: auto;
}

.textarea :deep(textarea) {
  font-family: var(--n-font-family-mono);
  font-size: 11.5px;
  line-height: 1.55;
  min-height: 190px;
  height: 100%;
  overflow: auto;
  overscroll-behavior: contain;
}

.status-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  padding: 6px 0 2px;
  border-top: 1px dashed var(--n-divider-color);
}

.status-bar__spacer {
  flex: 1 1 auto;
}

.chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--n-action-color);
  border: 1px solid var(--n-border-color);
  font-size: 10.5px;
  color: var(--n-text-color-3);
  white-space: nowrap;
}

.chip__num {
  font-weight: 700;
  font-size: 11.5px;
  color: var(--n-text-color);
  font-variant-numeric: tabular-nums;
}

.chip__label {
  color: var(--n-text-color-3);
}

.chip--proxy {
  border-color: rgba(59, 130, 246, 0.35);
  background: rgba(59, 130, 246, 0.08);
}

.chip--proxy .chip__num,
.chip--proxy :deep(.n-icon) {
  color: #60a5fa;
}

.counter {
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
  color: var(--n-text-color-3);
  display: inline-flex;
  align-items: baseline;
  gap: 3px;
}

.counter strong {
  color: var(--n-text-color);
  font-weight: 700;
  font-size: 12.5px;
}

.counter__sep {
  color: var(--n-text-color-3);
  opacity: 0.6;
}

.counter__max {
  color: var(--n-text-color-3);
}

.counter--over {
  color: var(--n-error-color) !important;
}

.actions {
  display: grid;
  grid-template-columns: minmax(0, 1.45fr) minmax(0, 1fr) minmax(0, 1fr) auto;
  gap: 8px;
  align-items: center;
  min-width: 0;
}

.actions__method {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.actions__method-button {
  min-width: 0;
  flex: 0 1 auto;
}

.actions__method-button--active {
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.08);
}

.actions__run,
.actions__add {
  min-width: 0;
}

.actions__add {
  background: var(--n-action-color);
  border-color: var(--n-border-color);
}

.warning {
  margin-top: 2px;
}

.mono {
  font-family: var(--n-font-family-mono);
}

@media (max-width: 767px) {
  .body {
    padding: 10px 12px 12px;
    gap: 8px;
  }

  .status-bar {
    gap: 6px;
    row-gap: 6px;
  }

  .actions {
    gap: 6px;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    grid-template-areas:
      "method clear"
      "run run"
      "add add";
  }

  .actions__method {
    grid-area: method;
    width: 100%;
  }

  .actions__method-button {
    flex: 1 1 calc(50% - 3px);
  }

  .actions__run {
    grid-area: run;
  }

  .actions__add {
    grid-area: add;
  }

  .actions__clear {
    grid-area: clear;
  }

  .textarea :deep(textarea) {
    min-height: 220px;
  }
}
</style>
