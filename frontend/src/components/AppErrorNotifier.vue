<!--
  AppErrorNotifier — component headless (không render DOM) chuyển đổi
  `jobsStore.error` / `settingsStore.error` thành toast của naive-ui thay vì
  banner cố định trong layout.

  Lý do tồn tại (fix UX):
  Banner cũ đặt trong `App.vue` giữa action-row và main-grid, v-if theo
  `errorMessages.length > 0`. Khi SSE/API fail lặp lại (VD auth token sai
  → 401 mỗi request), banner bật/tắt liên tục → main-grid tăng/giảm chiều
  cao → tất cả panel bị "jitter" đè liên tục. Toast của naive-ui trôi
  overlay top-right, KHÔNG chiếm chỗ layout.

  Behavior:
  - Watch immediate `jobsStore.error`: khi từ null → có message → `message.error(...)`
    rồi `clearError()` để tránh replay khi component re-render vì lý do khác.
  - Tương tự với `settingsStore.error` (dạng `{key, reason}` → format lại).
  - Không toast khi error === null (chỉ trigger transition rising edge).
-->
<script setup lang="ts">
import { watch } from 'vue'
import { useMessage } from 'naive-ui'

import { useJobsStore } from '../composables/useJobsStore'
import { useSettingsStore } from '../composables/useSettingsStore'

const message = useMessage()
const jobsStore = useJobsStore()
const settingsStore = useSettingsStore()

// Jobs API error — plain string, VD "settings_validation_error: ...".
watch(
  () => jobsStore.error,
  (val) => {
    if (val && val.length > 0) {
      message.error(val, { duration: 4000, keepAliveOnHover: true })
      // Consume: reset về null để lần fail tiếp theo (dù message giống) vẫn
      // trigger rising edge → toast lại. Nếu không consume, watch chỉ fire
      // khi content thực sự đổi → user không thấy retry lần 2.
      jobsStore.clearError()
    }
  },
  { immediate: true },
)

// Settings API error — object {key, reason}, format thành 1 dòng.
watch(
  () => settingsStore.error,
  (val) => {
    if (val) {
      message.error(`Settings [${val.key}]: ${val.reason}`, {
        duration: 5000,
        keepAliveOnHover: true,
      })
      settingsStore.clearError()
    }
  },
  { immediate: true },
)
</script>

<template>
  <!-- Headless — không render gì. -->
</template>
