<script setup lang="ts">
import { computed, watch } from 'vue'
import { NButton, NIcon, useDialog, useMessage } from 'naive-ui'

import { useJobsStore } from '../composables/useJobsStore'
import { IconPlay, IconRepeat, IconSquare, IconTrash, IconX } from '../icons'

const jobsStore = useJobsStore()
const message = useMessage()
const dialog = useDialog()

const stats = computed(() => jobsStore.jobStats)
const activeCount = computed(() => stats.value.pending + stats.value.running)
const failedCount = computed(() => stats.value.error + stats.value.stopped)
const heldCount = computed(() => stats.value.held)

watch(
  () => stats.value.running,
  async (runningNow, runningBefore) => {
    if (!jobsStore.autoRetryFailedEnabled) return
    if ((runningBefore ?? 0) <= 0 || runningNow !== 0) return

    try {
      const retried = await jobsStore.maybeAutoRetryFailed()
      if (retried) {
        message.success('Auto retried failed jobs')
      }
    } catch (ex) {
      message.error(
        `Auto retry failed operation failed: ${ex instanceof Error ? ex.message : String(ex)}`,
      )
    }
  },
)

function handleStopAll(): void {
  if (activeCount.value === 0) {
    message.info('No running jobs to stop')
    return
  }
  dialog.warning({
    title: 'Stop all running jobs?',
    content: `This will stop ${activeCount.value} job(s) (${stats.value.pending} pending + ${stats.value.running} running). Current progress of running jobs will be lost.`,
    positiveText: 'Stop all',
    negativeText: 'Cancel',
    onPositiveClick: async () => {
      try {
        const count = await jobsStore.stopAllJobs()
        message.success(`Stopped ${count} job(s)`)
      } catch (ex) {
        message.error(`Stop all failed: ${ex instanceof Error ? ex.message : String(ex)}`)
      }
    },
  })
}

async function handleRetryFailed(): Promise<void> {
  if (failedCount.value === 0) {
    message.info('No failed jobs to retry')
    return
  }
  try {
    const result = await jobsStore.rerunFailedJobs()
    const skippedSuffix =
      result.skipped.length > 0 ? `, skipped ${result.skipped.length}` : ''
    message.success(`Recreated ${result.created.length} job(s)${skippedSuffix}`)
  } catch (ex) {
    message.error(
      `Retry failed operation failed: ${ex instanceof Error ? ex.message : String(ex)}`,
    )
  }
}

function handleClearFailed(): void {
  if (failedCount.value === 0) {
    message.info('No failed jobs to clear')
    return
  }
  dialog.warning({
    title: 'Clear all failed jobs?',
    content: `This will remove ${failedCount.value} job(s) from the list (${stats.value.error} error + ${stats.value.stopped} stopped). This cannot be undone.`,
    positiveText: 'Clear',
    negativeText: 'Cancel',
    onPositiveClick: async () => {
      try {
        const count = await jobsStore.clearJobs('failed')
        message.success(`Cleared ${count} failed job(s)`)
      } catch (ex) {
        message.error(
          `Clear failed operation failed: ${ex instanceof Error ? ex.message : String(ex)}`,
        )
      }
    },
  })
}

async function handleStartAllHeld(): Promise<void> {
  if (heldCount.value === 0) {
    message.info('No held jobs to start')
    return
  }
  try {
    const count = await jobsStore.startAllHeld()
    if (count === 0) {
      message.info('No held jobs to start')
    } else {
      message.success(`Started ${count} held job(s)`)
    }
  } catch (ex) {
    message.error(`Start all failed: ${ex instanceof Error ? ex.message : String(ex)}`)
  }
}

function handleClearAll(): void {
  if (stats.value.total === 0) {
    message.info('No jobs to clear')
    return
  }
  dialog.warning({
    title: 'Clear all jobs?',
    content: `This will remove all ${stats.value.total} job(s), including ${activeCount.value} currently running. This action cannot be undone.`,
    positiveText: 'Clear all',
    negativeText: 'Cancel',
    onPositiveClick: async () => {
      try {
        const count = await jobsStore.clearJobs('all')
        message.success(`Cleared ${count} job(s)`)
      } catch (ex) {
        message.error(`Clear all failed: ${ex instanceof Error ? ex.message : String(ex)}`)
      }
    },
  })
}
</script>

<template>
  <div class="bulk-actions">
    <n-button
      v-if="heldCount > 0"
      size="small"
      type="primary"
      :title="`Start ${heldCount} held job(s)`"
      class="bulk-actions__start-held"
      @click="handleStartAllHeld"
    >
      <template #icon>
        <n-icon :component="IconPlay" />
      </template>
      Start {{ heldCount }} held
    </n-button>

    <n-button
      size="small"
      quaternary
      :type="jobsStore.autoRetryFailedEnabled ? 'primary' : 'default'"
      :title="
        jobsStore.autoRetryFailedEnabled
          ? 'Auto retry failed jobs when running jobs finish'
          : 'Enable auto retry failed when running jobs finish'
      "
      @click="jobsStore.setAutoRetryFailedEnabled(!jobsStore.autoRetryFailedEnabled)"
    >
      <template #icon>
        <n-icon :component="IconRepeat" />
      </template>
      Auto retry failed
    </n-button>

    <n-button
      size="small"
      quaternary
      type="warning"
      :disabled="activeCount === 0"
      :title="`Stop ${activeCount} running job(s)`"
      @click="handleStopAll"
    >
      <template #icon>
        <n-icon :component="IconSquare" />
      </template>
      Stop all
    </n-button>

    <n-button
      size="small"
      quaternary
      type="info"
      :disabled="failedCount === 0"
      :title="`Retry ${failedCount} failed job(s)`"
      @click="handleRetryFailed"
    >
      <template #icon>
        <n-icon :component="IconRepeat" />
      </template>
      Retry failed
    </n-button>

    <n-button
      size="small"
      quaternary
      :disabled="failedCount === 0"
      :title="`Clear ${failedCount} failed job(s)`"
      @click="handleClearFailed"
    >
      <template #icon>
        <n-icon :component="IconTrash" />
      </template>
      Clear failed
    </n-button>

    <n-button
      size="small"
      quaternary
      type="error"
      :disabled="stats.total === 0"
      title="Clear all jobs"
      @click="handleClearAll"
    >
      <template #icon>
        <n-icon :component="IconX" />
      </template>
      Clear all
    </n-button>
  </div>
</template>

<style scoped>
.bulk-actions {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding-right: 4px;
  border-right: 1px solid var(--border-color);
  margin-right: 4px;
}

@media (max-width: 767px) {
  .bulk-actions {
    display: flex;
    flex-wrap: wrap;
    padding-right: 0;
    margin-right: 0;
    border-right: none;
    gap: 6px;
    width: 100%;
  }
}
</style>
