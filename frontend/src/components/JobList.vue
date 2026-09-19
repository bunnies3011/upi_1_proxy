<!--
  JobList — bảng compact danh sách job (Requirement 12.3, 12.8).

  Row layout (theo mock UI mới):
    [#idx] [STATUS badge] [email masked] [PLAN tag] ... [time] [actions]

  Per-row actions (5 icon buttons):
    1. Rerun — chạy lại account_line (tạo job mới)
    2. Download — tải QR PNG (chỉ khi qr_ready)
    3. Copy link — copy payment_link (chỉ khi qr_ready)
    4. Check plan — verify tài khoản đã lên Plus qua session cache
    5. Delete — xóa hoàn toàn khỏi list (hard delete)

  Root class BẮT BUỘC giữ: `.job-list` (test snapshot query).

  KHÔNG dùng NDataTable — table plain cho control tuyệt đối về UI và
  không bị crash trong WKWebView khi có nhiều rows (kinh nghiệm rust-gpt-reg).
-->
<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  NButton,
  NModal,
  NIcon,
  NTag,
  NText,
  NTooltip,
  NVirtualList,
  NSpin,
  useDialog,
  useMessage,
} from 'naive-ui'

import DashboardCard from './DashboardCard.vue'
import {
  useJobsStore,
  type JobStatus,
  type JobViewModel,
  type PlanState,
  type TelegramNotificationEntry,
} from '../composables/useJobsStore'
import { useLiveQrGateStore } from '../composables/useLiveQrGate'
import { usePushGateStore } from '../composables/usePushGate'
import { useSettingsStore } from '../composables/useSettingsStore'
import {
  IconSend,
  IconCopy,
  IconDownload,
  IconList,
  IconPlay,
  IconRefresh,
  IconRepeat,
  IconQrcode,
  IconSquare,
  IconUserCheck,
  IconX,
} from '../icons'

type FilterKey = 'all' | 'pending' | 'running' | 'qr_ready' | 'error' | 'stopped'

/**
 * Chiều cao ước lượng 1 row (px) — khớp `.row { min-height: 42px }` CSS.
 * `NVirtualList` cần giá trị này để tính viewport ban đầu; `item-resizable`
 * (đặt ở template) cho phép nó tự đo lại chiều cao thực khi row wrap 2
 * dòng trên mobile (`@media max-width: 767px` — status/account/actions
 * xuống dòng riêng, cao hơn 42px cố định).
 *
 * Fix memory/CPU: trước đây `<ul><li v-for="job in filtered">` render
 * TOÀN BỘ job (không giới hạn — backend cho phép giữ tối đa 10.000 job
 * terminal). Với vài nghìn job cùng hiển thị, DOM node (mỗi row có 6
 * NButton + NTag + NTooltip) tích lũy hàng chục nghìn element, gây giật
 * UI / tăng RAM tab trình duyệt dù không phải "leak" theo nghĩa rò rỉ
 * (dọn sạch khi job bị xoá) — chỉ là chi phí DOM tỉ lệ thuận O(N) job
 * đang hiển thị. Virtualize để chi phí DOM luôn O(viewport), giống
 * pattern đã áp dụng cho `LogPanel.vue`.
 */
const ROW_ITEM_SIZE = 42

const jobsStore = useJobsStore()
const settingsStore = useSettingsStore()
const pushGateStore = usePushGateStore()
const liveQrGateStore = useLiveQrGateStore()
const message = useMessage()
const dialog = useDialog()

/**
 * Resume Push_Success_Gate — reset counter về 0, `paused=False`. Backend
 * wake JobManager scheduler ngay khi resume → job Push_Mode đang chờ sẽ
 * được kick vào running. Toast success khi xong; error đã set qua store.
 */
async function handleResumePushGate(): Promise<void> {
  await pushGateStore.resume()
  if (pushGateStore.error) {
    message.error(`Tiếp tục thất bại: ${pushGateStore.error}`)
  } else {
    message.success('Đã tiếp tục — scheduler chạy lại job Push')
  }
}

/**
 * `auto_retry_blocked_max` từ Settings — hiển thị dưới dạng
 * "R{n}/{max}" trên badge. Fallback về 0 (ẩn phần "/max") nếu setting
 * chưa được set — chỉ hiển thị `R{n}` thô.
 */
const autoRetryMax = computed<number>(() => {
  const v = settingsStore.settings['ideal.auto_retry_blocked_max']
  return typeof v === 'number' ? v : 0
})

// -----------------------------------------------------------------------------
// Ticker cho thời gian đếm elapsed — tick mỗi 1s, phóng reactive `nowSeconds`
// để cột thời gian tự re-render. Timer chạy toàn cục cho component, không
// spawn per-row (giữ chi phí O(1) bất kể số job).
// -----------------------------------------------------------------------------

const nowSeconds = ref<number>(Math.floor(Date.now() / 1000))
let tickerId: ReturnType<typeof setInterval> | null = null

const props = defineProps<{
  /**
   * Job đang được chọn từ App state — panel LogPanel/DetailPanel đang
   * hiển thị log/QR của job này. Component thêm class `.row--active` để
   * user thấy rõ đang xem job nào (giải quyết UX confusion "click vào job
   * nhưng không biết đã chọn cái nào").
   */
  selectedJobId?: string | null
}>()

const emit = defineEmits<{
  (e: 'select', jobId: string): void
}>()

const filterKey = ref<FilterKey>('all')

/**
 * Chips filter — key khớp `JobStatus` (hoặc `all`). Đếm số job từng nhóm
 * qua computed để giữ đồng bộ với `visibleJobs`.
 */
const chips: { key: FilterKey; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'pending', label: 'Pending' },
  { key: 'running', label: 'Running' },
  { key: 'qr_ready', label: 'QR ready' },
  { key: 'error', label: 'Error' },
  { key: 'stopped', label: 'Stopped' },
]

// Dẫn xuất từ `jobsStore.jobStats` (single-pass shared). Trước đây iterate
// `visibleJobs` độc lập ở đây → cùng 1 SSE event trigger 3 vòng O(N) (App
// stats + JobList counts + JobList qrReadyCount). Giờ tất cả share 1 pass.
const counts = computed<Record<FilterKey, number>>(() => {
  const s = jobsStore.jobStats
  return {
    all: s.total,
    pending: s.pending,
    running: s.running,
    qr_ready: s.qr_ready,
    error: s.error,
    stopped: s.stopped,
  }
})

/**
 * Set các `job_id` được coi là PLUS — nguồn từ `jobsStore.plusJobs` để
 * dùng chung định nghĩa với SuccessOutputPanel (đã persist `plan==='plus'`
 * HOẶC transient `planStates` in-flight). Bọc trong Set để lookup O(1).
 */
const plusJobIdSet = computed<Set<string>>(() => {
  const s = new Set<string>()
  for (const j of jobsStore.plusJobs) s.add(j.job_id)
  return s
})

/**
 * Ưu tiên nhóm hiển thị (thấp hơn = lên trên):
 *   0 → Plus (đã verify thành công)
 *   1 → qr_ready còn lại (đã tạo QR + thanh toán xong nhưng chưa Plus)
 *   2 → phần còn lại (pending / running / error / stopped)
 *
 * Trong cùng 1 nhóm giữ nguyên thứ tự `order ASC` kế thừa từ
 * `visibleJobs` — Array.prototype.sort là stable (ES2019+), nên chỉ cần
 * sort theo `rank` là đủ, không cần tie-break bằng `order` thủ công.
 */
function successRank(job: JobViewModel): number {
  if (plusJobIdSet.value.has(job.job_id)) return 0
  if (job.status === 'qr_ready') return 1
  return 2
}

