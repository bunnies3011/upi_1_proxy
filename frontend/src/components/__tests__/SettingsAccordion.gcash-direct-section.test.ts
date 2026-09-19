/**
 * Round-trip for the GCash direct settings section.
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

describe('SettingsAccordion - GCash direct section', () => {
  let pinia: Pinia
  let settingsStore: ReturnType<typeof useSettingsStore>

  beforeEach(() => {
    document.body.innerHTML = ''
    pinia = createPinia()
    setActivePinia(pinia)
    settingsStore = useSettingsStore()
    vi.spyOn(settingsStore, 'loadAll').mockResolvedValue()
    settingsStore.settings = {
      'gcash_direct.proxy_checkout': ['socks5://a:b@ph:1'],
      'gcash_direct.proxy_promotion': ['socks5://c:d@ph:2'],
      'gcash_direct.max_concurrent': 3,
      'gcash_direct.run_timeout_seconds': 300,
      'gcash_direct.stripe_request_timeout_seconds': 30,
      'gcash_direct.approve_error_retries': 4,
      'gcash_direct.require_promo': true,
      'gcash_direct.browser_qr_enabled': true,
      'gcash_direct.browser_qr_capture_enabled': true,
      'gcash_direct.browser_qr_timeout_seconds': 35,
      'gcash_direct.browser_hold_seconds': 300,
      'gcash_direct.browser_hold_max_active': 5,
      'gcash_direct.browser_headless': true,
      'gcash_direct.browser_use_proxy': false,
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
      '#setting-gcash-direct-proxy-checkout textarea',
    )
    const promotion = document.querySelector<HTMLTextAreaElement>(
      '#setting-gcash-direct-proxy-promotion textarea',
    )
    expect(checkout).not.toBeNull()
    expect(promotion).not.toBeNull()
    expect(checkout?.value).toBe('socks5://a:b@ph:1')
    expect(promotion?.value).toBe('socks5://c:d@ph:2')
    expect(document.body.textContent || '').toContain('GCash direct')
    expect(document.body.textContent || '').toContain('Resolve GCash link in browser session')
    expect(document.body.textContent || '').toContain('Capture QR from #qrcode image')
    expect(document.body.textContent || '').toContain('Keep browser alive after QR')
    expect(document.body.textContent || '').toContain('Max live checkout browsers')
  })

  it('persists proxy A as list_str', async () => {
    const bulkSpy = vi.spyOn(settingsStore, 'bulkUpdate').mockResolvedValue(true)
    await openModal()
    const checkout = document.querySelector<HTMLTextAreaElement>(
      '#setting-gcash-direct-proxy-checkout textarea',
    )
    checkout!.value = 'socks5://x:y@ph:9\nsocks5://x:y@ph:10'
    checkout!.dispatchEvent(new Event('input', { bubbles: true }))
    await flush()

    const saveButton = Array.from(
      document.querySelectorAll<HTMLButtonElement>('button'),
    ).find((b) => b.textContent?.trim() === 'Save all')
    saveButton!.click()
    await flush()

    const payload = bulkSpy.mock.calls[0][0] as Record<string, unknown>
    expect(payload['gcash_direct.proxy_checkout']).toEqual([
      'socks5://x:y@ph:9',
      'socks5://x:y@ph:10',
    ])
  })
})
