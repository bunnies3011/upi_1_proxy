<!--
  DashboardCard — chrome chuẩn cho mọi card trong Single_Screen_UI.

  Wrapper mỏng quanh NCard:
  - Normalize header: 12px uppercase title + optional monospaced meta.
  - Slot `extra` cho actions bên phải header.
  - Slot `filter` cho hàng filter phụ (job list chip filter).
  - `bodyPadding` prop điều khiển padding body — mặc định 0 để pane con tự
    quản (cho log/table full-bleed).

  Không dùng CSS bespoke ở caller — mọi tone màu/typography lấy từ theme
  token Naive UI (`var(--n-*)`).
-->
<script setup lang="ts">
import { NCard, NIcon } from 'naive-ui'
import type { Component } from 'vue'

defineProps<{
  title?: string
  icon?: Component
  meta?: string
  bodyPadding?: string
}>()
</script>

<template>
  <n-card
    class="dashboard-card"
    :content-style="`padding:${bodyPadding ?? '0'};display:flex;flex-direction:column;min-height:0;`"
    size="small"
    :bordered="true"
    :segmented="{ content: true }"
  >
    <template #header>
      <div class="head-title">
        <n-icon v-if="icon" :component="icon" size="14" class="head-icon" />
        <span class="head-text">{{ title }}</span>
        <span v-if="meta" class="head-meta mono">{{ meta }}</span>
        <slot name="meta" />
      </div>
    </template>
    <template v-if="$slots.extra" #header-extra>
      <slot name="extra" />
    </template>

    <slot />
  </n-card>
</template>

<style scoped>
.dashboard-card {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
  width: 100%;
}
.dashboard-card :deep(.n-card__content) {
  flex: 1 1 auto;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.dashboard-card :deep(.n-card-header) {
  padding: 8px 14px;
  border-bottom: 1px solid var(--n-border-color);
}
.dashboard-card :deep(.n-card-header__main),
.dashboard-card :deep(.n-card-header__extra) {
  font-size: 11px;
}

.head-title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.45px;
  color: var(--n-text-color);
  min-width: 0;
}
.head-icon {
  color: var(--n-primary-color);
  flex-shrink: 0;
}
.head-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.head-meta {
  font-size: 11px;
  font-weight: 400;
  text-transform: none;
  letter-spacing: 0;
  color: var(--n-text-color-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mono {
  font-family: var(--n-font-family-mono);
}
</style>
