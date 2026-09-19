import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia, type Pinia } from 'pinia'

import SuccessOutputPanel from '../SuccessOutputPanel.vue'
import { useJobsStore } from '../../composables/useJobsStore'
import { useSettingsStore } from '../../composables/useSettingsStore'

vi.mock('naive-ui', async (importOriginal) => {
  const actual = await importOriginal<typeof import('naive-ui')>()
  const { defineComponent, h } = await import('vue')
  return {
    ...actual,
    NVirtualList: defineComponent({
      name: 'NVirtualList',
      props: {
        items: {
          type: Array,
          default: () => [],
        },
      },
      setup(props, { slots }) {
        return () =>
          h(
            'div',
            { class: 'n-virtual-list-test' },
            (props.items as unknown[]).map((item) => slots.default?.({ item })),
          )
      },
    }),
    useMessage: () => ({
      success: vi.fn(),
      info: vi.fn(),
      warning: vi.fn(),
      error: vi.fn(),
    }),
  }
})

describe('SuccessOutputPanel', () => {
  let pinia: Pinia

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
  })

  it('removes Plus accounts from the shared input draft', async () => {
    const jobsStore = useJobsStore()
    const settingsStore = useSettingsStore()
    vi.spyOn(settingsStore, 'updateKey').mockResolvedValue(true)

    const plusLine = 'plus@icloud.com|password|TOTPSECRET'
    const keepLine = 'keep@icloud.com|password2|TOTPSECRET2'

    jobsStore.jobs.set('job-plus', {
      job_id: 'job-plus',
      payment_method: 'upi',
      account_masked: 'plus@icloud.com',
      account_line: plusLine,
      status: 'qr_ready',
      updated_at: 1,
      order: 1,
      plan: 'plus',
    })
    settingsStore.settings['ui.input_draft'] = `${plusLine}\n${keepLine}`

    const wrapper = mount(SuccessOutputPanel, {
      props: { selectedJobId: null },
      global: { plugins: [pinia] },
    })

    await wrapper.find('.success-output__remove-plus').trigger('click')

    expect(settingsStore.updateKey).toHaveBeenCalledWith('ui.input_draft', keepLine)
  })
})