const filtered = computed<JobViewModel[]>(() => {
  const base =
    filterKey.value === 'all'
      ? jobsStore.visibleJobs
      : jobsStore.visibleJobs.filter((j) => j.status === filterKey.value)
  // Copy trước khi sort — `visibleJobs` là reactive ref, mutate trực tiếp
  // sẽ tái phát trigger recompute vô hạn. Cột `#idx` KHÔNG bị ảnh hưởng
  // vì `displayIndex` map dựa trực tiếp trên `visibleJobs` (thứ tự tạo
  // cố định) — user vẫn thấy `#idx` ổn định qua mọi filter/status change.
  return [...base].sort((a, b) => successRank(a) - successRank(b))
})

/**
 * Map `job_id → chỉ số hiển thị tuyệt đối` (1..N theo `visibleJobs`, KHÔNG
 * theo `filtered`). Dùng để render cột `#idx` sao cho:
 *   - Job A ở vị trí thứ 5 trong list đầy đủ luôn hiển thị `#5` bất kể
 *     user chọn filter nào (Tất cả / Đang chạy / Có QR / ...).
 *   - Filter tab chỉ ẩn/hiện các row, KHÔNG re-number → user không thấy
 *     job "nhảy" số thứ tự khi đổi filter.
 * `visibleJobs` đã sort theo `order` ASC (backend cấp, cố định vĩnh viễn
 * cho từng job) nên index này ổn định qua mọi status change.
 */
const displayIndex = computed<Map<string, number>>(() => {
  const map = new Map<string, number>()
  jobsStore.visibleJobs.forEach((job, i) => {
    map.set(job.job_id, i + 1)
  })
  return map
})

/**
 * Map JobStatus → NTag type. `qr_ready` = success, `running` = info,
 * `error` = error, `stopped` = warning, `pending` = default.
 */
type TagType = 'default' | 'info' | 'success' | 'warning' | 'error'
function tagType(s: JobStatus): TagType {
  switch (s) {
    case 'pending': return 'default'
    case 'running': return 'info'
    case 'qr_ready': return 'success'
    case 'error': return 'error'
    case 'stopped': return 'warning'
  }
}

function statusLabel(s: JobStatus): string {
  switch (s) {
    case 'pending': return 'PENDING'
    case 'running': return 'RUNNING'
    case 'qr_ready': return 'SUCCESS'
    case 'error': return 'ERROR'
    case 'stopped': return 'STOPPED'
  }
}

/**
 * Hiển thị thời gian chạy JOB (elapsed) — port pattern từ
 * `gpt_signup_hybrid.upi.js fmtDuration`.
 *
 * Semantics theo trạng thái (dùng `job.started_at`/`job.finished_at` do
 * backend cấp — KHÔNG dùng `updated_at` vì nó là mốc broadcast SSE gần
 * nhất, có thể tăng liên tục theo log realtime → hiển thị sai):
 *
 *   - `pending`  → chưa từng chạy: hiển thị `—`.
 *   - `running`  → đếm động `now - started_at` (tick 1s).
 *   - terminal   → ĐÓNG BĂNG `finished_at - started_at`, KHÔNG đếm nữa.
 *     Nếu backend chưa có `finished_at` (DB row cũ trước migration) →
 *     fallback về `updated_at - started_at` để không hiển thị `—`.
 *   - job cũ không có `started_at` (DB row từ backend cũ) → `—`.
 *
 * Format:
 *   - `< 1s`     → `<1s`
 *   - `< 60s`    → `Ns` (VD `12s`)
 *   - `< 60min`  → `Mm Ss` (VD `1m 30s`)
 *   - `>= 60min` → `Hh Mm` (VD `2h 5m`)
 */
const TERMINAL_STATUSES: ReadonlySet<JobStatus> = new Set<JobStatus>([
  'qr_ready',
  'error',
  'stopped',
])

function formatElapsed(job: JobViewModel): string {
  const startedAt = job.started_at
  if (typeof startedAt !== 'number' || !Number.isFinite(startedAt) || startedAt <= 0) {
    return '—'
  }

  let endSeconds: number
  if (TERMINAL_STATUSES.has(job.status)) {
    // Job đã kết thúc → dùng finished_at nếu có, fallback updated_at.
    const finishedAt = job.finished_at
    if (typeof finishedAt === 'number' && Number.isFinite(finishedAt) && finishedAt > 0) {
      endSeconds = finishedAt
    } else if (typeof job.updated_at === 'number' && job.updated_at > 0) {
      endSeconds = job.updated_at
    } else {
      return '—'
    }
  } else if (job.status === 'running') {
    // Đang chạy → tick động tới thời điểm hiện tại.
    endSeconds = nowSeconds.value
  } else {
    // pending — không nên có started_at nhưng nếu có (edge case) thì
    // vẫn hiển thị `—` cho nhất quán.
    return '—'
  }

  const diff = endSeconds - startedAt
  if (diff < 1) return '<1s'
  if (diff < 60) return `${Math.floor(diff)}s`
  const minutes = Math.floor(diff / 60)
  if (minutes < 60) {
    const s = Math.floor(diff % 60)
    return `${minutes}m ${s}s`
  }
  const hours = Math.floor(minutes / 60)
  const m = minutes % 60
  return `${hours}h ${m}m`
}

const STOPPABLE: ReadonlySet<JobStatus> = new Set<JobStatus>(['pending', 'running'])

// ---------------------------------------------------------------------------
// Plan status — nguồn dữ liệu từ store (`jobsStore.planStates`) để dùng
// chung với SuccessOutputPanel. Component tuyệt đối không mutate state
// trực tiếp; mọi thay đổi đi qua action `jobsStore.checkPlan` /
// `jobsStore.checkPlanAll` (yêu cầu tái sử dụng code).
// ---------------------------------------------------------------------------

/**
 * Effective plan cho render badge — MERGE 2 nguồn theo thứ tự ưu tiên:
 *   1. `planStates.loading` → tag "…" (round-trip check-plan chưa xong).
 *   2. `job.plan` từ DB (persisted qua reload — yêu cầu 2026-07).
 *   3. `planStates` transient (khi FE vừa checkPlan xong, SSE chưa
 *      tới → nhánh loading đã cover; hoặc kết quả `unknown` BE
 *      không persist → chỉ nằm ở planStates).
 *   4. Không có → `null` → template `v-if` ẩn tag.
 *
 * Return `PlanState`-shape (`loading` false + `plan` union) hoặc `null`.
 */
type EffectivePlan =
  | { loading: true }
  | { loading: false; plan: 'plus' | 'free' | 'unknown' }
  | null

function isAlreadyPaidError(job: JobViewModel): boolean {
  if (job.error_code === 'oaipay_already_paid') return true
  const msg = (job.error_message || '').toLowerCase()
  return msg.includes('already paid') || msg.includes('user is already paid')
}

function effectivePlan(
  job: JobViewModel,
  state: PlanState | undefined,
): EffectivePlan {
  if (state?.loading) return { loading: true }
  if (job.plan === 'plus' || job.plan === 'free') {
    return { loading: false, plan: job.plan }
  }
  // Checkout already-paid proves Plus without check-plan.
  if (isAlreadyPaidError(job)) {
    return { loading: false, plan: 'plus' }
  }
  if (state && !state.loading) {
    return { loading: false, plan: state.plan }
  }
  return null
}

function planTagType(s: EffectivePlan): TagType {
  if (!s || s.loading) return 'default'
  if (s.plan === 'plus') return 'success'
  if (s.plan === 'free') return 'warning'
  return 'default'
}

