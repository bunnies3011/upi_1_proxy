/**
 * Free accounts panel + freeJobs store partition.
 *
 * freeJobs: every visible non-Plus job by plan alone (no status gate).
 * Panel: list free rows, Copy-all / Download raw account_line only,
 * in-flight warning when pending/running remain, empty state when none.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia, type Pinia } from 'pinia'
import { nextTick } from 'vue'

import FreeOutputPanel from '../FreeOutputPanel.vue'
import {
  useJobsStore,
  type JobStatus,
  type JobViewModel,
  type PlanState,
} from '../../composables/useJobsStore'

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

function makeJob(
  overrides: Partial<JobViewModel> & {
    job_id: string
    status?: JobStatus
  },
): JobViewModel {
  return {
    payment_method: 'ideal',
    account_masked: `${overrides.job_id.slice(0, 3)}***@x.com`,
    status: 'pending',
    updated_at: 1_700_000_000,
    order: 0,
    plan: null,
    ...overrides,
  }
}

function seedJobs(
  store: ReturnType<typeof useJobsStore>,
  jobs: JobViewModel[],
): void {
  const map = new Map<string, JobViewModel>()
  for (const job of jobs) map.set(job.job_id, job)
  store.jobs = map
}

function setPlanState(
  store: ReturnType<typeof useJobsStore>,
  jobId: string,
  state: PlanState,
): void {
  const next = new Map(store.planStates)
  next.set(jobId, state)
  store.planStates = next
}

/** Plan-only Plus (mirrors one store bucket predicate). */
function isPlusByPlan(
  job: JobViewModel,
  planStates: Map<string, PlanState>,
): boolean {
  if (job.plan === 'plus') return true
  const state = planStates.get(job.job_id)
  if (state && !state.loading && state.plan === 'plus') return true
  if (job.error_code === 'oaipay_already_paid') return true
  const msg = (job.error_message || '').toLowerCase()
  return msg.includes('already paid') || msg.includes('user is already paid')
}

function isNoFreeOffer(job: JobViewModel): boolean {
  if (job.error_code === 'no_free_offer') return true
  if (job.error_code !== 'login_failed') return false
  const msg = (job.error_message || '').toLowerCase()
  return (
    msg.includes('account_deleted_or_deactivated') ||
    msg.includes('deleted or deactivated') ||
    msg.includes('has been deleted') ||
    msg.includes('deactivated') ||
    msg.includes('do not have an account')
  )
}

