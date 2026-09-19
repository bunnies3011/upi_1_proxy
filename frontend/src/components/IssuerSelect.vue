<!--
  IssuerSelect — dropdown chọn `ideal.default_issuer` cho settings.

  Requirements:
  - 6.8: hiển thị tên bank thân thiện (ASN Bank, Rabobank, ABN AMRO…) khớp
    mã BIC trong whitelist `ideal.known_issuers`. Bảng ánh xạ TĨNH ở FE
    (không lưu Settings_Store). Emit LUÔN là mã kỹ thuật.
  - 6.9: mã trong whitelist chưa có ánh xạ → fallback về nguyên mã (không
    rỗng, không raise).

  BẮT BUỘC GIỮ EXPORTS (test property):
    - ISSUER_DISPLAY_NAME_MAP (Record<string, string>)
    - resolveIssuerDisplayName(id: string): string
-->
<script lang="ts">
/**
 * Bảng ánh xạ TĨNH mã kỹ thuật IssuerBank (`id`) → tên hiển thị thân thiện.
 * Nguồn: whitelist tối thiểu 11 mã khởi tạo tại Requirement 6.4.
 */
export const ISSUER_DISPLAY_NAME_MAP: Record<string, string> = {
  ASNBNL21: 'ASN Bank',
  RABONL2U: 'Rabobank',
  ABNANL2A: 'ABN AMRO',
  KNABNL2H: 'Knab',
  BITSNL2A: 'Bunq (Bits)',
  RBRBNL21: 'RegioBank',
  BUNQNL2A: 'bunq',
  TRIONL2U: 'Triodos Bank',
  FVLBNL22: 'Van Lanschot',
  NTSBDEB1: 'N26',
  REVOLT21: 'Revolut',
}

/**
 * Resolve tên hiển thị cho 1 mã kỹ thuật IssuerBank.
 * Fallback về chính `id` khi không có trong `ISSUER_DISPLAY_NAME_MAP`
 * (R6.9). KHÔNG raise, KHÔNG trả rỗng.
 */
export function resolveIssuerDisplayName(id: string): string {
  const mapped = ISSUER_DISPLAY_NAME_MAP[id]
  if (mapped !== undefined && mapped !== '') return mapped
  return id
}
</script>

<script setup lang="ts">
import { computed } from 'vue'
import { NIcon, NSelect } from 'naive-ui'
import type { SelectOption } from 'naive-ui'

import { IconBank } from '../icons'

const props = defineProps<{
  /** Mã kỹ thuật IssuerBank hiện được chọn (`ideal.default_issuer`). */
  modelValue: string
  /** Danh sách mã hợp lệ do parent truyền (từ `ideal.known_issuers`). */
  knownIssuers: string[]
}>()

const emit = defineEmits<{
  'update:modelValue': [value: string]
}>()

const options = computed<SelectOption[]>(() =>
  props.knownIssuers.map((id) => ({
    label: resolveIssuerDisplayName(id),
    value: id,
  })),
)

function handleUpdate(value: string | null): void {
  emit('update:modelValue', value ?? '')
}
</script>

<template>
  <n-select
    class="issuer-select"
    :value="modelValue || null"
    :options="options"
    placeholder="-- Select bank --"
    filterable
    clearable
    aria-label="Select default iDEAL bank"
    @update:value="handleUpdate"
  >
    <template #prefix>
      <n-icon :component="IconBank" />
    </template>
  </n-select>
</template>

<style scoped>
.issuer-select {
  width: 100%;
}
</style>
