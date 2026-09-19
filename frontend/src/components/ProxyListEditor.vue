<!--
  ProxyListEditor — edit danh sách proxy (`proxy.list`).

  Trách nhiệm hẹp:
  - Trình bày `modelValue: string[]` dưới dạng textarea, mỗi dòng 1 proxy
    URL/template (ví dụ `http://user:pass@host:port` hoặc `{SID}` template
    theo `core/proxy_format.materialize_template`).
  - Emit `update:modelValue` khi user chỉnh — v-model 2-chiều với parent.
  - Nút "Áp dụng" gọi thẳng `useSettingsStore().updateKey('proxy.list', …)`
    để lưu ngay proxy list mà không cần "Lưu tất cả" ở SettingsAccordion.

  KHÔNG validate format ở FE — backend là source of truth qua
  `SettingsRepository.set('proxy.list', …)`.
-->
<script setup lang="ts">
import { computed, ref } from 'vue'
import { NButton, NInput, NText } from 'naive-ui'

import { useSettingsStore } from '../composables/useSettingsStore'

const props = defineProps<{
  modelValue: string[]
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: string[]): void
}>()

const settingsStore = useSettingsStore()

const displayText = computed<string>(() => props.modelValue.join('\n'))

const applying = ref<boolean>(false)

function handleInput(v: string): void {
  const lines = v
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l.length > 0)
  emit('update:modelValue', lines)
}

async function handleApply(): Promise<void> {
  if (applying.value) return
  applying.value = true
  try {
    await settingsStore.updateKey('proxy.list', props.modelValue)
  } catch {
    /* network — user can retry */
  } finally {
    applying.value = false
  }
}
</script>

<template>
  <div class="proxy-list-editor">
    <label class="proxy-list-editor__label">
      Proxy list (one proxy per line — use
      <code class="mono">{SID}</code> to insert a session id)
    </label>
    <n-input
      :value="displayText"
      type="textarea"
      class="mono"
      :autosize="{ minRows: 5, maxRows: 12 }"
      spellcheck="false"
      placeholder="http://user:pass@host:port
http://user-{SID}:pass@host:port"
      @update:value="handleInput"
    />
    <div class="proxy-list-editor__row">
      <n-text depth="3" style="font-size: 11.5px; font-variant-numeric: tabular-nums">
        {{ modelValue.length }} proxy(s)
      </n-text>
      <n-button
        type="primary"
        secondary
        size="small"
        :loading="applying"
        :disabled="settingsStore.loading"
        @click="handleApply"
      >
        Apply now
      </n-button>
    </div>
  </div>
</template>

<style scoped>
.proxy-list-editor {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
  box-sizing: border-box;
}

.proxy-list-editor__label {
  font-size: 11.5px;
  font-weight: 500;
  color: var(--n-text-color-2);
}

.proxy-list-editor__label code {
  padding: 1px 4px;
  background: var(--n-action-color);
  color: var(--n-text-color);
  border-radius: 3px;
  font-size: 10.5px;
}

.proxy-list-editor__row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.mono {
  font-family: var(--n-font-family-mono);
}

.mono :deep(textarea) {
  font-family: var(--n-font-family-mono);
  font-size: 11.5px;
  line-height: 1.5;
}
</style>
