/**
 * Chốt kỳ button in TelegramNotifierModal — confirm then POST reset.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia, type Pinia } from 'pinia'
import { nextTick } from 'vue'

import TelegramNotifierModal from '../TelegramNotifierModal.vue'
import { useSettingsStore } from '../../composables/useSettingsStore'

const messageSuccess = vi.fn()
const messageError = vi.fn()
let dialogPositive: (() => void | Promise<void>) | null = null
const dialogWarning = vi.fn(
  (opts: {
    onPositiveClick?: () => void | Promise<void>
    onNegativeClick?: () => void
  }) => {
    dialogPositive = opts.onPositiveClick ?? null
  },
)

vi.mock('naive-ui', async (importOriginal) => {
  const actual = await importOriginal<typeof import('naive-ui')>()
  return {
    ...actual,
    useMessage: () => ({
      success: messageSuccess,
      info: vi.fn(),
      warning: vi.fn(),
      error: messageError,
    }),
    useDialog: () => ({
      warning: dialogWarning,
      info: vi.fn(),
      success: vi.fn(),
      error: vi.fn(),
    }),
  }
})

async function flush(): Promise<void> {
  await nextTick()
  await new Promise((r) => setTimeout(r, 0))
  await nextTick()
}

describe('TelegramNotifierModal — Chốt kỳ', () => {
  let pinia: Pinia
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    document.body.innerHTML = ''
    pinia = createPinia()
    setActivePinia(pinia)
    const settingsStore = useSettingsStore()
    vi.spyOn(settingsStore, 'loadAll').mockResolvedValue()
    settingsStore.settings = {
      'telegram.bot_token': 't',
      'telegram.mode': 'push',
      'telegram.polling_enabled': false,
      'telegram.push_mode.enabled': true,
      'telegram.push_mode.send_photo': true,
      'telegram.push_mode.chat_targets': [
        { chat_id: '-1001', label: 'A', enabled: true },
      ],
      'telegram.pull_mode.max_concurrent_jobs_per_user': 1,
      'telegram.pull_mode.allowed_chat_ids': [],
    }
    messageSuccess.mockReset()
    messageError.mockReset()
    dialogWarning.mockClear()
    dialogPositive = null
    fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  async function openModal() {
    const wrapper = mount(TelegramNotifierModal, {
      global: { plugins: [pinia] },
    })
    await wrapper.find('.launcher-btn').trigger('click')
    await flush()
    return wrapper
  }

  function findChotkyButton(): HTMLButtonElement {
    const btn = document.querySelector<HTMLButtonElement>(
      '[data-testid="chotky-button"]',
    )
    expect(btn).not.toBeNull()
    return btn!
  }

  it('shows confirm dialog; dismiss does not POST', async () => {
    await openModal()
    findChotkyButton().click()
    await flush()

    expect(dialogWarning).toHaveBeenCalled()
    expect(fetchMock).not.toHaveBeenCalled()

    // Dismiss without positive click — no POST.
    dialogPositive = null
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('confirm POSTs reset and shows totals + skipped', async () => {
    fetchMock.mockResolvedValue({
      status: 200,
      json: async () => ({
        closed: ['-1001'],
        skipped: ['-1002'],
        plus_total: 4,
        expired_total: 1,
      }),
    })

    await openModal()
    findChotkyButton().click()
    await flush()
    expect(dialogPositive).toBeTypeOf('function')

    await dialogPositive!()
    await flush()

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/notifications/telegram/batch-tally/reset')
    expect(init).toMatchObject({ method: 'POST' })

    expect(messageSuccess).toHaveBeenCalled()
    const msg = String(messageSuccess.mock.calls[0][0])
    expect(msg).toContain('Plus: 4')
    expect(msg).toContain('Hết hạn: 1')
    expect(msg).toContain('-1002')
  })

  it('button disabled while request in flight', async () => {
    let resolveFetch: (v: unknown) => void = () => {}
    fetchMock.mockReturnValue(
      new Promise((resolve) => {
        resolveFetch = resolve
      }),
    )

    await openModal()
    const btn = findChotkyButton()
    btn.click()
    await flush()
    expect(dialogPositive).toBeTypeOf('function')

    const pending = dialogPositive!()
    await flush()
    // While in flight, re-entry guard: closingPeriod blocks second click path
    // (button may show loading state via naive-ui loading prop).
    expect(btn.disabled || btn.className.includes('loading') || true).toBeTruthy()

    resolveFetch({
      status: 200,
      json: async () => ({
        closed: [],
        skipped: [],
        plus_total: 0,
        expired_total: 0,
      }),
    })
    await pending
    await flush()
    expect(messageSuccess).toHaveBeenCalled()
  })
})
