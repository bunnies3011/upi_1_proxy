/**
 * Round-trip for the Session cache section in `SettingsAccordion`.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia, type Pinia } from 'pinia'

import SettingsAccordion from '../SettingsAccordion.vue'
import { useSettingsStore } from '../../composables/useSettingsStore'

async function flush(): Promise<void> {
  await Promise.resolve()
  await new Promise((r) => setTimeout(r, 0))
}

describe('SettingsAccordion — Session cache section', () => {
  let pinia: Pinia
  let settingsStore: ReturnType<typeof useSettingsStore>
  let fetchMock: ReturnType<typeof vi.fn>
  let confirmMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    document.body.innerHTML = ''
    pinia = createPinia()
    setActivePinia(pinia)
    settingsStore = useSettingsStore()
    vi.spyOn(settingsStore, 'loadAll').mockResolvedValue()
    settingsStore.settings = {
      'session_cache.enabled': true,
      'session_cache.ttl_hours': 24,
    }
    fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 })
    vi.stubGlobal('fetch', fetchMock)
    confirmMock = vi.fn(() => true)
    vi.stubGlobal('confirm', confirmMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  async function openModal() {
    const wrapper = mount(SettingsAccordion, { global: { plugins: [pinia] } })
    await wrapper.find('.settings-accordion__toggle').trigger('click')
    await flush()
    return wrapper
  }

  it('renders a clear-cache button', async () => {
    await openModal()
    const body = document.body.textContent || ''
    expect(body).toContain('Session cache')
    expect(body).toContain('Clear cached sessions')
  })

  it('DELETEs all session cache after confirm', async () => {
    await openModal()
    const button = Array.from(
      document.querySelectorAll<HTMLButtonElement>('button'),
    ).find((b) => b.textContent?.includes('Clear cached sessions'))
    expect(button).toBeTruthy()

    button!.click()
    await flush()

    expect(confirmMock).toHaveBeenCalled()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/session-cache')
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: 'DELETE' })
    expect(document.body.textContent || '').toContain('Cached sessions cleared.')
  })
})
