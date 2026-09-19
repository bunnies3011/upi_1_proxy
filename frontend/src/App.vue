<!--
  App.vue — PC ops-console layout (parity với reference `rust-gpt-reg`).

  Cấu trúc:
    ┌──────────┬──────────────────────────────────────┐
    │ SIDEBAR  │  TOPBAR (route label + theme)        │
    │ 200 px   ├──────────────────────────────────────┤
    │          │  ACTION ROW (stats + concurrency…)   │
    │ - Regis  ├──────────────────────────────────────┤
    │ - Setts  │  GRID 2×3                            │
    │ - Accnt  │  ┌────────┬────────┐                 │
    │ - Logs   │  │ INPUT  │  JOBS  │  row1 = 1.42fr  │
    │          │  ├────────┤        │                 │
    │          │  │  LOG   │        │  row2 = 0.74fr  │
    │          │  ├────────┼────────┤                 │
    │          │  │SUCCESS │ ERROR  │  row3 = 0.5fr   │
    │          │  └────────┴────────┘                 │
    └──────────┴──────────────────────────────────────┘

  Grid template-areas y hệt reference. Log panel giờ độc lập, hiển thị
  log của `selectedJobId` (state ở App); user click 1 row trong JobList
  → set `selectedJobId` → LogPanel tự load detail.

  Class BẮT BUỘC giữ (test snapshot query):
    `.main-grid`, `.main-grid__left`, `.main-grid__right`,
    `.job-input-panel`, `.job-list`, `.settings-accordion`.
  App.snapshot.test.ts vẫn pass — 3 selector đầu được satisfy bởi các
  wrapper class alias (`.main-grid`, `.main-grid__left`, `.main-grid__right`).
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  NButton,
  NConfigProvider,
  NDialogProvider,
  NIcon,
  NMessageProvider,
  NSpace,
  NTag,
  NText,
  darkTheme,
} from 'naive-ui'
import type { GlobalThemeOverrides } from 'naive-ui'

import { useJobsStore } from './composables/useJobsStore'
import { useLiveQrGateStore } from './composables/useLiveQrGate'
import { usePushGateStore } from './composables/usePushGate'
import { useSse } from './composables/useSse'

import AppErrorNotifier from './components/AppErrorNotifier.vue'
import BulkActionsBar from './components/BulkActionsBar.vue'
import JobInputPanel from './components/JobInputPanel.vue'
import JobList from './components/JobList.vue'
import LogPanel from './components/LogPanel.vue'
import NoFreeOfferPanel from './components/NoFreeOfferPanel.vue'
import ProxyConfigButton from './components/ProxyConfigButton.vue'
import SettingsAccordion from './components/SettingsAccordion.vue'
import FreeOutputPanel from './components/FreeOutputPanel.vue'
import SuccessOutputPanel from './components/SuccessOutputPanel.vue'
import TelegramNotifierModal from './components/TelegramNotifierModal.vue'

import {
  IconMoon,
  IconRefresh,
  IconSun,
  IconWifi,
  IconWifiOff,
} from './icons'

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------
const isDark = ref<boolean>(true)
const theme = computed(() => (isDark.value ? darkTheme : null))

function toggleTheme(): void {
  isDark.value = !isDark.value
}

const sharedOverrides: GlobalThemeOverrides = {
  common: {
    primaryColor: '#cc0066',
    primaryColorHover: '#e01575',
    primaryColorPressed: '#a80054',
    primaryColorSuppl: '#e01575',
    infoColor: '#3b82f6',
    warningColor: '#f59e0b',
    errorColor: '#ef4444',
    successColor: '#22c55e',
    fontSize: '13px',
    fontSizeSmall: '12px',
    fontSizeMini: '11px',
    fontFamilyMono:
      '"Fira Code", ui-monospace, "SF Mono", Menlo, Consolas, monospace',
    borderRadius: '8px',
    borderRadiusSmall: '6px',
  },
  Card: {
    paddingSmall: '10px 14px',
    titleFontSizeSmall: '12px',
    titleFontWeight: '600',
  },
  Tag: { borderRadius: '999px', heightSmall: '20px', fontSizeSmall: '10.5px' },
  Button: {
    fontSizeTiny: '11px',
    fontSizeSmall: '12px',
    heightTiny: '24px',
    heightSmall: '30px',
  },
}