function planLabel(s: EffectivePlan): string {
  if (!s) return ''
  if (s.loading) return '…'
  if (s.plan === 'plus') return 'PLUS'
  if (s.plan === 'free') return 'FREE'
  return '?'
}

// ---------------------------------------------------------------------------
// Telegram notification badge helpers (feature "track Telegram per job")
// ---------------------------------------------------------------------------

/**
 * Đếm số lần notify thành công. Fail entry (Telegram API 4xx/5xx hoặc
 * network error) vẫn được lưu vào history để user biết đã cố gắng, nhưng
 * không tính vào count success.
 */
function successCount(entries?: TelegramNotificationEntry[]): number {
  if (!entries) return 0
  return entries.reduce((n, e) => n + (e.success ? 1 : 0), 0)
}

/**
 * Label ngắn cho badge — hiển thị `<success>/<total>`. Nếu total = success
 * hiển thị luôn số success (gọn hơn). Ví dụ: `"1"`, `"1/2"`.
 */
function telegramBadgeLabel(entries?: TelegramNotificationEntry[]): string {
  if (!entries || entries.length === 0) return ''
  const ok = successCount(entries)
  if (ok === entries.length) return String(ok)
  return `${ok}/${entries.length}`
}

/**
 * Màu tag: xanh khi mọi lần đều success, cam khi có ít nhất 1 fail
 * (user thấy warn để check tooltip).
 */
function telegramTagType(entries?: TelegramNotificationEntry[]): TagType {
  if (!entries || entries.length === 0) return 'default'
  return successCount(entries) === entries.length ? 'success' : 'warning'
}

/** Số job qr_ready — dùng để enable/disable nút "Check Plus All". Đọc từ
 * `jobStats` (single-pass shared) thay vì filter+allocate array riêng. */
const qrReadyCount = computed<number>(() => jobsStore.jobStats.qr_ready)

/** Cờ đang chạy bulk check-plan — disable nút để tránh double-click. */
const checkPlanAllRunning = ref<boolean>(false)

/**
 * Auto-reload "Check Plus All": đọc 2 setting từ SettingsStore.
 * - `ui.auto_check_plus_all_enabled` (bool, default false)
 * - `ui.auto_check_plus_all_interval_seconds` (int, default 60, min 5, max 3600)
 *
 * Khi enabled: `setInterval` mỗi N giây tự gọi `handleCheckPlanAll()`. Bảo
 * vệ:
 *   1. Chỉ chạy khi có ít nhất 1 job qr_ready (`qrReadyCount > 0`) — tránh
 *      spam log toast "no accounts".
 *   2. Skip nếu vòng trước còn `checkPlanAllRunning=true` — tránh 2 bulk
 *      chồng nhau khi API chậm.
 *   3. Pause khi tab ẩn (`document.hidden`) — tiết kiệm request + tránh
 *      wake tab background.
 *   4. Restart timer khi user đổi interval qua Settings — watch reactive
 *      2 giá trị và clear/spawn interval.
 */
const autoCheckPlusEnabled = computed<boolean>(
  () => settingsStore.settings['ui.auto_check_plus_all_enabled'] === true,
)
const autoCheckPlusIntervalMs = computed<number>(() => {
  const raw = settingsStore.settings['ui.auto_check_plus_all_interval_seconds']
  const seconds = typeof raw === 'number' && raw >= 5 ? raw : 60
  return seconds * 1000
})

let autoCheckPlusTimerId: ReturnType<typeof setInterval> | null = null

function startAutoCheckPlusTimer(): void {
  stopAutoCheckPlusTimer()
  if (!autoCheckPlusEnabled.value) return
  autoCheckPlusTimerId = setInterval(() => {
    if (typeof document !== 'undefined' && document.hidden) return
    if (checkPlanAllRunning.value) return
    if (qrReadyCount.value === 0) return
    // Fire-and-forget: `handleCheckPlanAll` tự set `checkPlanAllRunning`
    // và toast kết quả. Không await để không block callback interval.
    void handleCheckPlanAll()
  }, autoCheckPlusIntervalMs.value)
}

