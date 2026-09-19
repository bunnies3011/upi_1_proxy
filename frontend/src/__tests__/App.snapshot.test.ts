/**
 * Component/snapshot test cho `App.vue` — Single_Screen_UI layout.
 *
 * Validates: Requirements 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 12.7, 12.8, 12.9
 *
 * Cover:
 * - R12.1 / R12.8: Layout Single_Screen_UI hiển thị đủ 4 khu vực chính
 *   NGAY khi mount: `JobInputPanel`, `SettingsAccordion`, `JobList`, và
 *   vùng chứa `JobDetailPanel` (ban đầu rỗng — không có panel nào được
 *   auto-mở, đúng R12.3 "compact mặc định, chỉ mở khi user chủ động").
 *   KHÔNG còn login gate — mở tool là vào ngay (thay đổi UX yêu cầu user).
 * - R12.2: `SettingsAccordion` mặc định thu gọn — body
 *   (`.settings-accordion__body`) KHÔNG được render khi `isOpen === false`.
 *   Chỉ có `.settings-accordion__toggle`.
 * - R12.4: CSS layout responsive — `App.vue` chứa breakpoint 1024px cho
 *   desktop và 767/768px cho mobile/tablet.
 * - R12.6: QR ≥ 240×240 pixel trên mobile — `JobDetailPanel.vue` chứa
 *   quy tắc CSS min-width/min-height ≥ 240px cho container QR.
 * - R12.9: kích thước chạm tối thiểu 44×44 pixel — `App.vue` global style
 *   chứa `min-height: 44px` cho button/input/select/textarea, và
 *   `min-width: 44px` cho button.
 *
 * KHÔNG cover ở đây (jsdom hạn chế `getComputedStyle` + `matchMedia`):
 * - Verify runtime computed style theo breakpoint viewport thật.
 * - Verify SSE update trong 2 giây (R12.5) — trách nhiệm của integration
 *   test qua Playwright, không phải component test.
 * - Verify commit-after-confirm khi settings PUT thất bại (R12.7) —
 *   đã cover trong test của `useSettingsStore` (task 32.x).
 *
 * Chiến lược:
 * - Mount App qua @vue/test-utils; mock `fetch` global để `onMounted` của
 *   SettingsAccordion + JobList + useSse không crash trên jsdom.
 * - Đọc CSS thô của SFC (App.vue, JobDetailPanel.vue) để verify các quy
 *   tắc responsive/touch/QR có mặt — jsdom KHÔNG parse `@media` block
 *   nên phải grep raw text; đây là compromise thực dụng cho snapshot test.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { nextTick, ref } from 'vue'

// -----------------------------------------------------------------------------
// Mock `useSse` — composable thật gọi `fetch(/api/events/stream)` liên tục
// và `setTimeout(3000)` để reconnect, khiến Node event loop không thoát sau
// khi test pass. Ta thay bằng stub no-op giữ đủ contract (`connected`,
// `error`, `start`, `stop`) để `App.vue` template không lỗi.
// Phải khai báo `vi.mock` TRƯỚC khi import App để hoisting hoạt động đúng.
// -----------------------------------------------------------------------------
vi.mock('../composables/useSse', () => {
  return {
    useSse: () => ({
      connected: ref(false),
      error: ref<string | null>(null),
      start: () => {
        /* no-op — test không cần thật sự mở SSE stream */
      },
      stop: () => {
        /* no-op */
      },
    }),
  }
})

import App from '../App.vue'

// -----------------------------------------------------------------------------
// Path helpers — đọc raw SFC để test CSS rules
// -----------------------------------------------------------------------------

const __dirname = dirname(fileURLToPath(import.meta.url))
const APP_SFC_PATH = resolve(__dirname, '..', 'App.vue')
const JOB_DETAIL_SFC_PATH = resolve(
  __dirname,
  '..',
  'components',
  'JobDetailPanel.vue',
)

function readSfc(path: string): string {
  return readFileSync(path, 'utf-8')
}

// -----------------------------------------------------------------------------
// Fetch mock — trả về payload rỗng hợp lệ để `onMounted` các store không
// throw. `useSse` cũng dùng `fetch`; ta trả ReadableStream rỗng để nó tự
// kết thúc và scheduleReconnect sau — sẽ được abort khi unmount.
// -----------------------------------------------------------------------------

function makeEmptyStreamResponse(): Response {
  const emptyStream = new ReadableStream({
    start(controller) {
      controller.close()
    },
  })
  return new Response(emptyStream, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  })
}

