/**
 * Round-trip for the UPI direct settings section.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia, type Pinia } from 'pinia'

import SettingsAccordion from '../SettingsAccordion.vue'
import { useSettingsStore } from '../../composables/useSettingsStore'

async function flush(): Promise<void> {
  await Promise.resolve()
  await new Promise((r) => setTimeout(r, 0))
}

describe('SettingsAccordion — UPI direct section', () => {
  let pinia: Pinia
  let settingsStore: ReturnType<typeof useSettingsStore>

  beforeEach(() => {
    document.body.innerHTML = ''
    pinia = createPinia()
    setActivePinia(pinia)
    settingsStore = useSettingsStore()
    vi.spyOn(settingsStore, 'loadAll').mockResolvedValue()
    settingsStore.settings = {
      'upi_direct.proxy_checkout': ['socks5://a:b@h:1'],
      'upi_direct.proxy_promotion': ['socks5://c:d@h:2'],
      'upi_direct.max_concurrent': 3,
      'upi_direct.run_timeout_seconds': 300,
      'upi_direct.stripe_request_timeout_seconds': 30,
      'upi_direct.approve_error_retries': 4,
      'upi_direct.require_promo': true,
    }
  })

  async function openModal() {
    const wrapper = mount(SettingsAccordion, { global: { plugins: [pinia] } })
    await wrapper.find('.settings-accordion__toggle').trigger('click')
    await flush()
    return wrapper
  }

  it('renders A/B proxy pools', async () => {
    await openModal()
    const checkout = document.querySelector<HTMLTextAreaElement>(
      '#setting-upi-direct-proxy-checkout textarea',
    )
    const promotion = document.querySelector<HTMLTextAreaElement>(
      '#setting-upi-direct-proxy-promotion textarea',
    )
    const approveRetries = document.querySelector(
      '#setting-upi-direct-approve-error-retries',
    )
    expect(checkout).not.toBeNull()
    expect(promotion).not.toBeNull()
    expect(approveRetries).not.toBeNull()
    expect(checkout?.value).toBe('socks5://a:b@h:1')
    expect(promotion?.value).toBe('socks5://c:d@h:2')
    const body = document.body.textContent || ''
    expect(body).toContain('UPI direct')
    expect(body).toContain('Approve error calls')
  })

  it('persists proxy A as list_str', async () => {
    const bulkSpy = vi.spyOn(settingsStore, 'bulkUpdate').mockResolvedValue(true)
    await openModal()
    const checkout = document.querySelector<HTMLTextAreaElement>(
      '#setting-upi-direct-proxy-checkout textarea',
    )
    checkout!.value = 'socks5://x:y@h:9\nsocks5://x:y@h:10'
    checkout!.dispatchEvent(new Event('input', { bubbles: true }))
    await flush()

    const saveButton = Array.from(
      document.querySelectorAll<HTMLButtonElement>('button'),
    ).find((b) => b.textContent?.trim() === 'Save all')
    saveButton!.click()
    await flush()

    const payload = bulkSpy.mock.calls[0][0] as Record<string, unknown>
    expect(payload['upi_direct.proxy_checkout']).toEqual([
      'socks5://x:y@h:9',
      'socks5://x:y@h:10',
    ])
  })
})