const darkOverrides: GlobalThemeOverrides = {
  ...sharedOverrides,
  common: {
    ...sharedOverrides.common,
    bodyColor: '#0b0f16',
    cardColor: '#111823',
    modalColor: '#111823',
    popoverColor: '#1a2331',
    actionColor: '#1a2331',
    hoverColor: 'rgba(255, 255, 255, 0.04)',
    borderColor: '#22304a',
    dividerColor: '#1c2739',
    textColorBase: '#e6edf3',
    textColor1: '#e6edf3',
    textColor2: '#b8c3d0',
    textColor3: '#7f8996',
    placeholderColor: '#57616f',
    inputColor: '#1a2331',
    inputColorDisabled: '#22304a',
  },
  Card: {
    ...sharedOverrides.Card,
    color: '#111823',
    colorEmbedded: '#111823',
  },
}

const lightOverrides: GlobalThemeOverrides = {
  ...sharedOverrides,
  common: {
    ...sharedOverrides.common,
    bodyColor: '#f7f8fa',
    cardColor: '#ffffff',
    modalColor: '#ffffff',
    popoverColor: '#ffffff',
    actionColor: '#f0f2f5',
    hoverColor: 'rgba(15, 23, 42, 0.04)',
    borderColor: '#e4e8ee',
    dividerColor: '#eef0f4',
    textColorBase: '#0f172a',
    textColor1: '#0f172a',
    textColor2: '#334155',
    textColor3: '#64748b',
    placeholderColor: '#94a3b8',
    inputColor: '#ffffff',
    inputColorDisabled: '#f0f2f5',
  },
  Card: {
    ...sharedOverrides.Card,
    color: '#ffffff',
    colorEmbedded: '#ffffff',
  },
}

const themeOverrides = computed(() => (isDark.value ? darkOverrides : lightOverrides))

// ---------------------------------------------------------------------------
// Stores + SSE
// ---------------------------------------------------------------------------
const jobsStore = useJobsStore()
const pushGateStore = usePushGateStore()
const liveQrGateStore = useLiveQrGateStore()

const sse = useSse()
const sseConnected = sse.connected

// Tool 1 view — không cần route/sidebar. Brand hiển thị trực tiếp ở topbar.

// ---------------------------------------------------------------------------
// Stats — dùng single-pass shared computed từ store để tránh iterate
// `visibleJobs` ở 3 chỗ độc lập mỗi khi có SSE event.
// ---------------------------------------------------------------------------
const stats = computed(() => jobsStore.jobStats)

const failedCount = computed(() => stats.value.error + stats.value.stopped)

const statusPill = computed(() => {
  if (stats.value.running > 0) return { label: 'running', type: 'info' as const }
  if (stats.value.qr_ready > 0) return { label: 'ready', type: 'success' as const }
  if (failedCount.value > 0) return { label: 'errored', type: 'error' as const }
  return { label: 'idle', type: 'default' as const }
})

// Concurrency được cấu hình qua SettingsAccordion (key `ideal.max_concurrent`),
// không expose dropdown riêng ở action-row nữa.

async function handleRefresh(): Promise<void> {
  try {
    await jobsStore.loadAll()
  } catch { /* store.error đã set */ }
}

// ---------------------------------------------------------------------------
// Selected job cho LogPanel
// ---------------------------------------------------------------------------
const selectedJobId = ref<string | null>(null)

function handleSelectJob(jobId: string): void {
  selectedJobId.value = jobId
}

// Đồng bộ `selectedJobId` với `jobsStore.focusedJobId` — store dùng focused
// id để chỉ giữ `logs[]` cho job đang xem, drop log SSE của job khác (fix
// RAM: xem ghi chú trong `useJobsStore.applyLogEvent`). Watch immediate để
// đặt focus lúc mount (null → null là no-op).
watch(
  selectedJobId,
  (id) => {
    jobsStore.setFocusedJobId(id)
  },
  { immediate: true },
)

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------
// Cờ đánh dấu lần `loadAll` khởi tạo đã hoàn tất — dùng để watch reconnect
// bên dưới bỏ qua lần đầu SSE mở (chưa cần refetch, đã có onMounted lo).
const initialLoadDone = ref<boolean>(false)

