/**
 * Payment-method segmented-control behaviour for `JobInputPanel`.
 *
 * Validates the contract that the panel threads the operator-selected
 * payment method into `jobsStore.submitBatch(lines, paymentMethod, opts)`:
 *   1. Default selection is `ideal`.
 *   2. Selecting a payment-method button reaches the store as `paymentMethod`.
 *   3. The selected method survives a remount/F5 through localStorage.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia, type Pinia } from 'pinia'
import { NInput } from 'naive-ui'

import JobInputPanel from '../JobInputPanel.vue'
import { useJobsStore } from '../../composables/useJobsStore'
import { useSettingsStore } from '../../composables/useSettingsStore'

vi.mock('naive-ui', async (importOriginal) => {
  const actual = await importOriginal<typeof import('naive-ui')>()
  return {
    ...actual,
    useMessage: () => ({
      success: vi.fn(),
      info: vi.fn(),
      warning: vi.fn(),
      error: vi.fn(),
    }),
  }
})

const SAMPLE_LINE = 'user@icloud.com|password|TOTPSECRET'

describe('JobInputPanel - payment method segmented control', () => {
  let pinia: Pinia
  let jobsStore: ReturnType<typeof useJobsStore>

  beforeEach(() => {
    window.localStorage.clear()
    pinia = createPinia()
    setActivePinia(pinia)
    jobsStore = useJobsStore()
    vi.spyOn(jobsStore, 'submitBatch').mockResolvedValue({
      created: [],
      skipped: [],
    })
    // Stub draft autosave so the debounced watch never touches the network.
    vi.spyOn(useSettingsStore(), 'updateKey').mockResolvedValue(true)
  })

  async function mountWithLine() {
    const wrapper = mount(JobInputPanel, { global: { plugins: [pinia] } })
    // Populate the textarea so the Run button is enabled (lineCount > 0).
    wrapper.findComponent(NInput).vm.$emit('update:value', SAMPLE_LINE)
    await wrapper.vm.$nextTick()
    return wrapper
  }

  async function selectPaymentMethod(wrapper: VueWrapper, label: string) {
    const button = wrapper
      .findAll('.actions__method-button')
      .find((candidate) => candidate.text().trim() === label)
    expect(button, `payment method button "${label}"`).toBeTruthy()
    await button!.trigger('click')
    await wrapper.vm.$nextTick()
  }

  it('submits with ideal by default', async () => {
    const wrapper = await mountWithLine()

    await wrapper.find('.actions__run').trigger('click')

    expect(jobsStore.submitBatch).toHaveBeenCalledWith([SAMPLE_LINE], 'ideal', {
      start: true,
    })
  })

  it('submits with upi after selecting UPI', async () => {
    const wrapper = await mountWithLine()

    await selectPaymentMethod(wrapper, 'UPI')
    await wrapper.find('.actions__run').trigger('click')

    expect(jobsStore.submitBatch).toHaveBeenLastCalledWith([SAMPLE_LINE], 'upi', {
      start: true,
    })
  })

  it('submits with upi_nocdk after selecting UPI no CDK', async () => {
    const wrapper = await mountWithLine()

    await selectPaymentMethod(wrapper, 'UPI no CDK')
    await wrapper.find('.actions__run').trigger('click')

    expect(jobsStore.submitBatch).toHaveBeenLastCalledWith(
      [SAMPLE_LINE],
      'upi_nocdk',
      { start: true },
    )
  })

  it('submits with upi_direct after selecting UPI direct', async () => {
    const wrapper = await mountWithLine()

    await selectPaymentMethod(wrapper, 'UPI direct')
    await wrapper.find('.actions__run').trigger('click')

    expect(jobsStore.submitBatch).toHaveBeenLastCalledWith(
      [SAMPLE_LINE],
      'upi_direct',
      { start: true },
    )
  })

  it('submits with gcash_direct after selecting GCash direct', async () => {
    const wrapper = await mountWithLine()

    await selectPaymentMethod(wrapper, 'GCash direct')
    await wrapper.find('.actions__run').trigger('click')

    expect(jobsStore.submitBatch).toHaveBeenLastCalledWith(
      [SAMPLE_LINE],
      'gcash_direct',
      { start: true },
    )
  })

  it('keeps the selected method after remounting', async () => {
    const firstWrapper = await mountWithLine()

    await selectPaymentMethod(firstWrapper, 'UPI direct')
    firstWrapper.unmount()

    const secondWrapper = await mountWithLine()
    await secondWrapper.find('.actions__run').trigger('click')

    expect(jobsStore.submitBatch).toHaveBeenLastCalledWith(
      [SAMPLE_LINE],
      'upi_direct',
      { start: true },
    )
  })
})