function stopAutoCheckPlusTimer(): void {
  if (autoCheckPlusTimerId !== null) {
    clearInterval(autoCheckPlusTimerId)
    autoCheckPlusTimerId = null
  }
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

async function handleStop(jobId: string, ev: Event): Promise<void> {
  ev.stopPropagation()
  try {
    await jobsStore.stopJob(jobId)
  } catch {
    // Store đã set error; layout cha hiển thị.
  }
}

/**
 * Start 1 job đang `pending + held=true` (feature Add / Run). Nút ▶ chỉ
 * hiện trên row có `held=true` — nên KHÔNG cần confirm dialog (action
 * reversible: user có thể Stop nếu bấm nhầm).
 */
async function handleStart(jobId: string, ev: Event): Promise<void> {
  ev.stopPropagation()
  try {
    await jobsStore.startJob(jobId)
    message.success('Job started')
  } catch (ex) {
    message.error(
      `Start failed: ${ex instanceof Error ? ex.message : String(ex)}`,
    )
  }
}

/**
 * Xóa job — hard delete, không phục hồi được. Hiện confirm dialog trước
 * khi gọi API để phòng user misclick khi list dài + nút X cạnh nút Copy.
 */
function handleDelete(job: JobViewModel, ev: Event): void {
  ev.stopPropagation()
  dialog.warning({
    title: 'Delete job?',
    content: `Delete job ${job.account_masked}? This cannot be undone (log and QR will be lost).`,
    positiveText: 'Delete',
    negativeText: 'Cancel',
    onPositiveClick: async () => {
      try {
        // Store tự dọn `planStates` cho job đã xoá (xem
        // `deleteJob`/`handleJobNotFound` trong useJobsStore).
        await jobsStore.deleteJob(job.job_id)
        message.success('Job deleted')
      } catch (ex) {
        message.error(`Delete failed: ${ex instanceof Error ? ex.message : String(ex)}`)
      }
    },
  })
}

/**
 * Chạy lại job — reset về pending (dedup theo email). Confirm để tránh
 * user vô ý bấm khi đang xem job qr_ready thành công.
 */
function handleRerun(job: JobViewModel, ev: Event): void {
  ev.stopPropagation()
  const suffix =
    job.status === 'qr_ready'
      ? ' This job currently has a QR — rerunning will remove the old one.'
      : ''
  dialog.info({
    title: 'Rerun job?',
    content: `Rerun account ${job.account_masked}?${suffix}`,
    positiveText: 'Rerun',
    negativeText: 'Cancel',
    onPositiveClick: async () => {
      try {
        const result = await jobsStore.rerunJob(job.job_id)
        if (result.created.length > 0) {
          message.success(`Created new job: ${result.created[0].slice(0, 8)}…`)
        } else if (result.skipped.length > 0) {
          message.warning(`Could not rerun: ${result.skipped[0].reason}`)
        }
      } catch (ex) {
        message.error(`Rerun failed: ${ex instanceof Error ? ex.message : String(ex)}`)
      }
    },
  })
}

/**
 * Check plan cho 1 job — mọi mutation state đã nằm trong `store.checkPlan`.
 * Component chỉ toast thông báo kết quả cho user. Nếu plan === 'plus' →
 * account sẽ tự động xuất hiện trong SuccessOutputPanel (panel bind
 * `plusJobs` computed của store, không cần đẩy thủ công).
 */
async function handleCheckPlan(jobId: string, ev: Event): Promise<void> {
  ev.stopPropagation()
  try {
    const state = await jobsStore.checkPlan(jobId)
    if (state.loading) return // exhaustive guard — không xảy ra sau await.
    if (state.plan === 'plus') {
      message.success('Account upgraded to Plus — added to Successful accounts')
    } else if (state.plan === 'free') {
      message.info('Account is still Free — not counted as successful')
    } else {
      message.warning(`Could not determine plan: ${state.error ?? 'unknown'}`)
    }
  } catch (ex) {
    message.error(`Check plan failed: ${ex instanceof Error ? ex.message : String(ex)}`)
  }
}

/**
 * Bulk check-plan cho tất cả job qr_ready. Uỷ quyền hoàn toàn cho
 * `store.checkPlanAll` — component chỉ điều phối busy state + toast tổng
 * kết. Tránh submit lặp qua cờ `checkPlanAllRunning`.
 */
async function handleCheckPlanAll(): Promise<void> {
  if (checkPlanAllRunning.value) return
  if (qrReadyCount.value === 0) {
    message.info('No successful (qr_ready) accounts to check plan for')
    return
  }
  checkPlanAllRunning.value = true
  try {
    const tally = await jobsStore.checkPlanAll()
    // Format tổng kết ngắn gọn — user thấy ngay còn bao nhiêu account
    // chưa lên Plus mà cần xử lý tiếp. `skipped_plus` cho biết bao
    // nhiêu row đã Plus sẵn (cache DB), bỏ qua để không hammer ChatGPT
    // — hiển thị inline vào chip Plus khi có.
    const plusLabel =
      tally.skipped_plus > 0
        ? `${tally.plus} Plus (${tally.skipped_plus} cached)`
        : `${tally.plus} Plus`
    const parts = [plusLabel, `${tally.free} Free`]
    if (tally.unknown > 0) parts.push(`${tally.unknown} ?`)
    if (tally.errors > 0) parts.push(`${tally.errors} error(s)`)
    const summary = `Checked ${tally.total} account(s): ${parts.join(' · ')}`
    if (tally.plus > 0 && tally.errors === 0 && tally.unknown === 0) {
      message.success(summary)
    } else if (tally.errors > 0) {
      message.warning(summary)
    } else {
      message.info(summary)
    }
  } catch (ex) {
    message.error(`Check Plus All failed: ${ex instanceof Error ? ex.message : String(ex)}`)
  } finally {
    checkPlanAllRunning.value = false
  }
}

/**
 * Copy `payment_link` vào clipboard. Chạy trên Job có `status=qr_ready` và
 * `payment_link != null` (guard render bằng v-if — không cần re-check ở đây).
 */
const qrPreviewOpen = ref<boolean>(false)
const qrPreviewJobId = ref<string | null>(null)
const qrPreviewBlobUrl = ref<string | null>(null)
const qrPreviewError = ref<string | null>(null)
const qrPreviewLoading = ref<boolean>(false)

const qrPreviewJob = computed<JobViewModel | null>(() => {
  const id = qrPreviewJobId.value
  if (!id) return null
  return jobsStore.jobs.get(id) ?? null
})

const qrPreviewLink = computed<string | null>(() => qrPreviewJob.value?.payment_link ?? null)

function hasViewableQr(job: JobViewModel): boolean {
  if (job.status !== 'qr_ready') return false
  if (job.payment_method === 'gcash_direct') return Boolean(job.artifact_path)
  return Boolean(job.artifact_path || job.payment_link)
}

const qrPreviewSourceUrl = computed<string | null>(() => {
  const id = qrPreviewJobId.value
  if (!id) return null
  const job = jobsStore.jobs.get(id)
  if (!job || !hasViewableQr(job)) return null
  return `/api/jobs/${encodeURIComponent(id)}/qr.png`
})

function revokeQrPreviewBlobUrl(): void {
  if (qrPreviewBlobUrl.value !== null) {
    URL.revokeObjectURL(qrPreviewBlobUrl.value)
    qrPreviewBlobUrl.value = null
  }
}

async function loadQrPreviewBlob(sourceUrl: string): Promise<void> {
  const activeJobId = qrPreviewJobId.value
  qrPreviewError.value = null
  qrPreviewLoading.value = true
  try {
    const res = await fetch(sourceUrl, {
      method: 'GET',
      headers: { Accept: 'image/png' },
    })
    if (!res.ok) {
      let errorCode = `HTTP ${res.status}`
      try {
        const body = (await res.clone().json()) as {
          detail?: { error_code?: string; message?: string }
          error_code?: string
          message?: string
        }
        const detail = body.detail
        const code =
          (detail && typeof detail === 'object' && typeof detail.error_code === 'string'
            ? detail.error_code
            : undefined) ??
          (typeof body.error_code === 'string' ? body.error_code : undefined)
        if (code) errorCode = code
      } catch {
        /* not JSON */
      }
      if (!qrPreviewOpen.value || qrPreviewJobId.value !== activeJobId) return
      qrPreviewError.value = errorCode
      return
    }
    if (!qrPreviewOpen.value || qrPreviewJobId.value !== activeJobId) return
    const blob = await res.blob()
    if (!qrPreviewOpen.value || qrPreviewJobId.value !== activeJobId) return
    revokeQrPreviewBlobUrl()
    qrPreviewBlobUrl.value = URL.createObjectURL(blob)
  } catch (ex) {
    if (!qrPreviewOpen.value || qrPreviewJobId.value !== activeJobId) return
    qrPreviewError.value = ex instanceof Error ? ex.message : String(ex)
  } finally {
    if (!qrPreviewOpen.value || qrPreviewJobId.value !== activeJobId) return
    qrPreviewLoading.value = false
  }
}

async function openQrPreview(job: JobViewModel, ev: Event): Promise<void> {
  ev.stopPropagation()
  if (!hasViewableQr(job)) return
  qrPreviewJobId.value = job.job_id
  qrPreviewOpen.value = true
  try {
    await jobsStore.loadDetail(job.job_id)
  } catch {
    /* store.error already set */
  }
}

function closeQrPreview(): void {
  qrPreviewOpen.value = false
  qrPreviewJobId.value = null
  qrPreviewError.value = null
  qrPreviewLoading.value = false
  revokeQrPreviewBlobUrl()
}

watch(qrPreviewOpen, (open) => {
  if (open) return
  qrPreviewJobId.value = null
  qrPreviewError.value = null
  qrPreviewLoading.value = false
  revokeQrPreviewBlobUrl()
})

watch(
  qrPreviewSourceUrl,
  async (newUrl, oldUrl) => {
    if (newUrl === oldUrl) return
    if (newUrl === null) {
      qrPreviewError.value = null
      qrPreviewLoading.value = false
      revokeQrPreviewBlobUrl()
      return
    }
    await loadQrPreviewBlob(newUrl)
  },
  { immediate: true },
)

async function handleCopyLink(link: string | null | undefined, ev: Event): Promise<void> {
  ev.stopPropagation()
  if (!link) return
  if (typeof navigator === 'undefined' || !navigator.clipboard) {
    message.warning('Browser does not support clipboard')
    return
  }
  try {
    await navigator.clipboard.writeText(link)
    message.success('Copied payment link')
  } catch (ex) {
    message.error(`Copy failed: ${ex instanceof Error ? ex.message : String(ex)}`)
  }
}

/**
 * Download QR PNG file — dùng fetch rồi tạo blob URL để kích hoạt
 * `<a download>`, cho phép xử lý lỗi HTTP trước khi lưu file.
 */
async function handleDownloadQr(jobId: string, ev: Event): Promise<void> {
  ev.stopPropagation()
  try {
    const url = `/api/jobs/${encodeURIComponent(jobId)}/qr.png`
    const res = await fetch(url, {
      method: 'GET',
      headers: { Accept: 'image/png' },
    })
    if (!res.ok) {
      message.error(`Failed to load QR (HTTP ${res.status})`)
      return
    }
    const blob = await res.blob()
    const blobUrl = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = blobUrl
    a.download = `${jobId}.png`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(blobUrl)
    message.success('QR downloaded')
  } catch (ex) {
    message.error(`Download failed: ${ex instanceof Error ? ex.message : String(ex)}`)
  }
}

function handleSelect(jobId: string): void {
  emit('select', jobId)
}

async function handleRefresh(): Promise<void> {
  try {
    await jobsStore.loadAll()
  } catch {
    /* store đã set error */
  }
}

/**
 * Ticker chỉ chạy khi thực sự có job running VÀ tab đang hiển thị. Trước
 * fix này, `setInterval` bắn mỗi 1s vô điều kiện → mọi row viewport
 * re-render text elapsed dù không đổi + wake CPU khi tab background. Guard
 * qua watch(jobStats.running) để start/stop ticker + `visibilitychange`
 * để pause khi tab ẩn.
 *
 * loadAll() đã được App.vue.onMounted gọi 1 lần (source of truth). Xóa
 * lời gọi duplicate ở đây để tránh 2 request song song lúc mount.
 */
function startTicker(): void {
  if (tickerId !== null) return
  tickerId = setInterval(() => {
    if (typeof document !== 'undefined' && document.hidden) return
    nowSeconds.value = Math.floor(Date.now() / 1000)
  }, 1000)
}

function stopTicker(): void {
  if (tickerId !== null) {
    clearInterval(tickerId)
    tickerId = null
  }
}

function handleVisibilityChange(): void {
  if (typeof document === 'undefined') return
  if (!document.hidden && jobsStore.jobStats.running > 0) {
    // Tab visible trở lại → refresh `nowSeconds` ngay để user thấy elapsed
    // đúng thời điểm hiện tại (không phải giá trị đóng băng lúc tab ẩn).
    nowSeconds.value = Math.floor(Date.now() / 1000)
  }
}

watch(
  () => jobsStore.jobStats.running,
  (n) => {
    if (n > 0) startTicker()
    else stopTicker()
  },
  { immediate: true },
)

// Restart auto-reload timer khi user đổi enabled/interval qua Settings.
// `immediate: true` để lần load đầu (settings hydrate xong) tự spawn timer
// mà không cần đợi user tương tác. Watch 1 tuple để chỉ 1 lần restart khi
// cả 2 cùng đổi trong 1 tick (VD sau bulkUpdate).
watch(
  () => [autoCheckPlusEnabled.value, autoCheckPlusIntervalMs.value] as const,
  () => startAutoCheckPlusTimer(),
  { immediate: true },
)

onMounted(() => {
  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', handleVisibilityChange)
  }
})