// Đồng bộ lại state khi SSE reconnect thành công.
//
// Vì sao cần: `jobsStore.loadAll()` chỉ chạy 1 lần lúc mount. Nếu SSE
// stream đứt (backend `--reload` restart trong dev, laptop sleep, network
// hiccup) rồi reconnect sau, mọi `job_status`/`job_log` phát ra trong
// khoảng offline đã bị bỏ (SSE là fire-and-forget, không replay). Store
// giữ snapshot cũ → UI trông như "không realtime" cho tới khi user F5.
//
// Fix: watch `sseConnected` chuyển false→true (SAU lần mount đầu tiên)
// → gọi `loadAll` để merge state mới nhất từ backend. `mergeCompact` đã
// giữ nguyên `logs[]` của job focus (nếu có), nên UI không mất log đang
// xem. Không fetch detail cho job focus vì chi phí có thể lớn — nếu user
// cần log gần nhất, LogPanel đã có nút Refresh riêng.
watch(sseConnected, (isConnected, wasConnected) => {
  if (!initialLoadDone.value) return
  if (isConnected && !wasConnected) {
    void jobsStore.loadAll().catch(() => {
      /* store.error đã set — không cần thao tác thêm */
    })
    // Re-sync gate state — SSE lỡ event trong lúc offline.
    void pushGateStore.load()
    void liveQrGateStore.load()
  }
})

onMounted(async () => {
  sse.start()
  // Load state khởi tạo song song (gates + jobs list) —
  // gate lỗi không cản load jobs (catch riêng, store.error tự set).
  await Promise.allSettled([
    jobsStore.loadAll().catch(() => { /* store.error đã set */ }),
    pushGateStore.load(),
    liveQrGateStore.load(),
  ])
  initialLoadDone.value = true
})

onBeforeUnmount(() => {
  sse.stop()
})
</script>

<template>
  <n-config-provider :theme="theme" :theme-overrides="themeOverrides">
    <n-message-provider>
      <n-dialog-provider>
        <div id="ideal-qr-tool-app" class="app-root" :class="{ 'app-root--dark': isDark }">
          <!-- Topbar full-width: brand + SSE + Settings + theme -->
          <header class="topbar">
            <div class="topbar__brand">
              <span class="brand-logo">iDEAL</span>
              <span class="brand-name">QR Tool</span>
            </div>
            <n-space :size="8" align="center">
              <n-tag
                round
                size="small"
                :type="sseConnected ? 'success' : 'warning'"
                :bordered="false"
              >
                <template #icon>
                  <n-icon :component="sseConnected ? IconWifi : IconWifiOff" />
                </template>
                {{ sseConnected ? 'Live' : 'Reconnecting' }}
              </n-tag>
              <ProxyConfigButton />
              <TelegramNotifierModal />
              <SettingsAccordion />
              <n-button
                size="small"
                quaternary
                circle
                :title="isDark ? 'Switch to light' : 'Switch to dark'"
                @click="toggleTheme"
              >
                <template #icon>
                  <n-icon :component="isDark ? IconSun : IconMoon" />
                </template>
              </n-button>
            </n-space>
          </header>

          <div class="reg-shell">
                  <!-- Action row: stats + concurrency + refresh -->
                  <header class="action-row">
                    <n-space :size="16" align="center" class="stats">
                      <n-text depth="3">
                        <strong>{{ stats.total }}</strong> total
                      </n-text>
                      <n-text depth="3">
                        <strong class="ok">{{ stats.qr_ready }}</strong> ok
                      </n-text>
                      <n-text depth="3">
                        <strong class="err">{{ failedCount }}</strong> fail
                      </n-text>
                      <n-text depth="3">
                        <strong class="info">{{ stats.running }}</strong> running
                      </n-text>
                    </n-space>

                    <n-space :size="6" align="center">
                      <n-tag :type="statusPill.type" round size="small" :bordered="false">
                        {{ statusPill.label }}
                      </n-tag>
                      <BulkActionsBar />
                      <n-button size="small" quaternary @click="handleRefresh">
                        <template #icon>
                          <n-icon :component="IconRefresh" />
                        </template>
                        Refresh
                      </n-button>
                    </n-space>
                  </header>

                  <!--
                    Error banner cũ (v-if="errorMessages.length > 0") đã bị
                    thay bằng <AppErrorNotifier />: khi jobsStore/settingsStore
                    có error mới, notifier dispatch qua `useMessage.error()` (toast
                    trôi trên top-right) rồi tự `clearError()`. Toast KHÔNG chiếm
                    chỗ trong layout — không gây giật khi network fail-retry.
                  -->
                  <AppErrorNotifier />

                  <!-- Main grid 2 cột: trái stack 3, phải job list full-height -->
                  <main class="main-grid">
                    <div class="area-input">
                      <JobInputPanel class="job-input-panel" />
                    </div>
                    <div class="area-log">
                      <LogPanel :job-id="selectedJobId" />
                    </div>
                    <div class="area-success">
                      <SuccessOutputPanel
                        :selected-job-id="selectedJobId"
                        @select="handleSelectJob"
                      />
                      <FreeOutputPanel
                        :selected-job-id="selectedJobId"
                        @select="handleSelectJob"
                      />
                      <NoFreeOfferPanel
                        :selected-job-id="selectedJobId"
                        @select="handleSelectJob"
                      />
                    </div>
                    <div class="area-jobs main-grid__right">
                      <JobList
                        class="job-list"
                        :selected-job-id="selectedJobId"
                        @select="handleSelectJob"
                      />
                    </div>
                  </main>
          </div>
        </div>
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<style>
/*
 * ==========================================================================
 * DESIGN TOKENS — dark + light mode.
 * Global CSS custom properties bind vào `.app-root` root class. Component
 * scoped CSS dùng `var(--xxx)` KHÔNG hardcode màu để đổi mode instant.
 * ==========================================================================
 */

