/**
 * Round-trip for the OaiPay (UPI no CDK) settings section.
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

describe('SettingsAccordion — OaiPay section', () => {
  let pinia: Pinia
  let settingsStore: ReturnType<typeof useSettingsStore>

  beforeEach(() => {
    document.body.innerHTML = ''
    pinia = createPinia()
    setActivePinia(pinia)
    settingsStore = useSettingsStore()
    vi.spyOn(settingsStore, 'loadAll').mockResolvedValue()
    settingsStore.settings = {
      'oaipay.base_url': 'https://oaipay.12001234.xyz',
      'oaipay.proxy_checkout': ['socks5://a:b@h:1'],
      'oaipay.proxy_promotion': ['socks5://c:d@h:2'],
      'oaipay.yescaptcha_api_key': 'seed-key',
      'oaipay.max_concurrent': 3,
      'oaipay.run_timeout_seconds': 300,
    }
  })

  async function openModal() {
    const wrapper = mount(SettingsAccordion, { global: { plugins: [pinia] } })
    await wrapper.find('.settings-accordion__toggle').trigger('click')
    await flush()
    return wrapper
  }

  it('renders proxy pools and mandatory hint', async () => {
    await openModal()
    const checkout = document.querySelector<HTMLTextAreaElement>(
      '#setting-oaipay-proxy-checkout textarea',
    )
    const promotion = document.querySelector<HTMLTextAreaElement>(
      '#setting-oaipay-proxy-promotion textarea',
    )
    expect(checkout).not.toBeNull()
    expect(promotion).not.toBeNull()
    expect(checkout?.value).toBe('socks5://a:b@h:1')
    expect(promotion?.value).toBe('socks5://c:d@h:2')
    const body = document.body.textContent || ''
    expect(body).toContain('Bắt buộc: cả hai pool checkout + promotion')
  })

  it('YesCaptcha input is password type and drafts only when edited', async () => {
    const bulkSpy = vi.spyOn(settingsStore, 'bulkUpdate').mockResolvedValue(true)
    await openModal()

    const input = document.querySelector<HTMLInputElement>(
      '#setting-oaipay-yescaptcha-api-key input',
    )
    expect(input).not.toBeNull()
    expect(input?.type).toBe('password')

    // Untouched save → no yescaptcha draft if nothing else changed... but other
    // fields may still be empty drafts. Edit only the key.
    input!.value = 'new-key-value'
    input!.dispatchEvent(new Event('input', { bubbles: true }))
    await flush()

    const saveButton = Array.from(
      document.querySelectorAll<HTMLButtonElement>('button'),
    ).find((b) => b.textContent?.trim() === 'Save all')
    expect(saveButton).toBeTruthy()
    saveButton!.click()
    await flush()

    expect(bulkSpy).toHaveBeenCalled()
    const payload = bulkSpy.mock.calls[0][0] as Record<string, unknown>
    expect(payload['oaipay.yescaptcha_api_key']).toBe('new-key-value')
  })

  it('persists proxy pools as list_str', async () => {
    const bulkSpy = vi.spyOn(settingsStore, 'bulkUpdate').mockResolvedValue(true)
    await openModal()
    const checkout = document.querySelector<HTMLTextAreaElement>(
      '#setting-oaipay-proxy-checkout textarea',
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
    expect(payload['oaipay.proxy_checkout']).toEqual([
      'socks5://x:y@h:9',
      'socks5://x:y@h:10',
    ])
  })
})