onUnmounted(() => {
  stopTicker()
  stopAutoCheckPlusTimer()
  revokeQrPreviewBlobUrl()
  if (typeof document !== 'undefined') {
    document.removeEventListener('visibilitychange', handleVisibilityChange)
  }
})
</script>

<template>
  <section class="job-list" aria-label="Job list">
    <DashboardCard
      :icon="IconList"
      title="Job list"
      :meta="`${counts.all} job(s)`"
    >
      <template #extra>
        <!--
          Push_Success_Gate resume button — CHỈ hiện khi gate đã paused
          (đủ N success). To và pulse để dễ nhìn ở header (user request).
          Show progress dạng "5/10" cạnh label để thấy ngay tiến độ.
          Nếu gate enabled nhưng CHƯA paused → chỉ hiển thị badge nhỏ
          "Push: 3/10" (không phải button primary) để user biết state.
        -->
        <n-button
          v-if="pushGateStore.shouldShowResumeBanner"
          size="medium"
          type="warning"
          strong
          class="push-gate-resume-btn"
          :loading="pushGateStore.resuming"
          :title="`Scheduler đã tạm dừng vì đã gửi đủ ${pushGateStore.snapshot.threshold} Telegram Push — bấm để tiếp tục`"
          @click="handleResumePushGate"
        >
          <template #icon>
            <n-icon :component="IconPlay" />
          </template>
          Tiếp tục ({{ pushGateStore.snapshot.counter }}/{{ pushGateStore.snapshot.threshold }} Telegram đã gửi)
        </n-button>
        <n-tag
          v-else-if="pushGateStore.shouldShowProgress"
          size="small"
          round
          :bordered="false"
          type="info"
          class="push-gate-progress-tag"
          :title="`Push_Mode success wait — Telegram Push đã gửi ${pushGateStore.snapshot.counter}/${pushGateStore.snapshot.threshold} kể từ lần Tiếp tục gần nhất`"
        >
          <template #icon>
            <n-icon :component="IconSend" />
          </template>
          Telegram: {{ pushGateStore.snapshot.counter }}/{{ pushGateStore.snapshot.threshold }}
        </n-tag>
        <n-tag
          v-if="liveQrGateStore.shouldShowLiveBadge"
          size="small"
          round
          :bordered="false"
          :type="liveQrGateStore.snapshot.blocked ? 'warning' : 'info'"
          class="push-gate-progress-tag"
          :title="`Live QR Gate — ${liveQrGateStore.snapshot.live}/${liveQrGateStore.snapshot.capacity} (per chat max ${liveQrGateStore.snapshot.max_per_chat})`"
        >
          <template #icon>
            <n-icon :component="IconSend" />
          </template>
          Live QR: {{ liveQrGateStore.snapshot.live }}/{{ liveQrGateStore.snapshot.capacity }}
        </n-tag>
        <n-button
          size="tiny"
          quaternary
          type="success"
          :loading="checkPlanAllRunning"
          :disabled="qrReadyCount === 0"
          :title="
            qrReadyCount === 0
              ? 'No qr_ready accounts to check plan for'
              : `Check Plus for ${qrReadyCount} qr_ready account(s)`
          "
          @click="handleCheckPlanAll"
        >
          <template #icon>
            <n-icon :component="IconUserCheck" />
          </template>
          Check Plus All
        </n-button>
        <n-button
          size="tiny"
          quaternary
          :loading="jobsStore.loading"
          @click="handleRefresh"
        >
          <template #icon>
            <n-icon :component="IconRefresh" />
          </template>
          Refresh
        </n-button>
      </template>

      <div class="body">
        <div class="chips">
          <n-button
            v-for="c in chips"
            :key="c.key"
            size="tiny"
            round
            :type="filterKey === c.key ? 'primary' : 'default'"
            :secondary="filterKey === c.key"
            :ghost="filterKey !== c.key"
            @click="filterKey = c.key"
          >
            {{ c.label }}
            <n-text
              :depth="filterKey === c.key ? undefined : 3"
              style="margin-left: 6px; font-variant-numeric: tabular-nums"
            >
              {{ counts[c.key] }}
            </n-text>
          </n-button>
        </div>

        <div class="wrap">
          <NVirtualList
            v-if="filtered.length > 0"
            class="rows"
            :items="filtered"
            :item-size="ROW_ITEM_SIZE"
            item-resizable
            key-field="job_id"
          >
            <template #default="{ item: job }: { item: JobViewModel }">
            <div
              :key="job.job_id"
              class="row"
              :class="[
                `row--${job.status}`,
                { 'row--active': job.job_id === props.selectedJobId },
              ]"
              @click="handleSelect(job.job_id)"
            >
              <div class="col-idx mono">#{{ displayIndex.get(job.job_id) }}</div>
              <div class="col-status">
                <n-tag :type="tagType(job.status)" size="small" round :bordered="false">
                  {{ statusLabel(job.status) }}
                </n-tag>
                <!--
                  Badge Held: chỉ hiện khi `status='pending'` + `held=true`
                  (feature Add / Run tách bạch). Cho user biết job này đã
                  được add nhưng chưa vào queue chạy — bấm nút ▶ trong
                  action row để start. Tag "info" khác với "pending" thường
                  (default) để phân biệt visual.
                -->
                <n-tag
                  v-if="job.status === 'pending' && job.held"
                  size="small"
                  round
                  :bordered="false"
                  type="info"
                  title="Held — press ▶ to start this job"
                  class="held-badge"
                >
                  Held
                </n-tag>
                <!--
                  Badge auto-retry: hiển thị khi backend đã schedule ít
                  nhất 1 lần retry (`retry_count > 0`). Chỉ show max hiện
                  tại nếu đọc được từ settings; fallback show `R{n}` thô.
                  Semantic: "Job này đã tự chạy lại N lần vì lỗi transient
                  (blocked/anti-fraud/timing)".
                -->
                <n-tag
                  v-if="(job.retry_count ?? 0) > 0"
                  size="small"
                  round
                  :bordered="false"
                  type="warning"
                  :title="`Auto-retried ${job.retry_count} time(s)${autoRetryMax ? ` / ${autoRetryMax}` : ''}`"
                  class="retry-badge"
                >
                  R{{ job.retry_count }}<span v-if="autoRetryMax">/{{ autoRetryMax }}</span>
                </n-tag>
                <!--
                  Badge Telegram: hiển thị khi đã gửi ít nhất 1 notify.
                  Icon paper-plane + số lần gửi thành công / tổng. Tooltip
                  liệt kê 3 chat gần nhất (chat_label hoặc chat_id). Bấm
                  vào row rồi mở JobDetailPanel để xem đầy đủ history.
                -->
                <n-tooltip
                  v-if="(job.telegram_notifications?.length ?? 0) > 0"
                  placement="top"
                  :show-arrow="true"
                  trigger="hover"
                >
                  <template #trigger>
                    <n-tag
                      size="small"
                      round
                      :bordered="false"
                      :type="telegramTagType(job.telegram_notifications)"
                      class="telegram-badge"
                    >
                      <template #icon>
                        <n-icon :component="IconSend" />
                      </template>
                      {{ telegramBadgeLabel(job.telegram_notifications) }}
                    </n-tag>
                  </template>
                  <div class="telegram-tooltip">
                    <div class="telegram-tooltip__title">
                      Telegram sent
                      ({{ successCount(job.telegram_notifications) }}/{{
                        job.telegram_notifications!.length
                      }})
                    </div>
                    <ul class="telegram-tooltip__list">
                      <li
                        v-for="(entry, idx) in job.telegram_notifications!.slice(-5)"
                        :key="`${entry.chat_id}-${entry.sent_at}-${idx}`"
                        class="telegram-tooltip__item"
                      >
                        <span :class="entry.success ? 'ok' : 'fail'">
                          {{ entry.success ? '✓' : '✗' }}
                        </span>
                        {{ entry.chat_label || entry.chat_id }}
                        <span class="mono muted">·</span>
                        <span class="mono muted">
                          {{ new Date(entry.sent_at * 1000).toLocaleTimeString() }}
                        </span>
                      </li>
                    </ul>
                    <div
                      v-if="job.telegram_notifications!.length > 5"
                      class="telegram-tooltip__more"
                    >
                      … +{{ job.telegram_notifications!.length - 5 }} earlier
                    </div>
                  </div>
                </n-tooltip>
              </div>
              <div class="col-account mono" :title="job.account_masked">
                {{ job.account_masked }}
              </div>
              <div class="col-plan">
                <n-tag
                  v-if="effectivePlan(job, jobsStore.planStates.get(job.job_id))"
                  :type="planTagType(effectivePlan(job, jobsStore.planStates.get(job.job_id)))"
                  size="small"
                  round
                  :bordered="false"
                  :title="
                    job.plan === 'plus'
                      ? 'Persisted from DB — Successful account'
                      : job.plan === 'free'
                      ? 'Persisted from DB — still Free'
                      : 'Session-only result (not yet persisted)'
                  "
                >
                  {{ planLabel(effectivePlan(job, jobsStore.planStates.get(job.job_id))) }}
                </n-tag>
              </div>
              <div
                class="col-time mono"
                :title="
                  job.started_at
                    ? `Started: ${new Date(job.started_at * 1000).toLocaleString()}` +
                      (job.finished_at
                        ? ` · Finished: ${new Date(job.finished_at * 1000).toLocaleString()}`
                        : '')
                    : 'Not started yet'
                "
              >
                {{ formatElapsed(job) }}
              </div>
              <!--
                Actions column — LUÔN render đủ 6 nút trên mọi row để cột
                không co giãn khi status thay đổi. Nút không áp dụng với
                status hiện tại được disable (opacity mờ + cursor:
                not-allowed) thay vì ẩn — user luôn thấy đủ hành động,
                không phải đoán vì sao có row thiếu nút.

                Logic disable:
                - Rerun / Check plan / Delete: luôn enable.
                - Download QR: chỉ qr_ready.
                - Copy link: chỉ qr_ready + có payment_link.
                - Stop: chỉ pending/running.
              -->
              <div class="col-actions" @click.stop>
                <!-- Nút Start ▶ chỉ hiện khi job `pending + held=true`
                     (feature Add / Run tách bạch). Primary type để nổi
                     bật — user cần thấy rõ CTA. Các nút khác giữ nguyên
                     thứ tự trong action row (Rerun / Download / …). -->
                <n-button
                  v-if="job.status === 'pending' && job.held"
                  size="tiny"
                  type="primary"
                  circle
                  title="Start held job"
                  @click="handleStart(job.job_id, $event)"
                >
                  <template #icon>
                    <n-icon :component="IconPlay" />
                  </template>
                </n-button>
                <n-button
                  size="tiny"
                  quaternary
                  circle
                  title="Rerun"
                  @click="handleRerun(job, $event)"
                >
                  <template #icon>
                    <n-icon :component="IconRepeat" />
                  </template>
                </n-button>
                <n-button
                  size="tiny"
                  quaternary
                  circle
                  :disabled="!hasViewableQr(job)"
                  :title="hasViewableQr(job) ? 'Show QR and payment link' : 'No QR yet'"
                  @click="openQrPreview(job, $event)"
                >
                  <template #icon>
                    <n-icon :component="IconQrcode" />
                  </template>
                </n-button>
                <n-button
                  size="tiny"
                  quaternary
                  circle
                  :disabled="!hasViewableQr(job)"
                  :title="
                    hasViewableQr(job)
                      ? 'Download QR'
                      : 'No QR file'
                  "
                  @click="handleDownloadQr(job.job_id, $event)"
                >
                  <template #icon>
                    <n-icon :component="IconDownload" />
                  </template>
                </n-button>
                <n-button
                  size="tiny"
                  quaternary
                  circle
                  :disabled="job.status !== 'qr_ready' || !job.payment_link"
                  :title="
                    job.status === 'qr_ready' && job.payment_link
                      ? 'Copy payment link'
                      : 'No payment link yet'
                  "
                  @click="handleCopyLink(job.payment_link, $event)"
                >
                  <template #icon>
                    <n-icon :component="IconCopy" />
                  </template>
                </n-button>
                <n-button
                  size="tiny"
                  quaternary
                  circle
                  :loading="jobsStore.planStates.get(job.job_id)?.loading === true"
                  title="Check if account has upgraded to Plus"
                  @click="handleCheckPlan(job.job_id, $event)"
                >
                  <template #icon>
                    <n-icon :component="IconUserCheck" />
                  </template>
                </n-button>
                <n-button
                  size="tiny"
                  quaternary
                  circle
                  type="warning"
                  :disabled="!STOPPABLE.has(job.status)"
                  :title="STOPPABLE.has(job.status) ? 'Stop job' : 'Job is not running'"
                  @click="handleStop(job.job_id, $event)"
                >
                  <template #icon>
                    <n-icon :component="IconSquare" />
                  </template>
                </n-button>
                <n-button
                  size="tiny"
                  quaternary
                  circle
                  type="error"
                  title="Delete job"
                  @click="handleDelete(job, $event)"
                >
                  <template #icon>
                    <n-icon :component="IconX" />
                  </template>
                </n-button>
              </div>
            </div>
            </template>
          </NVirtualList>
          <div v-else class="empty">
            <n-icon :component="IconList" size="24" class="empty__icon" />
            <p class="empty__title">
              {{ filterKey === 'all' ? 'No jobs yet' : 'No jobs match this filter' }}
            </p>
            <p class="empty__sub">
              {{
                filterKey === 'all'
                  ? 'Paste accounts on the left then click "Create job".'
                  : `Switch filter to "All" to see ${counts.all} job(s).`
              }}
            </p>
          </div>
        </div>
      </div>
    </DashboardCard>

    <n-modal v-model:show="qrPreviewOpen" :mask-closable="true" display-directive="show">
      <div class="qr-modal">
        <div class="qr-modal__head">
          <div class="qr-modal__title">
            <n-icon :component="IconQrcode" size="18" />
            <span>
              {{
                qrPreviewJob
                  ? `${qrPreviewJob.account_masked} · QR`
                  : 'QR preview'
              }}
            </span>
          </div>
          <n-button quaternary circle title="Close" @click="closeQrPreview">
            <template #icon>
              <n-icon :component="IconX" />
            </template>
          </n-button>
        </div>

        <div class="qr-modal__body">
          <div v-if="qrPreviewLoading && !qrPreviewBlobUrl" class="qr-modal__loading">
            <n-spin size="large" />
          </div>
          <div v-else-if="qrPreviewBlobUrl" class="qr-modal__qr-wrap">
            <img :src="qrPreviewBlobUrl" alt="Payment QR" class="qr-modal__qr" />
          </div>
          <div v-else class="qr-modal__empty">
            No QR available yet
          </div>

          <div v-if="qrPreviewError" class="qr-modal__error">
            {{ qrPreviewError }}
          </div>

          <div class="qr-modal__link-block">
            <n-text depth="3" class="qr-modal__label">Payment link</n-text>
            <div class="qr-modal__link-row">
              <n-text class="qr-modal__link" :title="qrPreviewLink ?? ''">
                {{ qrPreviewLink ?? 'No payment link yet' }}
              </n-text>
              <n-button
                size="tiny"
                quaternary
                circle
                :disabled="!qrPreviewLink"
                title="Copy payment link"
                @click="handleCopyLink(qrPreviewLink, $event)"
              >
                <template #icon>
                  <n-icon :component="IconCopy" />
                </template>
              </n-button>
            </div>
          </div>
        </div>
      </div>
    </n-modal>
  </section>