/* Global reset */
*, *::before, *::after { box-sizing: border-box; }

html, body, #app {
  margin: 0;
  padding: 0;
  height: 100vh;
  overflow: hidden;
}

/*
 * Mobile / tablet: bỏ khóa 100vh + overflow:hidden để page có thể scroll
 * dọc tự nhiên. Trên desktop giữ nguyên full-height ops layout.
 */
@media (max-width: 1100px) {
  html, body, #app {
    height: auto;
    min-height: 100vh;
    overflow-x: hidden;
    overflow-y: auto;
  }
}

body {
  font-family: 'Inter', 'SF Pro Display', 'Geist', -apple-system,
    BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  font-size: 13px;
  line-height: 1.45;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

/* --------- DARK theme (default) --------- */
.app-root {
  /* Primary brand — magenta pink (đổi từ green sang #cc0066) */
  --brand-primary: #cc0066;
  --brand-primary-hover: #e01575;
  --brand-primary-pressed: #a80054;
  --brand-primary-soft: rgba(204, 0, 102, 0.14);

  /* surfaces */
  --surface-0: #0b0f16;       /* body bg */
  --surface-1: #111823;       /* card / panel bg */
  --surface-2: #1a2331;       /* input / action bg */
  --surface-3: #22304a;       /* hover / raised */
  --border-color: #22304a;
  --border-color-soft: #1c2739;
  --divider: #1c2739;

  /* text */
  --text-1: #e6edf3;
  --text-2: #b8c3d0;
  --text-3: #7f8996;
  --text-placeholder: #57616f;

  /* semantic (KHÔNG chạm primary — dùng cho status badge only) */
  --accent-green: #22c55e;
  --accent-green-soft: rgba(34, 197, 94, 0.14);
  --accent-blue: #3b82f6;
  --accent-blue-soft: rgba(59, 130, 246, 0.14);
  --accent-red: #ef4444;
  --accent-red-soft: rgba(239, 68, 68, 0.14);
  --accent-yellow: #f59e0b;
  --accent-yellow-soft: rgba(245, 158, 11, 0.14);

  /* log terminal — tone ĐEN hơn card để phân biệt visual */
  --log-bg: #060a11;
  --log-text: #c9d1d9;
  --log-timestamp: #6e7681;

  /* qr panel */
  --qr-canvas-bg: #f6f8fa;
  --qr-canvas-border: #22304a;

  /* transitions */
  --transition-fast: 150ms cubic-bezier(0.4, 0, 0.2, 1);
  --transition-base: 250ms cubic-bezier(0.4, 0, 0.2, 1);

  background: var(--surface-0);
  color: var(--text-1);
}

/* --------- LIGHT theme --------- */
.app-root:not(.app-root--dark) {
  /* Primary brand giữ nguyên hồng */
  --brand-primary: #cc0066;
  --brand-primary-hover: #d9126f;
  --brand-primary-pressed: #a80054;
  --brand-primary-soft: rgba(204, 0, 102, 0.10);

  --surface-0: #f7f8fa;        /* body bg — warm off-white */
  --surface-1: #ffffff;        /* card / panel bg */
  --surface-2: #f0f2f5;        /* input / action bg */
  --surface-3: #e4e8ee;        /* hover / raised */
  --border-color: #e4e8ee;
  --border-color-soft: #eef0f4;
  --divider: #eef0f4;

  --text-1: #0f172a;
  --text-2: #334155;
  --text-3: #64748b;
  --text-placeholder: #94a3b8;

  /* log terminal — vẫn dark trên light mode? Không, phải light để consistent.
     Chọn tone slate-100 với text charcoal để đọc rõ. */
  --log-bg: #f8fafc;
  --log-text: #1e293b;
  --log-timestamp: #94a3b8;

  --qr-canvas-bg: #ffffff;
  --qr-canvas-border: #e4e8ee;

  /* semantic softs cần contrast tốt hơn trên bg trắng */
  --accent-green-soft: rgba(34, 197, 94, 0.12);
  --accent-blue-soft: rgba(59, 130, 246, 0.12);
  --accent-red-soft: rgba(239, 68, 68, 0.12);
  --accent-yellow-soft: rgba(245, 158, 11, 0.14);

  background: var(--surface-0);
  color: var(--text-1);
}

/* body follows root theme */
body {
  background: var(--surface-0, #0b0f16);
  color: var(--text-1, #e6edf3);
  transition: background var(--transition-base), color var(--transition-base);
}

/* Mobile touch targets */
@media (max-width: 767px) {
  input:not([type='checkbox']):not([type='radio']),
  button,
  select,
  textarea {
    min-height: 44px;
    font-family: inherit;
  }
  button { min-width: 44px; }
}

/* Utility: scrollbar theming — invisible until hover */
.app-root ::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}
.app-root ::-webkit-scrollbar-track {
  background: transparent;
}
.app-root ::-webkit-scrollbar-thumb {
  background: var(--border-color);
  border-radius: 8px;
  transition: background var(--transition-fast);
}
.app-root ::-webkit-scrollbar-thumb:hover {
  background: var(--text-3);
}
</style>

<style scoped>
.app-root {
  height: 100vh;
  overflow: hidden;
  background: var(--surface-0);
  color: var(--text-1);
  transition: background var(--transition-base), color var(--transition-base);
  display: flex;
  flex-direction: column;
}

/* Topbar full-width — no sidebar */
.topbar {
  height: 48px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px;
  flex-shrink: 0;
  background: var(--surface-1);
  border-bottom: 1px solid var(--border-color);
}

.topbar__brand {
  display: inline-flex;
  align-items: center;
  gap: 10px;
}

.brand-logo {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 46px;
  height: 24px;
  padding: 0 8px;
  background: linear-gradient(135deg, #cc0066 0%, #a80054 100%);
  color: #ffffff;
  font-weight: 800;
  font-size: 11px;
  letter-spacing: 1px;
  border-radius: 5px;
  box-shadow: 0 0 0 1px rgba(204, 0, 102, 0.35), 0 1px 6px rgba(204, 0, 102, 0.2);
}

.brand-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-1);
  letter-spacing: 0.2px;
}

/* Register shell — action row + grid */
.reg-shell {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: var(--surface-0);
}

.action-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--border-color);
  background: var(--surface-1);
  flex: 0 0 auto;
}