describe('freeJobs store partition', () => {
  let pinia: Pinia
  let store: ReturnType<typeof useJobsStore>

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    store = useJobsStore()
  })

  it('includes free, null, unknown, error, pending, and running non-Plus jobs', () => {
    seedJobs(store, [
      makeJob({
        job_id: 'plus-ready',
        status: 'qr_ready',
        plan: 'plus',
        order: 1,
        account_line: 'plus@x.com|p|t',
      }),
      makeJob({
        job_id: 'free-ready',
        status: 'qr_ready',
        plan: 'free',
        order: 2,
        account_line: 'free@x.com|p|t',
      }),
      makeJob({
        job_id: 'null-plan',
        status: 'qr_ready',
        plan: null,
        order: 3,
        account_line: 'null@x.com|p|t',
      }),
      makeJob({
        job_id: 'err-job',
        status: 'error',
        plan: null,
        order: 4,
        account_line: 'err@x.com|p|t',
      }),
      makeJob({
        job_id: 'pending-job',
        status: 'pending',
        plan: null,
        order: 5,
        account_line: 'pend@x.com|p|t',
      }),
      makeJob({
        job_id: 'running-job',
        status: 'running',
        plan: null,
        order: 6,
        account_line: 'run@x.com|p|t',
      }),
    ])

    const freeIds = store.freeJobs.map((j) => j.job_id)
    expect(freeIds).toEqual([
      'free-ready',
      'null-plan',
      'err-job',
      'pending-job',
      'running-job',
    ])
    expect(freeIds).not.toContain('plus-ready')
  })

  it('excludes persisted plan-plus jobs that are not yet qr_ready', () => {
    seedJobs(store, [
      makeJob({
        job_id: 'plus-pending',
        status: 'pending',
        plan: 'plus',
        order: 1,
        account_line: 'pluspend@x.com|p|t',
      }),
      makeJob({
        job_id: 'plus-running',
        status: 'running',
        plan: 'plus',
        order: 2,
        account_line: 'plusrun@x.com|p|t',
      }),
      makeJob({
        job_id: 'plain-free',
        status: 'error',
        plan: 'free',
        order: 3,
        account_line: 'plain@x.com|p|t',
      }),
    ])

    const freeIds = store.freeJobs.map((j) => j.job_id)
    expect(freeIds).toEqual(['plain-free'])
    expect(freeIds).not.toContain('plus-pending')
    expect(freeIds).not.toContain('plus-running')
  })

  it('excludes already-paid checkout errors from Free export', () => {
    seedJobs(store, [
      makeJob({
        job_id: 'already-paid',
        status: 'error',
        plan: null,
        order: 1,
        account_line: 'paid@x.com|p|t',
        error_code: 'oaipay_already_paid',
        error_message: 'User is already paid (detail=User is already paid)',
      }),
      makeJob({
        job_id: 'legacy-paid-msg',
        status: 'error',
        plan: null,
        order: 2,
        account_line: 'legacy@x.com|p|t',
        error_code: 'oaipay_run_failed',
        error_message: 'OaiPay run failed (detail=User is already paid)',
      }),
      makeJob({
        job_id: 'real-error',
        status: 'error',
        plan: null,
        order: 3,
        account_line: 'fail@x.com|p|t',
        error_code: 'oaipay_stream_error',
        error_message: 'stream truncated',
      }),
    ])

    const freeIds = store.freeJobs.map((j) => j.job_id)
    expect(freeIds).toEqual(['real-error'])
    expect(freeIds).not.toContain('already-paid')
    expect(freeIds).not.toContain('legacy-paid-msg')
  })

  it('moves permanent rejected jobs into the no-free excluded bucket', () => {
    seedJobs(store, [
      makeJob({
        job_id: 'no-free',
        status: 'error',
        plan: null,
        order: 1,
        account_line: 'paid-amount@x.com|p|t',
        error_code: 'no_free_offer',
        error_message: 'Free offer required but amount=169407',
      }),
      makeJob({
        job_id: 'deactivated',
        status: 'error',
        plan: null,
        order: 2,
        account_line: 'dead@x.com|p|t',
        error_code: 'login_failed',
        error_message:
          'Login failed: reason=invalid_credential detail=account_deleted_or_deactivated',
      }),
      makeJob({
        job_id: 'real-error',
        status: 'error',
        plan: null,
        order: 3,
        account_line: 'retry-me@x.com|p|t',
        error_code: 'checkout_failed',
      }),
    ])

    expect(store.freeJobs.map((j) => j.job_id)).toEqual(['real-error'])
    expect(store.noFreeOfferJobs.map((j) => j.job_id)).toEqual([
      'no-free',
      'deactivated',
    ])
  })

  it('excludes jobs marked plus only via planStates transient', () => {
    seedJobs(store, [
      makeJob({
        job_id: 'transient-plus',
        status: 'qr_ready',
        plan: null,
        order: 1,
        account_line: 'tp@x.com|p|t',
      }),
      makeJob({
        job_id: 'still-free',
        status: 'qr_ready',
        plan: null,
        order: 2,
        account_line: 'sf@x.com|p|t',
      }),
    ])
    setPlanState(store, 'transient-plus', {
      loading: false,
      plan: 'plus',
    })
    setPlanState(store, 'still-free', {
      loading: false,
      plan: 'free',
    })

    const freeIds = store.freeJobs.map((j) => j.job_id)
    expect(freeIds).toEqual(['still-free'])
  })

  it('keeps Plus, Free, and no-free-offer buckets disjoint and covering visible jobs', () => {
    seedJobs(store, [
      makeJob({
        job_id: 'a-plus',
        status: 'qr_ready',
        plan: 'plus',
        order: 1,
      }),
      makeJob({
        job_id: 'b-plus-not-ready',
        status: 'pending',
        plan: 'plus',
        order: 2,
      }),
      makeJob({
        job_id: 'c-free',
        status: 'error',
        plan: 'free',
        order: 3,
      }),
      makeJob({
        job_id: 'd-null',
        status: 'running',
        plan: null,
        order: 4,
      }),
      makeJob({
        job_id: 'e-transient',
        status: 'qr_ready',
        plan: null,
        order: 5,
      }),
      makeJob({
        job_id: 'f-no-free',
        status: 'error',
        plan: null,
        order: 6,
        error_code: 'no_free_offer',
      }),
    ])
    setPlanState(store, 'e-transient', { loading: false, plan: 'plus' })

    const visible = store.visibleJobs
    const freeIds = new Set(store.freeJobs.map((j) => j.job_id))
    const noFreeIds = new Set(store.noFreeOfferJobs.map((j) => j.job_id))
    const plusByPlanIds = new Set(
      visible.filter((j) => isPlusByPlan(j, store.planStates)).map((j) => j.job_id),
    )

    // Disjoint
    for (const id of freeIds) {
      expect(plusByPlanIds.has(id)).toBe(false)
      expect(noFreeIds.has(id)).toBe(false)
    }
    for (const id of noFreeIds) {
      expect(plusByPlanIds.has(id)).toBe(false)
    }
    // Cover all visible
    for (const job of visible) {
      const inFree = freeIds.has(job.job_id)
      const inPlus = plusByPlanIds.has(job.job_id)
      const inNoFree = noFreeIds.has(job.job_id)
      expect(inFree || inPlus || inNoFree).toBe(true)
      expect(inFree && inPlus).toBe(false)
      expect(inFree && inNoFree).toBe(false)
      expect(inPlus && inNoFree).toBe(false)
      expect(inNoFree).toBe(isNoFreeOffer(job))
    }
    expect(freeIds.size + plusByPlanIds.size + noFreeIds.size).toBe(
      visible.length,
    )
  })

  it('does not put status-gated plusJobs complement into freeJobs incorrectly', () => {
    // plusJobs requires qr_ready; plan-plus + pending must still leave Free.
    seedJobs(store, [
      makeJob({
        job_id: 'plus-pending',
        status: 'pending',
        plan: 'plus',
        order: 1,
        account_line: 'x@x.com|p|t',
      }),
    ])
    expect(store.plusJobs.map((j) => j.job_id)).toEqual([])
    expect(store.freeJobs.map((j) => j.job_id)).toEqual([])
  })
})