function buildFetchMock() {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString()

    if (url.endsWith('/api/settings') || url.includes('/api/settings?')) {
      return new Response(JSON.stringify({ settings: {} }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    if (url.endsWith('/api/jobs')) {
      return new Response('[]', {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    if (url.endsWith('/api/events/stream')) {
      return makeEmptyStreamResponse()
    }
    // Default fallback — 404 để phát hiện nếu component gọi endpoint lạ.
    return new Response('{}', {
      status: 404,
      headers: { 'Content-Type': 'application/json' },
    })
  })
}

// -----------------------------------------------------------------------------
// Mount helper
// -----------------------------------------------------------------------------

async function mountApp(): Promise<VueWrapper> {
  // Không còn login gate — mount trực tiếp sẽ render main-grid ngay.
  const wrapper = mount(App, {
    attachTo: document.body,
    global: {
      plugins: [createPinia()],
    },
  })

  // Chờ 2 microtask tick: (1) sau mount, (2) sau khi onMounted của children
  // resolve các Promise từ loadAll (đã mock fetch return sẵn). Đủ để DOM
  // ổn định trước khi assertion.
  await nextTick()
  await nextTick()
  await Promise.resolve()
  await nextTick()

  return wrapper
}

// -----------------------------------------------------------------------------
// Test lifecycle
// -----------------------------------------------------------------------------

let originalFetch: typeof fetch

/**
 * Xoá auth token khỏi jsdom `window.localStorage` (nếu có) — dùng
 * `window.localStorage` thay `globalThis.localStorage` vì Node 22 có
 * localStorage global experimental gây xung đột với jsdom.
 */
function clearBrowserStorage(): void {
  if (typeof window !== 'undefined' && window.localStorage) {
    window.localStorage.clear()
  }
}

beforeEach(() => {
  clearBrowserStorage()
  // Cài Pinia active cho các composable dùng ngoài mount (nếu có).
  setActivePinia(createPinia())

  originalFetch = globalThis.fetch
  globalThis.fetch = buildFetchMock() as unknown as typeof fetch
})

afterEach(() => {
  globalThis.fetch = originalFetch
  clearBrowserStorage()
  // KHÔNG dùng `vi.restoreAllMocks()` — sẽ khôi phục cả `vi.mock('useSse')`
  // ở top-level và làm test kế tiếp lại chạy vào SSE thật (gây hang).
  vi.resetAllMocks()
})

// -----------------------------------------------------------------------------
// Tests
// -----------------------------------------------------------------------------

describe('App.vue Single_Screen_UI layout (Requirements 12.1–12.9)', () => {
  it('renders 4 main areas immediately without login gate (R12.1, R12.3, R12.8)', async () => {
    const wrapper = await mountApp()

    // Area 1: JobInputPanel — nhận diện qua root class `.job-input-panel`.
    expect(
      wrapper.find('.job-input-panel').exists(),
      'JobInputPanel phải render trong main-grid trái (R12.1)',
    ).toBe(true)

    // Area 2: SettingsAccordion — root class `.settings-accordion`.
    expect(
      wrapper.find('.settings-accordion').exists(),
      'SettingsAccordion phải render trong main-grid trái (R12.1, R12.2)',
    ).toBe(true)

    // Area 3: JobList — root class `.job-list`.
    expect(
      wrapper.find('.job-list').exists(),
      'JobList phải render trong main-grid phải (R12.1, R12.8)',
    ).toBe(true)

    // Area 4: Detail stack — ban đầu KHÔNG có `JobDetailPanel` vì `openPanelIds`
    // rỗng (R12.3 — chỉ mở khi user click); nhưng vùng chứa (`section.main-grid__right`)
    // phải tồn tại để có thể mở panel sau.
    expect(
      wrapper.find('.main-grid__right').exists(),
      'Khu vực chứa JobDetailPanel phải tồn tại (R12.3)',
    ).toBe(true)
    expect(
      wrapper.findAll('.job-detail-panel').length,
      'Ban đầu KHÔNG có JobDetailPanel nào mở (R12.3)',
    ).toBe(0)

    // Sanity: main-grid có mặt (2 cột / 1 cột theo breakpoint — không assert
    // computed style vì jsdom).
    expect(wrapper.find('.main-grid').exists()).toBe(true)

    wrapper.unmount()
  })

  it('SettingsAccordion body is collapsed by default (R12.2)', async () => {
    const wrapper = await mountApp()

    const accordion = wrapper.find('.settings-accordion')
    expect(accordion.exists()).toBe(true)

    // Toggle button luôn có, dùng để mở/thu gọn.
    expect(
      accordion.find('.settings-accordion__toggle').exists(),
      'Toggle button luôn hiện (R12.2)',
    ).toBe(true)

    // Body (`.settings-accordion__body`) KHÔNG được render khi thu gọn —
    // template dùng `v-if="isOpen"` với `isOpen = ref(false)` mặc định.
    expect(
      accordion.find('.settings-accordion__body').exists(),
      'Body accordion phải ẩn khi mặc định thu gọn (R12.2)',
    ).toBe(false)

    // aria-expanded="false" để trợ năng đúng trạng thái.
    const toggle = accordion.find('.settings-accordion__toggle')
    expect(toggle.attributes('aria-expanded')).toBe('false')

    wrapper.unmount()
  })

  // ---------------------------------------------------------------------------
  // Kiểm tra CSS raw — jsdom không parse `@media`, không compute style theo
  // viewport thật; nên assertion dựa vào text CSS của SFC để chứng minh quy
  // tắc responsive/touch/QR có trong bundle.
  // ---------------------------------------------------------------------------

  it('App.vue defines responsive breakpoints 1024px and 768px (R12.4)', () => {
    const source = readSfc(APP_SFC_PATH)

    // R12.4 — desktop breakpoint ≥ 1024px.
    expect(
      /@media\s*\(\s*min-width:\s*1024px\s*\)/.test(source),
      'App.vue phải khai báo breakpoint min-width 1024px (R12.4)',
    ).toBe(true)

    // R12.4 — mobile breakpoint < 768px (max-width: 767px).
    expect(
      /@media\s*\(\s*max-width:\s*767px\s*\)/.test(source),
      'App.vue phải khai báo breakpoint max-width 767px (R12.4)',
    ).toBe(true)

    // Tablet breakpoint 768–1023px (không bắt buộc — nhưng App có, verify
    // để lock behavior khỏi bị regress).
    expect(
      /@media\s*\(\s*min-width:\s*768px\s*\)\s*and\s*\(\s*max-width:\s*1023px\s*\)/.test(
        source,
      ),
      'App.vue nên có tablet breakpoint 768–1023px (R12.4)',
    ).toBe(true)
  })

  it('App.vue enforces 44×44 touch target for interactive elements (R12.9)', () => {
    const source = readSfc(APP_SFC_PATH)

    // Global style block phải có `min-height: 44px` cho button/input/select/textarea.
    // Test khá lỏng để không phụ thuộc thứ tự selector; kiểm tra sự có mặt
    // đồng thời của selector list + rule.
    expect(
      /input[^{]*button[^{]*select[^{]*textarea\s*\{[^}]*min-height:\s*44px/.test(
        source,
      ),
      'App.vue phải áp min-height 44px cho input/button/select/textarea (R12.9)',
    ).toBe(true)

    // Button phải có min-width 44px riêng.
    expect(
      /button\s*\{[^}]*min-width:\s*44px/.test(source),
      'App.vue phải áp min-width 44px cho button (R12.9)',
    ).toBe(true)
  })

  it('JobDetailPanel.vue enforces QR minimum size ≥ 240×240 on mobile (R12.6)', () => {
    const source = readSfc(JOB_DETAIL_SFC_PATH)

    // Chấp nhận cả `min-width` lẫn `width` ≥ 240px, và cả `min-height` /
    // `height`. Kiểm tra tối thiểu một cặp width/height ≥ 240px xuất hiện
    // trên selector liên quan QR (.qr-image / .qr-section img / .qr-container).
    const hasQrMinWidth =
      /(qr-image|qr-section|qr-container|qr-code)[^}]*min-width:\s*(24\d|2[5-9]\d|[3-9]\d\d)px/.test(
        source,
      ) ||
      /(qr-image|qr-section|qr-container|qr-code)[^}]*width:\s*(24\d|2[5-9]\d|[3-9]\d\d)px/.test(
        source,
      )

    const hasQrMinHeight =
      /(qr-image|qr-section|qr-container|qr-code)[^}]*min-height:\s*(24\d|2[5-9]\d|[3-9]\d\d)px/.test(
        source,
      ) ||
      /(qr-image|qr-section|qr-container|qr-code)[^}]*height:\s*(24\d|2[5-9]\d|[3-9]\d\d)px/.test(
        source,
      )

    expect(
      hasQrMinWidth,
      'JobDetailPanel.vue phải khai báo QR container width ≥ 240px (R12.6)',
    ).toBe(true)
    expect(
      hasQrMinHeight,
      'JobDetailPanel.vue phải khai báo QR container height ≥ 240px (R12.6)',
    ).toBe(true)
  })
})