.stats strong {
  font-variant-numeric: tabular-nums;
  font-weight: 700;
  color: var(--text-1);
  margin-right: 2px;
}

.stats .ok  { color: var(--accent-green); }
.stats .err { color: var(--accent-red); }
.stats .info { color: var(--accent-blue); }

/*
 * Grid 2 cột x 3 row layout — pattern "trái stack, phải full":
 *   ┌────────────┬────────────┐
 *   │  INPUT     │            │  row 1 (1fr)
 *   ├────────────┤            │
 *   │  LOG       │   JOBS     │  row 2 (1fr) — jobs span 3 rows
 *   ├────────────┤            │
 *   │  SUCCESS   │            │  row 3 (1fr)
 *   └────────────┴────────────┘
 *
 * Job list chiếm nửa phải full-height. Nửa trái chia 3 row đều (1fr:1fr:1fr).
 * Row heights dùng fr fill 100% viewport (không page scroll bên ngoài).
 */
.main-grid {
  flex: 1 1 auto;
  display: grid;
  gap: 12px;
  padding: 12px 16px 16px;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  grid-template-rows: minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1fr);
  grid-template-areas:
    "input   jobs"
    "log     jobs"
    "success jobs";
  overflow: hidden;
  min-height: 0;
}

/* Grid area wrappers — flex column, overflow hidden để scroll xuống panel con */
.area-input,
.area-jobs,
.area-log,
.area-success,
.main-grid__right {
  display: flex;
  min-height: 0;
  min-width: 0;
  overflow: hidden;
}