</template>

<style scoped>
.job-list {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
  box-sizing: border-box;
}

.body {
  display: flex;
  flex-direction: column;
  min-height: 0;
  flex: 1 1 auto;
  overflow: hidden;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--n-divider-color);
  background: linear-gradient(180deg, rgba(255,255,255,0.02) 0%, transparent 100%);
  flex: 0 0 auto;
}

.wrap {
  flex: 1 1 auto;
  min-height: 0;
  /* `NVirtualList` (bên trong `.rows`) tự quản lý scroll nội bộ
   * (height:100%; overflow:auto) — để `.wrap` cũng `overflow: auto` sẽ
   * tạo scrollbar lồng scrollbar. `hidden` ở đây, scroll thật nằm ở
   * `.rows :deep(.v-vl)`. */
  overflow: hidden;
}

/*
 * Rows — control tốt hơn cho row action buttons + responsive column
 * widths. Header không dùng vì badge status đã đủ label.
 *
 * Virtualized qua `NVirtualList` (naive-ui, re-export `vueuc`) — chỉ
 * render DOM node của các row ĐANG NẰM TRONG VIEWPORT + buffer, thay vì
 * toàn bộ `filtered` (có thể lên tới hàng nghìn job — backend cho giữ
 * tối đa 10.000 job terminal đồng thời). Root cần full height của
 * `.wrap` để component tính đúng viewport — `:deep()` vì NVirtualList
 * không nhận class trực tiếp trên root nó render.
 */