describe('FreeOutputPanel', () => {
  let pinia: Pinia
  let store: ReturnType<typeof useJobsStore>
  let writeText: ReturnType<typeof vi.fn>
  let createObjectURL: ReturnType<typeof vi.fn>
  let revokeObjectURL: ReturnType<typeof vi.fn>

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    store = useJobsStore()

    writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    })

    createObjectURL = vi.fn().mockReturnValue('blob:mock-url')
    revokeObjectURL = vi.fn()
    vi.stubGlobal('URL', {
      ...URL,
      createObjectURL,
      revokeObjectURL,
    })
  })

  function mountPanel() {
    return mount(FreeOutputPanel, {
      global: { plugins: [pinia] },
      props: { selectedJobId: null },
    })
  }

  it('renders one row per free account and empty state when none', async () => {
    seedJobs(store, [
      makeJob({
        job_id: 'plus-1',
        status: 'qr_ready',
        plan: 'plus',
        order: 1,
        account_line: 'plus@x.com|p|t',
      }),
      makeJob({
        job_id: 'free-1',
        status: 'error',
        plan: 'free',
        order: 2,
        account_line: 'free@x.com|p|t',
      }),
    ])

    const wrapper = mountPanel()
    await nextTick()

    const creds = wrapper.findAll('.row__credential').map((n) => n.text())
    expect(creds).toEqual(['free@x.com|p|t'])
    expect(wrapper.find('[data-testid="free-empty"]').exists()).toBe(false)

    seedJobs(store, [
      makeJob({
        job_id: 'plus-1',
        status: 'qr_ready',
        plan: 'plus',
        order: 1,
        account_line: 'plus@x.com|p|t',
      }),
    ])
    await nextTick()
    expect(wrapper.find('[data-testid="free-empty"]').exists()).toBe(true)
    expect(wrapper.findAll('.row').length).toBe(0)

    wrapper.unmount()
  })

  it('Copy-all writes only raw account_line values joined by newlines', async () => {
    seedJobs(store, [
      makeJob({
        job_id: 'raw-1',
        status: 'error',
        plan: 'free',
        order: 1,
        account_line: 'a@x.com|pa|ta',
      }),
      makeJob({
        job_id: 'masked-1',
        status: 'error',
        plan: 'free',
        order: 2,
        // no account_line → masked only
        account_masked: 'b***@x.com',
      }),
      makeJob({
        job_id: 'raw-2',
        status: 'stopped',
        plan: null,
        order: 3,
        account_line: 'c@x.com|pc|tc',
      }),
    ])

    const wrapper = mountPanel()
    await nextTick()

    await wrapper.get('[data-testid="free-copy-all"]').trigger('click')
    expect(writeText).toHaveBeenCalledWith('a@x.com|pa|ta\nc@x.com|pc|tc')

    wrapper.unmount()
  })

  it('Download builds a text blob of the same raw lines', async () => {
    seedJobs(store, [
      makeJob({
        job_id: 'raw-1',
        status: 'error',
        plan: 'free',
        order: 1,
        account_line: 'a@x.com|pa|ta',
      }),
      makeJob({
        job_id: 'masked-1',
        status: 'error',
        plan: 'free',
        order: 2,
        account_masked: 'b***@x.com',
      }),
    ])

    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => undefined)

    const wrapper = mountPanel()
    await nextTick()

    await wrapper.get('[data-testid="free-download"]').trigger('click')

    expect(createObjectURL).toHaveBeenCalledTimes(1)
    const blob = createObjectURL.mock.calls[0][0] as Blob
    expect(blob).toBeInstanceOf(Blob)
    expect(blob.type).toContain('text/plain')
    // jsdom Blob may lack `.text()` — read via FileReader.
    const text = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result ?? ''))
      reader.onerror = () => reject(reader.error)
      reader.readAsText(blob)
    })
    expect(text).toBe('a@x.com|pa|ta\n')
    expect(clickSpy).toHaveBeenCalled()

    clickSpy.mockRestore()
    wrapper.unmount()
  })

  it('shows in-flight banner with live count and keeps export enabled', async () => {
    seedJobs(store, [
      makeJob({
        job_id: 'p1',
        status: 'pending',
        plan: null,
        order: 1,
        account_line: 'p1@x.com|p|t',
      }),
      makeJob({
        job_id: 'r1',
        status: 'running',
        plan: null,
        order: 2,
        account_line: 'r1@x.com|p|t',
      }),
      makeJob({
        job_id: 'done',
        status: 'error',
        plan: 'free',
        order: 3,
        account_line: 'done@x.com|p|t',
      }),
    ])

    const wrapper = mountPanel()
    await nextTick()

    const banner = wrapper.get('[data-testid="free-inflight-banner"]')
    expect(banner.text()).toMatch(/2 account/)

    const copyBtn = wrapper.get('[data-testid="free-copy-all"]')
    const dlBtn = wrapper.get('[data-testid="free-download"]')
    expect(copyBtn.attributes('disabled')).toBeUndefined()
    expect(dlBtn.attributes('disabled')).toBeUndefined()
    // Naive may use aria-disabled; also check prop on component
    expect(
      (copyBtn.element as HTMLButtonElement).disabled ||
        copyBtn.attributes('aria-disabled') === 'true',
    ).toBe(false)

    // Resolve in-flight → banner hides
    seedJobs(store, [
      makeJob({
        job_id: 'done',
        status: 'error',
        plan: 'free',
        order: 1,
        account_line: 'done@x.com|p|t',
      }),
    ])
    await nextTick()
    expect(wrapper.find('[data-testid="free-inflight-banner"]').exists()).toBe(
      false,
    )

    wrapper.unmount()
  })
})