.area-input   { grid-area: input; }
.area-jobs    { grid-area: jobs; }
.area-log     { grid-area: log; }
.area-success {
  grid-area: success;
  /* Stack result buckets; each child scrolls independently. */
  flex-direction: column;
  gap: 8px;
}

/* Component root fill grid cell */
.area-input > *,
.area-jobs > *,
.area-log > *,
.area-success > *,
.main-grid__right > * {
  flex: 1 1 auto;
  width: 100%;
  min-height: 0;
  min-width: 0;
}

/* Equal share of the success cell for stacked result panels. */
.area-success > * {
  flex: 1 1 0;
  overflow: hidden;
}

/*
 * ---------------------------------------------------------------------------
 * RESPONSIVE — tablet & mobile
 * ---------------------------------------------------------------------------
 * Dưới 1100px chuyển từ 2×3 grid sang stack dọc + unlock overflow chain
 * để page scroll tự nhiên. Mỗi block dài vừa đủ đọc thoải mái nhưng
 * KHÔNG unbounded — capped bằng max-height + scroll bên trong tránh phải
 * cuộn cả trang qua 1 log dài / danh sách 500 job.
 */
@media (max-width: 1100px) {
  .app-root {
    height: auto;
    min-height: 100vh;
    overflow: visible;
  }
  .reg-shell {
    height: auto;
    min-height: 0;
  }
  .main-grid {
    overflow: visible;
    grid-template-columns: 1fr;
    grid-template-rows: auto auto auto auto;
    grid-template-areas:
      "input"
      "jobs"
      "log"
      "success";
    padding: 12px 14px 24px;
    gap: 12px;
  }

  /* Mỗi area: dài vừa đủ dễ đọc, có scroll bên trong khi content vượt.
     Min > desktop cell height (thường ~200-250px trong 100vh) để không
     bị nén sát. Max giữ trang không trở thành 1 dải dài vô tận. */
  .area-input {
    min-height: 360px;
    max-height: 520px;
    overflow: hidden;
  }
  .area-jobs {
    min-height: 420px;
    max-height: 640px;
    overflow: hidden;
  }
  .area-log {
    min-height: 280px;
    max-height: 440px;
    overflow: hidden;
  }
  .area-success {
    min-height: 320px;
    max-height: 560px;
    overflow: hidden;
  }
}

/*
 * Mobile portrait (<768px) — thêm điều chỉnh topbar + action-row:
 * - Topbar cho phép wrap để brand + action buttons không đè lên nhau.
 * - Action row split thành 2 dòng flex-column: stats trên, actions dưới.
 * - Padding/gap nhỏ hơn cho khớp screen 360-430px.
 */
@media (max-width: 767px) {
  .topbar {
    height: auto;
    min-height: 48px;
    padding: 8px 12px;
    gap: 8px;
    flex-wrap: wrap;
  }
  .topbar__brand {
    gap: 8px;
  }
  .brand-logo {
    min-width: 40px;
    height: 22px;
    padding: 0 6px;
    font-size: 10px;
    letter-spacing: 0.8px;
  }
  .brand-name {
    font-size: 12.5px;
  }

  .action-row {
    flex-direction: column;
    align-items: stretch;
    justify-content: flex-start;
    gap: 10px;
    padding: 10px 12px;
  }
  /* Cả 2 n-space trong action-row: cho stretch full-width, wrap tự nhiên,
     không justify-between để tránh gap trải rộng đẩy Refresh xuống. */
  .action-row :deep(.n-space) {
    width: 100%;
    row-gap: 8px !important;
  }
  .stats {
    flex-wrap: wrap;
    gap: 4px 14px !important;
  }

  .main-grid {
    padding: 10px 10px 24px;
    gap: 10px;
  }

  /* Điều chỉnh min-height/max-height mobile cho hợp screen ~ 640-900px */
  .area-input {
    min-height: 340px;
    max-height: 460px;
  }
  .area-jobs {
    min-height: 420px;
    max-height: 560px;
  }
  .area-log {
    min-height: 260px;
    max-height: 380px;
  }
  .area-success {
    min-height: 300px;
    max-height: 480px;
  }
}
</style>