.rows {
  height: 100%;
}
.rows :deep(.v-vl) {
  height: 100%;
}

.row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--n-divider-color);
  cursor: pointer;
  transition: background 0.1s ease;
  position: relative;
  min-height: 42px;
}
.row:hover {
  background: var(--n-hover-color);
}
/*
 * `.row:last-child` (border-bottom: none cho row cuối) đã bị bỏ khi
 * chuyển sang `NVirtualList`: virtual list chỉ render DOM node của row
 * NẰM TRONG VIEWPORT, nên "last-child" giữa các lần scroll sẽ trỏ vào
 * row viewport cuối (không phải row cuối thật của `filtered`) — border
 * biến mất sai vị trí khi cuộn. Giữ border-bottom cho MỌI row là tradeoff
 * chấp nhận được (khác biệt cực nhỏ ở row cuối cùng thật).
 */
.row:focus-visible {
  outline: 2px solid var(--n-primary-color, #cc0066);
  outline-offset: -2px;
}

/* Row status accent — left border 3px */
.row--running::before,
.row--qr_ready::before,
.row--error::before,
.row--stopped::before,
.row--pending::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 3px;
}
.row--pending::before   { background: transparent; }
.row--running::before   { background: var(--accent-blue, #3b82f6); }
.row--qr_ready::before  { background: var(--accent-green, #22c55e); }
.row--error::before     { background: var(--accent-red, #ef4444); }
.row--stopped::before   { background: var(--accent-yellow, #f59e0b); }

/*
 * Active row: đè background nhấn primary (magenta) + accent bar trái to hơn
 * để user nhận diện job đang xem log/QR. Đè `background: var(...) !important`
 * để thắng cả `.row:hover` khi user rê chuột qua row active.
 */
.row--active {
  background: var(--brand-primary-soft, rgba(204, 0, 102, 0.14)) !important;
}
.row--active::before {
  background: var(--brand-primary, #cc0066) !important;
  width: 4px;
}
.row--active .col-account {
  color: var(--brand-primary, #cc0066);
  font-weight: 600;
}

.col-idx {
  color: var(--n-text-color-3);
  font-size: 11px;
  min-width: 32px;
  flex: 0 0 32px;
  text-align: right;
}
.col-status {
  flex: 0 0 auto;
  min-width: 76px;
  display: flex;
  align-items: center;
  gap: 4px;
}
/*
 * Badge R{n} — cùng size với status tag, tone warning để nhận biết ngay
 * job đã có auto-retry. Chỉ 1-2 ký tự nên min-width nhỏ, font-variant
 * tabular-nums cho số cân đối.
 */
.retry-badge {
  font-size: 10px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.5px;
  padding: 0 6px;
}
/* Badge Held — chip nhỏ cạnh status Pending. Cùng size với retry-badge để
   khi có nhiều tag trong 1 row, chiều cao không nhảy. */
.held-badge {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.5px;
  padding: 0 6px;
}
/*
 * Badge Telegram — cùng size với status/retry tag. Icon paper plane +
 * số lần gửi. Tone success/warning tuỳ theo tất cả entry success hay
 * có ít nhất 1 fail (helper `telegramTagType`).
 */
.telegram-badge {
  font-size: 10px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  padding: 0 6px;
}

/*
 * Push_Success_Gate resume button — to hơn hẳn các nút quaternary khác
 * trong header (`size="medium"` + `type="warning" strong`) để user
 * không thể miss. Pulse animation nhẹ khi paused để attract attention
 * mà không gây khó chịu (2.5s cycle, không nhấp nháy nhanh).
 */
.push-gate-resume-btn {
  font-weight: 600;
  animation: push-gate-pulse 2.5s ease-in-out infinite;
  box-shadow: 0 0 0 0 var(--accent-yellow-soft, rgba(245, 158, 11, 0.3));
}

@keyframes push-gate-pulse {
  0%, 100% {
    box-shadow: 0 0 0 0 var(--accent-yellow-soft, rgba(245, 158, 11, 0.3));
  }
  50% {
    box-shadow: 0 0 0 6px transparent;
  }
}

.push-gate-progress-tag {
  font-size: 11px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
/*
 * Tooltip chi tiết — hiển thị top 5 entry gần nhất kèm chat_label/chat_id
 * và giờ gửi. Icon ✓/✗ để phân biệt success/fail nhanh.
 */
.telegram-tooltip {
  min-width: 200px;
  max-width: 280px;
  font-size: 12px;
}
.telegram-tooltip__title {
  font-weight: 600;
  margin-bottom: 4px;
}
.telegram-tooltip__list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.telegram-tooltip__item {
  display: flex;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.telegram-tooltip__item .ok {
  color: var(--n-color-success, #18a058);
  font-weight: 700;
}
.telegram-tooltip__item .fail {
  color: var(--n-color-warning, #f0a020);
  font-weight: 700;
}
.telegram-tooltip__item .muted {
  color: var(--n-text-color-3, #aaa);
  font-size: 11px;
}
.telegram-tooltip__more {
  margin-top: 4px;
  font-size: 11px;
  color: var(--n-text-color-3, #aaa);
  font-style: italic;
}
.col-account {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: var(--n-text-color);
}
.col-plan {
  flex: 0 0 auto;
  min-width: 46px;
  display: flex;
  align-items: center;
}
.col-time {
  flex: 0 0 auto;
  min-width: 70px;
  text-align: right;
  font-variant-numeric: tabular-nums;
  color: var(--n-text-color-2);
  font-size: 11.5px;
}
/*
 * Cột action — LUÔN render 6 nút NButton size="tiny" circle (~22px mỗi
 * nút + 2px gap = 142px). Không cần fixed width vì mọi row có cùng số
 * nút → auto-width nhất quán qua toàn bộ list. Set `flex-shrink: 0` để
 * cột không bị co lại khi cột account dài.
 *
 * Nút bị disable (không áp dụng với status hiện tại) giảm opacity + đổi
 * cursor không chấp nhận, giữ nguyên footprint để layout không xô đẩy.
 */
.col-actions {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 2px;
  white-space: nowrap;
}

/*
 * Nút disabled trong action row — mờ hơn default của naive-ui để user
 * phân biệt rõ nút active/inactive khi liếc nhanh. Cursor mặc định của
 * naive-ui đã là `not-allowed` khi disabled.
 */
.col-actions :deep(.n-button--disabled) {
  opacity: 0.28;
}

.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  padding: 60px 20px;
  color: var(--n-text-color-3);
  gap: 10px;
}
.empty__icon {
  color: var(--n-text-color-3);
  opacity: 0.4;
  margin-bottom: 2px;
  padding: 12px;
  border: 1px dashed var(--n-border-color);
  border-radius: 999px;
}
.empty__title {
  margin: 0;
  font-size: 13px;
  font-weight: 500;
  color: var(--n-text-color-2);
}
.empty__sub {
  margin: 0;
  font-size: 11.5px;
  max-width: 300px;
  line-height: 1.5;
}

.mono {
  font-family: var(--n-font-family-mono);
}

/*
 * ---------------------------------------------------------------------------
 * MOBILE — row 2 dòng:
 *   [#idx STATUS R{n}  ......email......  PLAN  time]
 *   [.................. 6 nút action ....................] (scroll-x nếu tràn)
 *
 * Nếu để nguyên 1 dòng, 6 nút circle 24px + status tag + email dài + time
 * = ~360px min, đè lên hoặc nén tất cả cột trên screen 375px.
 * ---------------------------------------------------------------------------
 */
@media (max-width: 767px) {
  .chips {
    padding: 10px 10px;
    gap: 5px;
  }
  .row {
    flex-wrap: wrap;
    padding: 8px 12px;
    row-gap: 4px;
    column-gap: 8px;
    min-height: 0;
  }
  .col-idx { min-width: 26px; flex: 0 0 26px; }
  .col-status { min-width: auto; }
  .col-account {
    flex: 1 1 100%;
    order: 10; /* xuống dòng riêng để không bóp status/plan/time */
    font-size: 12.5px;
    line-height: 1.35;
  }
  /*
   * col-plan khi rỗng (chưa check plan) vẫn chiếm chỗ 40px làm trống giữa
   * status và time → thu về auto để không tạo gap ma. Khi có tag PLUS/FREE
   * thì content tự đẩy width.
   */
  .col-plan { min-width: 0; }
  .col-time {
    font-size: 11px;
    min-width: 0;
    margin-left: auto; /* đẩy về cuối dòng 1 khi các cột trước ngắn */
  }
  .col-actions {
    flex: 1 1 100%;
    order: 20;
    justify-content: flex-start;
    gap: 2px;
    overflow-x: auto;
    padding: 2px 0;
    -webkit-overflow-scrolling: touch;
  }
  /* Ẩn scrollbar visual của actions bar cho gọn */
  .col-actions::-webkit-scrollbar { display: none; }

  /*
   * Override global mobile touch rule (button { min-height: 44px }) cho
   * các nút icon trong row: 36×36 vẫn đạt WCAG AA (>=24×24) mà giữ info
   * density đủ. 6 nút × 44 = 264 làm mỗi row cao ~200px, chỉ nhét được
   * 3 job trên viewport 900px.
   */
  .col-actions :deep(.n-button) {
    min-height: 32px;
    min-width: 32px;
    height: 32px;
    width: 32px;
  }
  .col-actions :deep(.n-button .n-icon) {
    font-size: 15px;
  }
}
.qr-modal {
  width: min(92vw, 560px);
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
  border-radius: 12px;
  background: var(--n-card-color);
  border: 1px solid var(--n-divider-color);
  box-shadow: 0 18px 48px rgba(0, 0, 0, 0.38);
}

.qr-modal__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.qr-modal__title {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--n-text-color);
}

.qr-modal__title span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.qr-modal__body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.qr-modal__loading,
.qr-modal__empty {
  min-height: 320px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--n-text-color-3);
  border: 1px dashed var(--n-divider-color);
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.02);
}

.qr-modal__qr-wrap {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 12px;
  border-radius: 12px;
  background: #ffffff;
}

.qr-modal__qr {
  display: block;
  width: min(100%, 420px);
  height: auto;
  max-height: 60vh;
  object-fit: contain;
}

.qr-modal__error {
  padding: 8px 10px;
  border-radius: 10px;
  border: 1px solid rgba(239, 68, 68, 0.35);
  background: rgba(239, 68, 68, 0.08);
  color: var(--n-color-error, #ef4444);
  font-size: 12px;
  line-height: 1.4;
  word-break: break-word;
}

.qr-modal__link-block {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.qr-modal__label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.qr-modal__link-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.qr-modal__link {
  min-width: 0;
  flex: 1 1 auto;
  font-family: var(--n-font-family-mono);
  font-size: 12px;
  line-height: 1.45;
  word-break: break-all;
}

</style>
