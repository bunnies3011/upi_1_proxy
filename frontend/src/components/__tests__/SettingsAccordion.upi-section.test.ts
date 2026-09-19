/**
 * Round-trip for the UPI settings section in `SettingsAccordion`.
 *
 * Verifies the operator-facing `upi.*` keys are:
 *   1. READ from `useSettingsStore` into the form (seeded license codes show
 *      up in the textarea when the modal opens).
 *   2. PERSISTED the same way every other section persists — buffered in the
 *      internal `drafts` and flushed through `settingsStore.bulkUpdate` on
 *      "Save all", using the exact `upi.*` key names.
 *
 * The modal teleports its body to `document.body`, so DOM queries target the
 * document rather than the component wrapper. `loadAll` is stubbed so the
 * `onMounted` hook never touches the network.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia, type Pinia } from 'pinia'

import SettingsAccordion from '../SettingsAccordion.vue'
import { useSettingsStore } from '../../composables/useSettingsStore'

async function flush(): Promise<void> {
  // Two ticks: one for the click-driven reactive update, one for the
  // teleported modal content to render into document.body.
  await Promise.resolve()
  await new Promise((r) => setTimeout(r, 0))
}

describe('SettingsAccordion — UPI section', () => {
  let pinia: Pinia
  let settingsStore: ReturnType<typeof useSettingsStore>

  beforeEach(() => {
    document.body.innerHTML = ''
    pinia = createPinia()
    setActivePinia(pinia)
    settingsStore = useSettingsStore()
    vi.spyOn(settingsStore, 'loadAll').mockResolvedValue()
    settingsStore.settings = {
      'upi.license_codes': ['PK-AAAA1111', 'PK-BBBB2222'],
      'upi.max_concurrent': 3,
      'upi.run_timeout_seconds': 120,
      'upi.eligibility_precheck': false,
    }
  })

  async function openModal() {
    const wrapper = mount(SettingsAccordion, { global: { plugins: [pinia] } })
    await wrapper.find('.settings-accordion__toggle').trigger('click')
    await flush()
    return wrapper
  }

  it('reads seeded license codes from the store into the textarea', async () => {
    await openModal()
    const textarea = document.querySelector<HTMLTextAreaElement>(
      '#setting-upi-license-codes textarea',
    )
    expect(textarea).not.toBeNull()
    expect(textarea?.value).toBe('PK-AAAA1111\nPK-BBBB2222')
  })

  it('persists edited license codes through bulkUpdate with the upi.* key', async () => {
    const bulkSpy = vi
      .spyOn(settingsStore, 'bulkUpdate')
      .mockResolvedValue(true)

    await openModal()

    const textarea = document.querySelector<HTMLTextAreaElement>(
      '#setting-upi-license-codes textarea',
    )
    expect(textarea).not.toBeNull()
    // Drive naive-ui's v-model via a native input event on the textarea.
    textarea!.value = 'PK-CCCC3333'
    textarea!.dispatchEvent(new Event('input', { bubbles: true }))
    await flush()

    const saveButton = Array.from(
      document.querySelectorAll<HTMLButtonElement>('button'),
    ).find((b) => b.textContent?.trim() === 'Save all')
    expect(saveButton).toBeTruthy()
    saveButton!.click()
    await flush()

    expect(bulkSpy).toHaveBeenCalledTimes(1)
    const payload = bulkSpy.mock.calls[0][0] as Record<string, unknown>
    expect(payload['upi.license_codes']).toEqual(['PK-CCCC3333'])
  })
})
