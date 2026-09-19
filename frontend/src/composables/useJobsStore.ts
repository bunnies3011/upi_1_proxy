/**
 * useJobsStore — Pinia store quản lý danh sách Job (Requirement 8.1, 8.7, 12.3, 12.8).
 *
 * Vai trò:
 * - Nguồn duy nhất cho UI khi hiển thị JobList + JobDetailPanel (design.md
 *   "State: `useJobsStore` (Pinia) giữ `Map<job_id, JobViewModel>`, cập nhật
 *   qua `useSse` khi nhận event `job_status`/`job_log`").
 * - Đóng vai trò client cho các endpoint jobs: `GET /api/jobs`,
 *   `GET /api/jobs/{id}`, `POST /api/jobs`, `DELETE /api/jobs/{id}`.
 *   Endpoint `GET /api/jobs/{id}/qr.png` KHÔNG dùng qua store — component
 *   binding trực tiếp `<img :src="/api/jobs/{id}/qr.png">` (design.md).
 *
 * Reactivity của `Map` / `Set`:
 * - Vue 3 hỗ trợ reactive Map/Set native (mutation `.set`/`.delete`/`.add`
 *   trigger update). Bọc trong `ref(new Map())` để Pinia setup store nhận
 *   diện là state, đồng thời `for...of` trong `computed` cũng được track.
 *
 * Requirement 11.5 (KHÔNG dùng localStorage cho runtime config):
 * - Toàn bộ state ở đây là in-memory, mất khi reload — đúng chủ ý. Nếu
 *   tương lai muốn "hidden job IDs" survive reload, phải chuyển qua
 *   Settings Store SQLite (bằng key mới trong whitelist), KHÔNG được dùng
 *   `localStorage.setItem`.
 *
 * Requirement 12.7 (optimistic update chỉ SAU khi API xác nhận):
 * - `submitBatch`/`stopJob` chỉ commit thay đổi vào store SAU khi fetch
 *   thành công. Failure → giữ nguyên state trước đó + set `error`, để UI
 *   hiển thị thông báo lỗi mà không cần logic revert phức tạp.
 */

import { computed, ref, type ComputedRef, type Ref } from 'vue'
import { defineStore } from 'pinia'

// ---------------------------------------------------------------------------
// Types — khớp shape backend `api/schemas.py` (JobViewCompact/JobViewDetail).
// ---------------------------------------------------------------------------

/**
 * `Job_Status` enum khớp `core/payment_flow.py::JobStatus`. Union type thay
 * cho enum runtime vì TS union đủ cho type-check + zero runtime cost.
 */
export type JobStatus = 'pending' | 'running' | 'qr_ready' | 'error' | 'stopped'

/**
 * 1 log entry của job (Requirement 8.8, 14.2). `ts` là epoch seconds; `extra`
 * là metadata bổ sung ĐÃ được backend redact trước khi phát (R14.8) — store
 * chỉ chuyển tiếp nguyên vẹn.
 */
export interface JobLogEntry {
  ts: number
  message: string
  extra: Record<string, unknown>
}

/**
 * 1 bản ghi khi backend đã gửi (hoặc cố gắng gửi hết retry) QR PNG tới
 * Telegram (Bug 13 fix — feature "track Telegram notification per job";
 * mở rộng thêm retry+backoff qua queue nội bộ — xem
 * `backend/app/notifiers/telegram/notifier.py::_send_with_retry`).
 * Backend append ĐÚNG 1 entry sau khi kết thúc (thành công hoặc hết
 * retry) — không có 1 entry riêng cho mỗi attempt fail giữa đường. FE
 * hiển thị badge (số lần đã gửi) + tooltip liệt kê chat trên JobList item
 * và section chi tiết ở JobDetailPanel.
 *
 * Contract:
 *   - `chat_id`: Telegram chat_id (string vì có thể là số âm supergroup
 *     vượt Number.MAX_SAFE_INTEGER).
 *   - `chat_label`: label user đặt (`""` khi chưa đặt — FE fallback
 *     hiển thị `chat_id`).
 *   - `sent_at`: epoch seconds — thời điểm attempt CUỐI (không phải lần
 *     đầu) hoàn tất.
 *   - `success`: True khi API trả OK (ở bất kỳ attempt nào); False +
 *     `error` khi hết retry vẫn fail.
 *   - `error`: `null` khi success; dict `{status_code, error_code,
 *     description}` của attempt cuối khi fail.
 *   - `attempts`: số lần đã thử gửi (1 = thành công/fail ngay lần đầu,
 *     >1 = đã retry). Optional — entry cũ trước khi ship retry không có
 *     field này, FE fallback hiển thị `1`.
 */
export interface TelegramNotificationEntry {
  chat_id: string
  chat_label: string
  sent_at: number
  success: boolean
  error: {
    status_code: number | null
    error_code: number | null
    description: string
  } | null
  attempts?: number
}

/**
 * View model 1 job cho UI. Kết hợp field từ cả `JobViewCompact` (list) và
 * `JobViewDetail` (detail): `logs`/`artifact_path`/`error_code`/`error_message`
 * chỉ có khi đã gọi `loadDetail()` hoặc SSE đã đẩy log/artifact về.
 */
export interface JobViewModel {
  job_id: string
  payment_method: string
  account_masked: string
  /**
   * Raw credential `email|password|2fa` — populate từ backend qua
   * `GET /api/jobs` (compact) hoặc `GET /api/jobs/{id}` (detail). Field
   * này TỒN TẠI QUA RELOAD TRANG vì backend persist trong SQLite. Trước
   * đây FE dùng in-memory Map `rawAccountLines` → reload là mất →
   * SuccessOutputPanel hiển thị "0 có credential"; giờ dùng `account_line`
   * từ BE → luôn có credential đầy đủ để copy.
   *
   * Optional để backward compat với entry tạo từ SSE `job_log` chưa có
   * (chỉ có sau `loadAll`/`loadDetail`).
   */
  account_line?: string
  status: JobStatus
  updated_at: number
  /** Chỉ populated sau `loadDetail()` hoặc khi SSE `job_log` đẩy về. */
  logs?: JobLogEntry[]
  artifact_path?: string | null
  error_code?: string | null
  error_message?: string | null
  /**
   * URL thanh toán (VD `https://pay.ideal.nl/transactions/...`), có mặt khi
   * và chỉ khi `status === 'qr_ready'`. Cho phép UI copy link nhanh trên
   * JobList row mà không cần download PNG rồi decode. Populated qua:
   * - `GET /api/jobs` (compact) — khi refresh list.
   * - `GET /api/jobs/{id}` (detail) — khi mở JobDetailPanel.
   * - SSE `job_status` extra — khi backend broadcast qr_ready lúc handler
   *   vừa hoàn tất, để UI có link trong ≤2 giây (R12.5).
   */
  payment_link?: string | null
  /**
   * Counter tăng đơn điệu do backend cấp lúc tạo job — CỐ ĐỊNH VĨNH VIỄN
   * cho 1 job (dedup reuse KHÔNG đổi order). FE sort `visibleJobs` theo
   * `order ASC` để thứ tự UI không phụ thuộc Map insertion order của
   * Vue reactive.
   *
   * Optional trên type vì entry tạo từ SSE `job_log` (chưa có `order`
   * trong payload) sẽ tạm không có; sort fallback đưa entry đó xuống
   * cuối cho tới khi `loadAll`/`loadDetail` populate.
   */
  order?: number
  /**
   * Số lần auto-retry đã dùng cho job này (Settings
   * `ideal.auto_retry_blocked_*`). Backend tăng mỗi khi
   * `_maybe_auto_retry` schedule lại; reset về 0 khi user thao tác thủ
   * công (submit/rerun). FE hiển thị badge `R{n}` trên row khi > 0.
   */
  retry_count?: number
  /**
   * Mốc thời gian chạy job THẬT (epoch seconds) — khác `updated_at` (mốc
   * broadcast SSE gần nhất, có thể tăng liên tục theo log).
   *
   *   - `pending` → chưa từng chạy: cả 2 null. FE hiển thị `—`.
   *   - `running` → `started_at` set, `finished_at` null: FE tick 1s tính
   *     `now - started_at`.
   *   - terminal (`qr_ready`/`error`/`stopped`) → cả 2 set: FE đóng băng
   *     `finished_at - started_at`, KHÔNG đếm nữa.
   */
  started_at?: number | null
  finished_at?: number | null
  /**
   * History Telegram notification cho job này. Rỗng khi Telegram disabled
   * hoặc job chưa `qr_ready`. Backend broadcast qua SSE `job_notified`
   * mỗi lần notifier `send_photo` xong.
   */
  telegram_notifications?: TelegramNotificationEntry[]
  /**
   * Cờ orthogonal với `status`. `held=true` chỉ có ý nghĩa khi
   * `status === 'pending'` — job đã tạo qua nút "+ Add" nhưng chưa vào
   * queue backend, chờ user bấm Start (per-row ▶ hoặc bulk "Start all
   * held"). Backend clear khi transition sang RUNNING.
   *
   * Optional để backward compat với BE cũ chưa broadcast field này —
   * undefined coi như false (giữ hành vi cũ 100%).
   */
  held?: boolean
  /**
   * Kết quả check-plan cuối cùng đã persist trong DB (yêu cầu 2026-07 —
   * Successful accounts luôn hiện qua reload trang):
   *   - `null`   → chưa từng check hoặc job vừa rerun. FE dùng
   *                `planStates` transient (loading state) để hiển thị
   *                spinner khi user bấm nút Check.
   *   - `"plus"` → tài khoản Plus (SuccessOutputPanel lọc theo giá trị
   *                này). "Check Plus All" SKIP row `plan==='plus'`.
   *   - `"free"` → Free. UI hiển thị badge FREE; "Check Plus All"
   *                VẪN re-check.
   * Optional cho backward compat với BE cũ chưa broadcast — undefined
   * coi như null.
   */
  plan?: 'plus' | 'free' | null
}

/**
 * 1 dòng account bị skip trong batch submit gần nhất (Requirement 8.2).
 * Frontend hiển thị ở JobInputPanel để user đối chiếu và sửa input.
 */
export interface SkippedLine {
  line: string
  reason: string
}

/**
 * Trạng thái kết quả check-plan cho 1 job.
 *
 * Cần lift lên store (thay vì để local `reactive` trong JobList.vue) vì:
 *   - Yêu cầu 2026-07: Success panel chỉ hiển thị account đã lên PLUS
 *     (không còn dùng `status === qr_ready` làm điều kiện thành công).
 *     SuccessOutputPanel phải đọc được `planStates` để lọc — cần chung
 *     nguồn với JobList (nơi user bấm check).
 *   - Nút "Check Plus All" (cạnh "Làm mới") cần cùng logic của
 *     `handleCheckPlan` từng item → tránh duplicate.
 *
 * Không persist qua reload page — plan có thể đổi theo thời gian, mỗi
 * phiên user tự bấm check lại. Session cache backend đủ nhanh (~1s/req).
 */
export type PlanState =
  | { loading: true }
  | {
      loading: false
      plan: 'plus' | 'free' | 'unknown'
      raw?: string
      error?: string
    }

// ---------------------------------------------------------------------------
// Backend response DTOs (khớp `backend/app/api/schemas.py`).
// ---------------------------------------------------------------------------

interface JobViewCompactDTO {
  job_id: string
  payment_method: string
  account_masked: string
  /** Raw credential `email|password|2fa` từ input textarea, expose từ BE. */
  account_line: string
  status: string
  updated_at: number
  payment_link?: string | null
  order: number
  retry_count?: number
  started_at?: number | null
  finished_at?: number | null
  telegram_notifications?: TelegramNotificationEntry[]
  /** Xem docstring `JobViewModel.held`. Optional cho BE cũ chưa migrate. */
  held?: boolean
  /** Xem docstring `JobViewModel.plan`. Optional cho BE cũ chưa migrate. */
  plan?: 'plus' | 'free' | null
}

interface JobLogEntryDTO {
  ts: number
  message: string
  extra?: Record<string, unknown>
}

interface JobViewDetailDTO extends JobViewCompactDTO {
  logs?: JobLogEntryDTO[]
  artifact_path?: string | null
  error_code?: string | null
  error_message?: string | null
}

interface SubmitJobsResponseDTO {
  created_job_ids: string[]
  skipped: SkippedLine[]
}

interface StopJobResponseDTO {
  job_id: string
  status: string
}

interface ErrorResponseDTO {
  error_code?: string
  message?: string
  details?: Record<string, unknown>
}

// ---------------------------------------------------------------------------
// SSE event payload contracts (khớp `core/sse.py`):
// - `job_status`: `{job_id, status, ...extra}` (extra: `error_code`,
//   `error_message`, `artifact_path`).
// - `job_log`:    `{job_id, message, ts, ...extra}` (extra: đã redact ở
//   backend).
// `updated_at` KHÔNG có trong SSE payload gốc — task 28.4 (useSse) chịu
// trách nhiệm gắn `Date.now()/1000` trước khi dispatch tới store để đảm
// bảo `visibleJobs` sort đúng.
// ---------------------------------------------------------------------------

export interface JobStatusEvent {
  job_id: string
  status: JobStatus
  updated_at: number
  error_code?: string | null
  error_message?: string | null
  artifact_path?: string | null
  payment_link?: string | null
  payment_method?: string
  account_masked?: string
  /**
   * Order counter đi kèm SSE `job_status` — backend gán cho mỗi lần
   * broadcast để FE có sort key ngay từ event đầu tiên (không phải chờ
   * loadAll). Optional để backward compat với event cũ không có field
   * này (fallback: sort đưa entry xuống cuối).
   */
  order?: number
  /**
   * Số lần auto-retry backend đã dùng cho job (broadcast kèm mỗi
   * transition status). Optional để backward compat với backend cũ
   * chưa có field này.
   */
  retry_count?: number
  /**
   * Mốc thời gian chạy — SSE đẩy về mỗi khi transition status. Semantics
   * xem trong `JobViewModel.started_at`.
   */
  started_at?: number | null
  finished_at?: number | null
  /**
   * Cờ Add / Run held — backend broadcast kèm mỗi transition status để
   * FE nhận merge patch. Optional cho BE cũ (undefined coi như false).
   */
  held?: boolean
  /**
   * Plan đã persist — backend broadcast kèm mỗi transition status
   * (`_broadcast_status` gắn `plan=record.plan`) để FE reactive giữ
   * đúng SuccessOutputPanel + badge PLUS/FREE mà không cần `loadAll`.
   * Optional cho BE cũ chưa broadcast (undefined coi như "no change").
   */
  plan?: 'plus' | 'free' | null
}

export interface JobLogEvent {
  job_id: string
  ts: number
  message: string
  /** Mọi key khác trong payload SSE (đã redact ở backend). */
  [key: string]: unknown
}

/**
 * SSE event `job_notified` — backend broadcast sau mỗi lần
 * `TelegramNotifier.send_photo` (thành công hoặc thất bại). FE merge
 * `entry` vào cuối `job.telegram_notifications` array.
 *
 * Xem `broadcast_job_notified` ở `backend/app/core/sse.py`.
 */
export interface JobNotifiedEvent {
  job_id: string
  entry: TelegramNotificationEntry
}

// ---------------------------------------------------------------------------
// Config + helpers
// ---------------------------------------------------------------------------

const API_BASE = '/api'

/**
 * Cap số log entry giữ trong `JobViewModel.logs` — PHẢI khớp
 * `_MAX_LOG_ENTRIES_PER_JOB` ở `backend/app/core/job_manager.py` (deque
 * maxlen=500). Backend đã bounded phía server, nhưng trước fix này FE
 * `applyLogEvent` append vô hạn (`[...existing.logs, entry]` không trim)
 * — với nhiều job sống lâu / chạy đồng thời, `jobs` Map giữ TOÀN BỘ job
 * (không chỉ job đang hiển thị) nên tổng RAM log tăng không giới hạn dù
 * backend đã cap, gây OOM tab Chrome (`SBOX_FATAL_MEMORY_EXCEEDED`).
 *
 * Trim theo kiểu ring-buffer (giữ N entry mới nhất — `slice(-N)`) mỗi khi
 * `applyLogEvent` append, để invariant "logs.length <= N" luôn đúng ngay
 * cả khi SSE bắn dồn dập.
 */
const MAX_LOG_ENTRIES_PER_JOB = 500

/**
 * Cap kích thước JSON-serialized của field `extra` mỗi log entry (bytes).
 * Backend redact secret nhưng vẫn có thể log full HTTP response / stacktrace
 * nhiều KB — với 500 entries/job × N job, `extra` không capped có thể chiếm
 * hàng chục MB. Vượt cap → thay bằng snippet `{_truncated, _size, _preview}`
 * để không mất hoàn toàn context nhưng cắt được phần bulk.
 */
const MAX_EXTRA_JSON_BYTES = 2048

/**
 * Cap `telegram_notifications` per job — job qr_ready có thể gửi nhiều chat
 * lần lượt (bulk fanout N chat × M retry). Không cap sẽ tăng vô hạn theo
 * thời gian sống job trong Map.
 */
const MAX_TELEGRAM_NOTIFICATIONS_PER_JOB = 100

/**
 * Cắt `extra` payload nếu vượt cap. Trả về object mới (KHÔNG mutate input)
 * — Vue reactivity không bị nhầm dirty vì payload SSE dùng 1 lần rồi bỏ.
 * JSON.stringify có thể throw với circular ref → fallback về object rỗng
 * để không kill toàn stream.
 */
function capExtraPayload(extra: Record<string, unknown>): Record<string, unknown> {
  try {
    const serialized = JSON.stringify(extra)
    if (serialized.length <= MAX_EXTRA_JSON_BYTES) return extra
    return {
      _truncated: true,
      _size: serialized.length,
      _preview: serialized.slice(0, 256),
    }
  } catch {
    return {}
  }
}

/**
 * Set các giá trị `Job_Status` hợp lệ — dùng để narrow từ `string` (DTO)
 * xuống `JobStatus` (union). Nếu backend trả trạng thái lạ (không nên xảy
 * ra), store fallback `'error'` để UI vẫn hiển thị được thay vì crash.
 */
const KNOWN_JOB_STATUSES: ReadonlySet<JobStatus> = new Set<JobStatus>([
  'pending',
  'running',
  'qr_ready',
  'error',
  'stopped',
])

function narrowJobStatus(raw: string): JobStatus {
  return (KNOWN_JOB_STATUSES as Set<string>).has(raw)
    ? (raw as JobStatus)
    : 'error'
}

/**
 * Lỗi thin wrapper — mang `error_code` (nếu backend trả) để UI phân loại
 * chính xác. Message ưu tiên `body.message`, fallback `body.error_code`,
 * cuối cùng là `HTTP <status>`.
 */
export class JobsApiError extends Error {
  readonly status: number
  readonly errorCode: string | null
  readonly details: Record<string, unknown>

  constructor(
    status: number,
    message: string,
    errorCode: string | null,
    details: Record<string, unknown>,
  ) {
    super(message)
    this.name = 'JobsApiError'
    this.status = status
    this.errorCode = errorCode
    this.details = details
  }
}

/**
 * Fetch + parse lỗi thành `JobsApiError` với `error_code` từ body theo
 * shape `ErrorResponse` của backend. Trả về Response gốc để caller tự parse
 * JSON — tách trách nhiệm.
 */
async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const mergedHeaders: Record<string, string> = {
    Accept: 'application/json',
    ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
    ...((init?.headers as Record<string, string> | undefined) ?? {}),
  }

  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: mergedHeaders,
  })

  if (!res.ok) {
    // FastAPI trả lỗi domain theo shape `{"detail": {"error_code", "message",
    // "details"}}` (raise HTTPException) HOẶC `{"error_code", "message",
    // "details"}` trực tiếp (trả JSONResponse). Hỗ trợ cả 2 shape.
    let body: ErrorResponseDTO = {}
    try {
      const parsed = (await res.json()) as Record<string, unknown>
      const detail = parsed['detail']
      if (detail && typeof detail === 'object') {
        body = detail as ErrorResponseDTO
      } else {
        body = parsed as ErrorResponseDTO
      }
    } catch {
      // Response không phải JSON hợp lệ — giữ body rỗng.
    }
    const errorCode = typeof body.error_code === 'string' ? body.error_code : null
    const message =
      (typeof body.message === 'string' && body.message) ||
      errorCode ||
      `HTTP ${res.status}`
    throw new JobsApiError(
      res.status,
      message,
      errorCode,
      body.details ?? {},
    )
  }

  return res
}

/**
 * Convert 1 log entry DTO sang `JobLogEntry`. `extra` mặc định `{}` nếu
 * backend trả thiếu (fail-safe, giữ nguyên contract).
 */
function toJobLogEntry(dto: JobLogEntryDTO): JobLogEntry {
  return {
    ts: dto.ts,
    message: dto.message,
    extra: dto.extra ?? {},
  }
}

/**
 * Merge compact view mới về từ server vào entry cũ trong map. Giữ nguyên
 * `logs`/`artifact_path`/`error_code`/`error_message` nếu đã fetch qua
 * `loadDetail()` trước đó — tránh mất chi tiết khi `loadAll()` refresh
 * danh sách. Nếu entry cũ không có, tạo mới.
 */
function mergeCompact(
  existing: JobViewModel | undefined,
  compact: JobViewCompactDTO,
): JobViewModel {
  const base: JobViewModel = {
    job_id: compact.job_id,
    payment_method: compact.payment_method,
    account_masked: compact.account_masked,
    account_line: compact.account_line,
    status: narrowJobStatus(compact.status),
    updated_at: compact.updated_at,
    // `payment_link` giờ có trong compact — ưu tiên giá trị mới nhất từ
    // server (khi refresh list). Nếu compact không có, fallback về giá trị
    // existing (đã set qua SSE/loadDetail trước đó).
    payment_link: compact.payment_link ?? existing?.payment_link ?? null,
    order: compact.order,
    retry_count: compact.retry_count ?? existing?.retry_count ?? 0,
    started_at: compact.started_at ?? existing?.started_at ?? null,
    finished_at: compact.finished_at ?? existing?.finished_at ?? null,
    // Ưu tiên list từ backend (source of truth); nếu compact không kèm
    // (payload cũ trước migration), giữ existing hoặc rỗng.
    telegram_notifications:
      compact.telegram_notifications ??
      existing?.telegram_notifications ??
      [],
    held: compact.held ?? existing?.held ?? false,
    // `plan` ưu tiên compact (BE là source of truth persist qua DB).
    // Nếu compact không có (BE cũ trước migration) → giữ existing.
    // Không fallback về null nếu chỉ vắng mặt trong 1 patch, tránh clear
    // nhầm giá trị đã có từ SSE trước.
    plan:
      compact.plan !== undefined
        ? compact.plan
        : existing?.plan ?? null,
  }
  if (!existing) {
    return base
  }
  // Giữ lại các field detail đã có (logs/artifact/error) nếu existing đã
  // được populate qua loadDetail() hoặc SSE.
  return {
    ...base,
    logs: existing.logs,
    artifact_path: existing.artifact_path,
    error_code: existing.error_code,
    error_message: existing.error_message,
  }
}

/**
 * Convert detail view DTO thành `JobViewModel`. Ghi đè hoàn toàn (detail là
 * snapshot đầy đủ nhất từ server, KHÔNG cần merge với entry cũ).
 */
function fromDetail(detail: JobViewDetailDTO): JobViewModel {
  return {
    job_id: detail.job_id,
    payment_method: detail.payment_method,
    account_masked: detail.account_masked,
    account_line: detail.account_line,
    status: narrowJobStatus(detail.status),
    updated_at: detail.updated_at,
    logs: (detail.logs ?? []).map(toJobLogEntry),
    artifact_path: detail.artifact_path ?? null,
    error_code: detail.error_code ?? null,
    error_message: detail.error_message ?? null,
    payment_link: detail.payment_link ?? null,
    order: detail.order,
    retry_count: detail.retry_count ?? 0,
    started_at: detail.started_at ?? null,
    finished_at: detail.finished_at ?? null,
    telegram_notifications: detail.telegram_notifications ?? [],
    held: detail.held ?? false,
    plan: detail.plan ?? null,
  }
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export interface UseJobsStoreState {
  jobs: Ref<Map<string, JobViewModel>>
  hiddenJobIds: Ref<Set<string>>
  skippedLastBatch: Ref<SkippedLine[]>
  loading: Ref<boolean>
  error: Ref<string | null>
  autoRetryFailedEnabled: Ref<boolean>
  /**
   * ID job đang được UI focus (LogPanel/JobDetailPanel đang hiển thị).
   * Store dùng để quyết định có giữ `logs[]` đầy đủ cho job đó không —
   * job không focus chỉ giữ meta compact, log SSE bị drop để tiết kiệm RAM.
   * Khi user chọn job khác, `setFocusedJobId` clear logs job cũ + LogPanel
   * tự `loadDetail(newId)` để lấy logs mới nhất từ backend (persist SQLite).
   */
  focusedJobId: Ref<string | null>
  /**
   * Map job_id → account_line thô (VD `email|password|totp_secret`) user
   * đã submit, để `SuccessOutputPanel` hiển thị lại dưới dạng credential
   * plain-text khi job qr_ready. In-memory only (R11.5 — không persist), mất
   * khi reload page. Chỉ populate khi FE tự submit trong session hiện tại;
   * job load từ backend qua `loadAll()` (từ trước reload) không có entry
   * → panel Success fallback hiển thị `account_masked`.
   */
  rawAccountLines: Ref<Map<string, string>>
  /**
   * Map `job_id → PlanState`. UI đọc để quyết định:
   *   - JobList: tag PLUS/FREE + loading spinner nút check-plan.
   *   - SuccessOutputPanel: chỉ hiện account có `plan === 'plus'`.
   * KHÔNG persist. Reset khi delete/clear/reset.
   */
  planStates: Ref<Map<string, PlanState>>
  visibleJobs: ComputedRef<JobViewModel[]>
  /**
   * Danh sách job đã lên PLUS — dẫn xuất từ `visibleJobs` +
   * `planStates`. Điều kiện: status `qr_ready` AND
   * `planStates.get(job_id)?.plan === 'plus'`. Sort theo `order ASC`
   * (kế thừa `visibleJobs`). Đây là NGUỒN SỰ THẬT DUY NHẤT cho
   * "account thành công" — SuccessOutputPanel bind computed này thay
   * vì tự lọc.
   */
  plusJobs: ComputedRef<JobViewModel[]>
  /**
   * Visible jobs that are NOT Plus by plan alone (no status gate).
   * `isPlus = job.plan==='plus' || planStates plus`. Excludes permanent
   * no-free-offer errors so FreeOutputPanel only contains accounts worth
   * re-running.
   */
  freeJobs: ComputedRef<JobViewModel[]>
  /** Permanent amount-gate rejects grouped separately and never re-run. */
  noFreeOfferJobs: ComputedRef<JobViewModel[]>
  /**
   * Đếm số job theo trạng thái — SINGLE PASS O(N) chia sẻ giữa App.vue
   * stats bar và JobList counts chip. Trước fix này, App.vue.stats +
   * JobList.counts + JobList.qrReadyCount chạy 3 vòng O(N) độc lập cho
   * cùng 1 event SSE → chi phí re-eval nhân 3.
   */
  jobStats: ComputedRef<{
    total: number
    pending: number
    running: number
    qr_ready: number
    error: number
    stopped: number
    /**
     * Số job `pending + held=true` — subset của `pending`. UI dùng để
     * hiển thị nút "▶ Start N held" trong BulkActionsBar (chỉ hiện
     * khi > 0) và tooltip trên chip Pending.
     */
    held: number
  }>
  loadAll: () => Promise<void>
  loadDetail: (jobId: string) => Promise<void>
  submitBatch: (
    lines: string[],
    paymentMethod?: string,
    options?: { start?: boolean },
  ) => Promise<{ created: string[]; skipped: SkippedLine[] }>
  /**
   * `POST /api/jobs/{id}/start` — chuyển 1 job `pending + held=true` vào
   * queue để scheduler chạy. Server broadcast SSE `job_status` với
   * `held=false` → `applyStatusEvent` merge patch, FE ẩn badge Held +
   * nút Start.
   */
  startJob: (jobId: string) => Promise<void>
  /**
   * `POST /api/jobs/start-all` — bulk start mọi job `pending + held=true`
   * theo order tăng dần. Trả số job vừa start.
   */
  startAllHeld: () => Promise<number>
  stopJob: (jobId: string) => Promise<void>
  rerunJob: (
    jobId: string,
  ) => Promise<{ created: string[]; skipped: SkippedLine[] }>
  checkPlan: (jobId: string) => Promise<PlanState>
  /**
   * Bulk check-plan cho tất cả job `qr_ready`. Tái sử dụng `checkPlan`
   * bên dưới — chạy song song có giới hạn concurrency để không hammer
   * chatgpt.com (ChatGPT rate-limit `/api/auth/session` ~10 req/s per
   * IP). Trả `{plus, free, unknown, errors, skipped_plus}` tổng kết
   * cho UI toast.
   *
   * `skipped_plus`: số row đã có `plan === 'plus'` persist trong DB
   * nên bỏ qua network call (yêu cầu 2026-07 "check plus all bỏ qua
   * tài khoản plus luôn"). Row skip vẫn được ĐẾM vào `plus` tally để
   * user thấy tổng số Plus hiện có; `total` = số check thật + skip.
   */
  checkPlanAll: () => Promise<{
    plus: number
    free: number
    unknown: number
    errors: number
    total: number
    skipped_plus: number
  }>
  deleteJob: (jobId: string) => Promise<void>
  stopAllJobs: () => Promise<number>
  rerunFailedJobs: () => Promise<{ created: string[]; skipped: SkippedLine[] }>
  setAutoRetryFailedEnabled: (enabled: boolean) => void
  maybeAutoRetryFailed: () => Promise<boolean>
  clearJobs: (
    filter?: 'all' | 'failed' | 'completed' | 'finished',
  ) => Promise<number>
  hideJob: (jobId: string) => void
  unhideJob: (jobId: string) => void
  isHidden: (jobId: string) => boolean
  clearError: () => void
  applyStatusEvent: (evt: JobStatusEvent) => void
  applyLogEvent: (evt: JobLogEvent) => void
  applyNotifiedEvent: (evt: JobNotifiedEvent) => void
  setFocusedJobId: (jobId: string | null) => void
  reset: () => void
}

export const useJobsStore = defineStore('jobs', (): UseJobsStoreState => {
    // -------------------------------------------------------------------
    // State
    // -------------------------------------------------------------------
    const jobs: Ref<Map<string, JobViewModel>> = ref(new Map<string, JobViewModel>())
    const hiddenJobIds: Ref<Set<string>> = ref(new Set<string>())
    const skippedLastBatch: Ref<SkippedLine[]> = ref<SkippedLine[]>([])
    const loading: Ref<boolean> = ref(false)
    const error: Ref<string | null> = ref<string | null>(null)
    const autoRetryFailedEnabled: Ref<boolean> = ref(false)
    const rawAccountLines: Ref<Map<string, string>> = ref(new Map<string, string>())
    const planStates: Ref<Map<string, PlanState>> = ref(new Map<string, PlanState>())
    const focusedJobId: Ref<string | null> = ref<string | null>(null)
    let autoRetryFailedInFlight = false

    // -------------------------------------------------------------------
    // Getters
    // -------------------------------------------------------------------
    /**
     * Danh sách job KHÔNG bị hide, sort tăng dần theo `order`.
     *
     * `order` là counter do backend cấp lúc tạo job và CỐ ĐỊNH vĩnh viễn
     * cho job đó (dedup reuse KHÔNG đổi order). Sort theo `order ASC`
     * đảm bảo:
     * - Thứ tự UI trùng thứ tự tạo job (thứ tự line input textarea của
     *   lần đầu tiên submit).
     * - Status đổi (pending → running → success/error) KHÔNG đổi vị trí.
     * - Retry failed / rerun job KHÔNG đổi vị trí.
     *
     * KHÔNG dùng Map insertion order làm sort key vì Vue reactive wrap
     * trên `ref(new Map())` có thể có edge case khi có nhiều mutation
     * đồng thời từ SSE — dẫn tới user thấy "job nhảy vị trí trong lúc
     * chạy". Sort theo field `order` là guarantee tuyệt đối.
     *
     * Entry chưa có `order` (VD SSE `job_log` tới trước loadAll tạo
     * placeholder) → xếp cuối bằng cách coi order = Infinity. Sau
     * loadAll populate `order` thật, entry sẽ nhảy về đúng vị trí một
     * lần duy nhất.
     */
    const visibleJobs: ComputedRef<JobViewModel[]> = computed(() => {
      const arr: JobViewModel[] = []
      for (const [jobId, job] of jobs.value) {
        if (!hiddenJobIds.value.has(jobId)) {
          arr.push(job)
        }
      }
      arr.sort((a, b) => {
        const oa = typeof a.order === 'number' ? a.order : Number.POSITIVE_INFINITY
        const ob = typeof b.order === 'number' ? b.order : Number.POSITIVE_INFINITY
        return oa - ob
      })
      return arr
    })

    /**
     * Chỉ những job đã được VERIFY đã lên PLUS (thay vì mọi qr_ready).
     * Điều kiện `AND`:
     *   1. `status === 'qr_ready'` — flow tạo QR + thanh toán xong.
     *   2. `job.plan === 'plus'` (persisted DB) HOẶC `planStates.get(id)?.plan === 'plus'`
     *      (in-memory transient trong lúc user bấm check nhưng SSE chưa đẩy về).
     *
     * Ưu tiên `job.plan` từ backend (nguồn thật, tồn tại qua reload
     * trang — yêu cầu 2026-07). `planStates` chỉ đóng vai trò state
     * transient trong lúc HTTP request chưa hoàn tất — tránh flash
     * UI xuống rồi lên khi round-trip mạng chậm.
     *
     * Job chưa check plan → KHÔNG được coi là thành công (dù đã qr_ready).
     * User bấm "Check Plus All" hoặc icon UserCheck từng row để verify.
     */
    const plusJobs: ComputedRef<JobViewModel[]> = computed(() => {
      const arr: JobViewModel[] = []
      for (const job of visibleJobs.value) {
        if (job.status !== 'qr_ready') continue
        if (job.plan === 'plus') {
          arr.push(job)
          continue
        }
        const state = planStates.value.get(job.job_id)
        if (state && !state.loading && state.plan === 'plus') {
          arr.push(job)
        }
      }
      return arr
    })

    /**
     * Plan-only Plus check (no status gate). Differs from `plusJobs`,
     * which also requires `status === 'qr_ready'`.
     *
     * Also treats checkout "already paid" errors as Plus so Free re-run
     * export never includes accounts that are already subscribed.
     */
    function isPlusByPlan(job: JobViewModel): boolean {
      if (job.plan === 'plus') return true
      const state = planStates.value.get(job.job_id)
      if (state && !state.loading && state.plan === 'plus') return true
      if (job.error_code === 'oaipay_already_paid') return true
      const msg = (job.error_message || '').toLowerCase()
      return msg.includes('already paid') || msg.includes('user is already paid')
    }

    function isDeactivatedLoginJob(job: JobViewModel): boolean {
      if (job.error_code !== 'login_failed') return false
      const logText = (job.logs || []).map((entry) => entry.message).join('\n')
      const msg = `${job.error_message || ''}\n${logText}`.toLowerCase()
      return (
        msg.includes('account_deleted_or_deactivated') ||
        msg.includes('deleted or deactivated') ||
        msg.includes('has been deleted') ||
        msg.includes('deactivated') ||
        msg.includes('do not have an account')
      )
    }

    function isNoFreeOfferJob(job: JobViewModel): boolean {
      return job.error_code === 'no_free_offer' || isDeactivatedLoginJob(job)
    }

    /**
     * Every visible non-Plus account (any status). Plan-plus jobs that
     * are not yet qr_ready stay out of Free — they are known Plus.
     * Permanent amount-gate rejects stay out too: re-running them would just
     * hit the same paid amount again.
     */
    const freeJobs: ComputedRef<JobViewModel[]> = computed(() =>
      visibleJobs.value.filter(
        (job) => !isPlusByPlan(job) && !isNoFreeOfferJob(job),
      ),
    )

    const noFreeOfferJobs: ComputedRef<JobViewModel[]> = computed(() =>
      visibleJobs.value.filter(isNoFreeOfferJob),
    )

    /**
     * Đếm job theo trạng thái, single-pass. Vue cache computed → mọi
     * consumer (App.vue action-row, JobList chip filter, JobList
     * qrReadyCount) chia sẻ 1 lần iterate mỗi khi `visibleJobs` đổi.
     */
    const jobStats = computed(() => {
      const s = {
        total: 0,
        pending: 0,
        running: 0,
        qr_ready: 0,
        error: 0,
        stopped: 0,
        held: 0,
      }
      for (const job of visibleJobs.value) {
        s.total += 1
        s[job.status] += 1
        // `held` count là subset của `pending` — job pending+held là "chờ
        // Start", pending+held=false là "trong queue chờ semaphore slot".
        if (job.status === 'pending' && job.held) {
          s.held += 1
        }
      }
      return s
    })

    // -------------------------------------------------------------------
    // Helpers nội bộ
    // -------------------------------------------------------------------
    function setErrorFromException(ex: unknown): void {
      if (ex instanceof JobsApiError) {
        error.value = ex.errorCode
          ? `${ex.errorCode}: ${ex.message}`
          : ex.message
        return
      }
      if (ex instanceof Error) {
        error.value = ex.message
        return
      }
      error.value = String(ex)
    }

    // -------------------------------------------------------------------
    // Actions — REST
    // -------------------------------------------------------------------
    /**
     * `GET /api/jobs` — lấy list compact, đồng bộ vào map:
     * - Job có trong response: merge compact fields, GIỮ lại detail fields
     *   (logs/artifact/error) đã fetch trước đó.
     * - Job không còn trong response: xoá khỏi map (backend đã dọn / job
     *   bị xoá ngoài phiên hiện tại).
     * - Job mới: thêm entry.
     */
    async function loadAll(): Promise<void> {
      loading.value = true
      error.value = null
      try {
        const res = await apiFetch('/jobs', { method: 'GET' })
        const list = (await res.json()) as JobViewCompactDTO[]

        const nextMap = new Map<string, JobViewModel>()
        for (const compact of list) {
          const merged = mergeCompact(jobs.value.get(compact.job_id), compact)
          nextMap.set(compact.job_id, merged)
        }
        jobs.value = nextMap
        // Prune side-Map cho các job đã bị backend cleanup (TTL 24h) hoặc
        // bulk delete từ client khác. Trước fix này, `rawAccountLines` /
        // `planStates` chỉ được xóa khi user explicit delete/rerun — job
        // biến khỏi `jobs` qua `loadAll` để lại entry orphan tồn tại vô
        // hạn định, tăng RAM steady-state theo số ngày chạy.
        for (const id of rawAccountLines.value.keys()) {
          if (!nextMap.has(id)) rawAccountLines.value.delete(id)
        }
        for (const id of planStates.value.keys()) {
          if (!nextMap.has(id)) planStates.value.delete(id)
        }
        if (focusedJobId.value !== null && !nextMap.has(focusedJobId.value)) {
          focusedJobId.value = null
        }
      } catch (ex) {
        setErrorFromException(ex)
        throw ex
      } finally {
        loading.value = false
      }
    }

    /**
     * Xử lý lỗi 404 `job_not_found` cho các operation theo `jobId`.
     *
     * Backend `_cleanup_old_terminal_jobs` (job terminal > 24h) hoặc
     * `delete_job` (user hoặc bulk) có thể xóa job khỏi state trong khi
     * FE vẫn giữ entry trong Map (VD LogPanel đang mở, background auto
     * loadDetail sau SSE reconnect). Khi FE gọi `/jobs/{id}` với ID
     * đã bị xóa → 404 → toast phiền.
     *
     * Semantic đúng: "job đã biến mất ở backend" == "coi như user đã
     * xóa cục bộ". Dọn Map ngay, KHÔNG set `error` (không show toast),
     * KHÔNG throw để caller (VD watch trong LogPanel) không nổ.
     *
     * Return `true` nếu đã xử lý là 404, caller nên `return` silently.
     */
    function handleJobNotFound(ex: unknown, jobId: string): boolean {
      if (ex instanceof JobsApiError && ex.status === 404) {
        jobs.value.delete(jobId)
        rawAccountLines.value.delete(jobId)
        planStates.value.delete(jobId)
        return true
      }
      return false
    }

    /**
     * `GET /api/jobs/{id}` — lấy detail đầy đủ (logs + artifact + error),
     * merge vào map. Nếu job chưa tồn tại trong map (ví dụ user paste
     * job_id thủ công), tạo entry mới từ detail.
     *
     * Fail-safe 404: nếu backend đã xóa job (cleanup TTL 24h hoặc user
     * bulk delete), `handleJobNotFound` dọn entry khỏi Map và trả sớm
     * KHÔNG throw — LogPanel/JobDetailPanel watch cứ tiếp diễn bình
     * thường, chỉ mất log của job đó (đã xóa dù sao cũng không cần).
     */
    async function loadDetail(jobId: string): Promise<void> {
      error.value = null
      try {
        const res = await apiFetch(`/jobs/${encodeURIComponent(jobId)}`, {
          method: 'GET',
        })
        const detail = (await res.json()) as JobViewDetailDTO
        jobs.value.set(jobId, fromDetail(detail))
      } catch (ex) {
        if (handleJobNotFound(ex, jobId)) return
        setErrorFromException(ex)
        throw ex
      }
    }

    /**
     * `POST /api/jobs` — submit batch. Cập nhật `skippedLastBatch` để
     * JobInputPanel hiển thị dòng bị skip kèm reason, sau đó `loadAll()`
     * để đồng bộ list mới nhất (bao gồm các job vừa tạo — có SSE cũng đẩy
     * `job_status: pending` nhưng loadAll là source of truth).
     */
    async function submitBatch(
      lines: string[],
      paymentMethod = 'ideal',
      options: { start?: boolean } = {},
    ): Promise<{ created: string[]; skipped: SkippedLine[] }> {
      loading.value = true
      error.value = null
      try {
        const res = await apiFetch('/jobs', {
          method: 'POST',
          body: JSON.stringify({
            payment_method: paymentMethod,
            lines,
            // Mặc định `start=true` (nút Run). Nút "+ Add" gọi với
            // `start=false` → job vào trạng thái `pending + held=true`,
            // scheduler bỏ qua đến khi user bấm Start.
            start: options.start ?? true,
          }),
        })
        const body = (await res.json()) as SubmitJobsResponseDTO
        skippedLastBatch.value = body.skipped ?? []
        // Map job_id → account_line thô cho panel Success (R11.5: chỉ
        // in-memory, không persist). Chỉ map được khi backend giữ đúng
        // thứ tự lines input với created_job_ids (contract R8.1). Nếu
        // response có skipped, mảng `lines` FE gửi và `created_job_ids`
        // sẽ có kích thước lệch nhau — dùng skipped để lọc line tương ứng.
        const createdIds = body.created_job_ids ?? []
        const skipReasons = new Set(
          (body.skipped ?? []).map((s) => s.line),
        )
        const acceptedLines = lines.filter((l) => !skipReasons.has(l))
        for (let i = 0; i < createdIds.length && i < acceptedLines.length; i++) {
          rawAccountLines.value.set(createdIds[i], acceptedLines[i])
        }
        // Sync list sau khi tạo — không đợi SSE để đảm bảo state đúng ngay
        // cả khi SSE chưa kết nối (task 28.4 chưa mount).
        await loadAll()
        return {
          created: body.created_job_ids ?? [],
          skipped: body.skipped ?? [],
        }
      } catch (ex) {
        setErrorFromException(ex)
        throw ex
      } finally {
        loading.value = false
      }
    }

    /**
     * `DELETE /api/jobs/{id}` — yêu cầu stop. Cập nhật status trong map
     * theo response (backend có thể trả `running` nếu handler chưa exit —
     * SSE `job_status: stopped` sẽ đẩy về khi thực sự dừng, `applyStatusEvent`
     * cập nhật tiếp).
     */
    async function stopJob(jobId: string): Promise<void> {
      error.value = null
      try {
        const res = await apiFetch(`/jobs/${encodeURIComponent(jobId)}`, {
          method: 'DELETE',
        })
        const body = (await res.json()) as StopJobResponseDTO
        const existing = jobs.value.get(jobId)
        if (existing) {
          existing.status = narrowJobStatus(body.status)
          // KHÔNG đổi updated_at ở đây — backend chưa broadcast SSE thì
          // updated_at server-side chưa đổi; giữ nguyên để tránh reorder
          // trong list. SSE sẽ đẩy updated_at chính xác khi handler exit.
        }
      } catch (ex) {
        if (handleJobNotFound(ex, jobId)) return
        setErrorFromException(ex)
        throw ex
      }
    }

    /**
     * `POST /api/jobs/{id}/start` — start 1 job đang `pending + held=true`.
     *
     * Optimistic update: clear `held` local ngay sau khi API OK để nút
     * Start biến mất tức thì mà không chờ SSE (server broadcast
     * `job_status` kèm `held=false` sau ~<200ms). `applyStatusEvent`
     * sau đó cứ merge patch — idempotent, không lặp side-effect.
     *
     * 409 `job_not_held`: FE hiển thị lỗi ngắn (có thể do race với auto-clear
     * lúc scheduler đã pick job vào cùng thời điểm). 404: handle như job
     * đã bị xóa.
     */
    async function startJob(jobId: string): Promise<void> {
      error.value = null
      try {
        await apiFetch(`/jobs/${encodeURIComponent(jobId)}/start`, {
          method: 'POST',
        })
        const existing = jobs.value.get(jobId)
        if (existing) {
          existing.held = false
        }
      } catch (ex) {
        if (handleJobNotFound(ex, jobId)) return
        setErrorFromException(ex)
        throw ex
      }
    }

    /**
     * `POST /api/jobs/start-all` — bulk start mọi job `pending + held=true`.
     *
     * KHÔNG optimistic clear per-job ở client — dựa vào SSE (server
     * broadcast per-job `job_status` khi mỗi job start xong) để merge
     * patch chính xác. Sau khi API trả về, chạy `loadAll` để bảo đảm
     * state list đồng bộ dù SSE có bị mất (safety net trong lúc mạng
     * dở).
     *
     * Trả `started_count` (số job vừa được start) — UI dùng toast xác nhận.
     */
    async function startAllHeld(): Promise<number> {
      error.value = null
      try {
        const res = await apiFetch('/jobs/start-all', { method: 'POST' })
        const body = (await res.json()) as { started_count?: number }
        await loadAll()
        return body.started_count ?? 0
      } catch (ex) {
        setErrorFromException(ex)
        throw ex
      }
    }

    /**
     * `POST /api/jobs/{id}/rerun` — chạy lại job (tạo job mới với cùng
     * account_line + payment_method). Response giống `submit_batch`.
     * Sau khi thành công, `loadAll()` để đồng bộ job mới vào map.
     */
    async function rerunJob(
      jobId: string,
    ): Promise<{ created: string[]; skipped: SkippedLine[] }> {
      error.value = null
      try {
        const res = await apiFetch(
          `/jobs/${encodeURIComponent(jobId)}/rerun`,
          { method: 'POST' },
        )
        const body = (await res.json()) as SubmitJobsResponseDTO
        skippedLastBatch.value = body.skipped ?? []
        await loadAll()
        return {
          created: body.created_job_ids ?? [],
          skipped: body.skipped ?? [],
        }
      } catch (ex) {
        if (handleJobNotFound(ex, jobId)) {
          return { created: [], skipped: [] }
        }
        setErrorFromException(ex)
        throw ex
      }
    }

    /**
     * Narrow raw `plan` từ backend (`Record<string, unknown>`) về union
     * hẹp để giữ type-safety khi assign vào `PlanState`. Backend contract:
     * `plan ∈ {'plus','free','unknown'}`; nếu trả giá trị lạ (phiên bản
     * mismatch) → coi là `'unknown'` để UI không crash.
     */
    function narrowPlan(raw: unknown): 'plus' | 'free' | 'unknown' {
      return raw === 'plus' || raw === 'free' ? raw : 'unknown'
    }

    /**
     * `POST /api/jobs/{id}/check-plan` — kiểm tra tài khoản đã lên Plus
     * chưa. Cập nhật `planStates[jobId]` với kết quả (hoặc trạng thái
     * `loading` khi đang gọi, `plan='unknown'` khi lỗi transport). Trả
     * lại `PlanState` cuối cùng để caller quyết định toast/log.
     *
     * Idempotent: bấm nhiều lần → cập nhật state gần nhất; không throw
     * ngay cả khi backend trả `plan='unknown'` + `error='no_session_cached'`
     * — UI dùng field `.error` trong PlanState để hiển thị.
     *
     * Tập trung mọi mutation của `planStates` TẠI ĐÂY để `checkPlanAll`,
     * JobList `handleCheckPlan`, SSE handler tương lai... không phải
     * duplicate logic set loading/error.
     */
    async function checkPlan(jobId: string): Promise<PlanState> {
      error.value = null
      planStates.value.set(jobId, { loading: true })
      try {
        const res = await apiFetch(
          `/jobs/${encodeURIComponent(jobId)}/check-plan`,
          { method: 'POST' },
        )
        const body = (await res.json()) as Record<string, unknown>
        const state: PlanState = {
          loading: false,
          plan: narrowPlan(body.plan),
          raw: typeof body.raw_plan === 'string' ? body.raw_plan : undefined,
          error: typeof body.error === 'string' ? body.error : undefined,
        }
        planStates.value.set(jobId, state)
        // Đồng bộ `job.plan` local ngay sau khi API OK — backend đã
        // persist DB và broadcast SSE, nhưng SSE có thể tới sau HTTP
        // response vài ms → nếu không mutate ngay, `plusJobs` computed
        // sẽ vẫn dựa vào `planStates` transient (đúng nhưng dễ mất khi
        // reload nếu user F5 trong khoảng chờ SSE).
        //
        // Chỉ ghi `plus`/`free` (khớp whitelist BE persist); `unknown`
        // = không thể xác định → giữ nguyên giá trị cũ để lần sau còn
        // cơ hội.
        if (state.plan === 'plus' || state.plan === 'free') {
          const existing = jobs.value.get(jobId)
          if (existing && existing.plan !== state.plan) {
            jobs.value.set(jobId, { ...existing, plan: state.plan })
          }
        }
        return state
      } catch (ex) {
        if (handleJobNotFound(ex, jobId)) {
          // Job đã bị xoá ngoài (cleanup TTL hoặc user bulk delete) —
          // dọn planStates cục bộ, không toast (đồng bộ với `handleJobNotFound`
          // vốn dọn `jobs`/`rawAccountLines`).
          planStates.value.delete(jobId)
          const state: PlanState = {
            loading: false,
            plan: 'unknown',
            error: 'job_deleted',
          }
          return state
        }
        const state: PlanState = {
          loading: false,
          plan: 'unknown',
          error: ex instanceof Error ? ex.message : String(ex),
        }
        planStates.value.set(jobId, state)
        setErrorFromException(ex)
        throw ex
      }
    }

    /**
     * Check plan cho TẤT CẢ job đang ở `qr_ready`. Chạy song song có
     * giới hạn concurrency để không hammer chatgpt.com.
     *
     * Tái sử dụng `checkPlan(jobId)` — không duplicate logic set state /
     * narrow plan. Bulk chỉ điều phối scheduling + tổng kết đếm.
     *
     * Concurrency = 5: cân bằng giữa (a) rút ngắn thời gian tổng khi
     * user có ~50 account thành công (10s thay vì 50s tuần tự) và (b)
     * tránh 429 từ ChatGPT edge (mỗi req ~500-800ms, 5 concurrent → RPS
     * ~8-10, dưới ngưỡng rate-limit thực tế đo được).
     *
     * KHÔNG raise dù có 1 vài job fail — trả `errors` count để caller
     * hiển thị message tổng kết ("N plus / M free / K lỗi").
     */
    async function checkPlanAll(): Promise<{
      plus: number
      free: number
      unknown: number
      errors: number
      total: number
      skipped_plus: number
    }> {
      const CONCURRENCY = 5
      // Snapshot danh sách target ngay tại thời điểm gọi — nếu trong lúc
      // chạy user submit job mới hoặc job đổi status, KHÔNG kéo theo
      // để tránh vòng lặp vô định.
      //
      // SKIP job đã có `plan === 'plus'` (yêu cầu 2026-07 "check plus
      // all bỏ qua tài khoản plus luôn"): tài khoản đã Plus rồi thì
      // không cần call ChatGPT lại — giảm rate-limit + tăng tốc bulk
      // khi số Plus lớn. Vẫn ĐẾM vào `plus` tally (để user thấy tổng
      // Plus hiện có), nhưng qua nhánh `skipped_plus` để phân biệt
      // "cache hit" và "vừa mới verify".
      //
      // Job `plan === 'free'` KHÔNG skip — free có thể lên plus theo
      // thời gian, user có thể chạy check để refresh.
      const targets: string[] = []
      let skippedPlus = 0
      for (const job of visibleJobs.value) {
        if (job.status !== 'qr_ready') continue
        if (job.plan === 'plus') {
          skippedPlus += 1
          continue
        }
        targets.push(job.job_id)
      }

      const tally = {
        plus: skippedPlus,
        free: 0,
        unknown: 0,
        errors: 0,
        total: targets.length + skippedPlus,
        skipped_plus: skippedPlus,
      }
      if (targets.length === 0) return tally

      let cursor = 0
      async function worker(): Promise<void> {
        while (cursor < targets.length) {
          const idx = cursor++
          const jobId = targets[idx]
          try {
            const state = await checkPlan(jobId)
            if (state.loading) {
              // Không bao giờ xảy ra sau `await`, nhưng cover exhaustive.
              tally.unknown += 1
            } else if (state.plan === 'plus') {
              tally.plus += 1
            } else if (state.plan === 'free') {
              tally.free += 1
            } else {
              tally.unknown += 1
            }
          } catch {
            // `checkPlan` đã set `planStates` với error state; chỉ đếm.
            tally.errors += 1
          }
        }
      }

      const workerCount = Math.min(CONCURRENCY, targets.length)
      const workers: Promise<void>[] = []
      for (let i = 0; i < workerCount; i++) workers.push(worker())
      await Promise.all(workers)
      return tally
    }

    /**
     * `DELETE /api/jobs/{id}/remove` — hard delete (xóa hoàn toàn khỏi list).
     * Khác với `stopJob`: KHÔNG chỉ dừng mà xóa entry khỏi backend state,
     * và loại khỏi Map ở FE ngay.
     */
    async function deleteJob(jobId: string): Promise<void> {
      error.value = null
      try {
        await apiFetch(`/jobs/${encodeURIComponent(jobId)}/remove`, {
          method: 'DELETE',
        })
        jobs.value.delete(jobId)
        rawAccountLines.value.delete(jobId)
        planStates.value.delete(jobId)
      } catch (ex) {
        // 404 = backend đã xóa job từ trước (double-delete hoặc cleanup
        // vừa dọn); coi như thành công + dọn Map cục bộ, không toast.
        if (handleJobNotFound(ex, jobId)) return
        setErrorFromException(ex)
        throw ex
      }
    }

    /**
     * `POST /api/jobs/stop-all` — dừng tất cả pending/running.
     * Sau khi thành công, `loadAll()` để sync trạng thái mới nhất.
     */
    async function stopAllJobs(): Promise<number> {
      error.value = null
      try {
        const res = await apiFetch('/jobs/stop-all', { method: 'POST' })
        const body = (await res.json()) as { stopped_count: number }
        await loadAll()
        return body.stopped_count ?? 0
      } catch (ex) {
        setErrorFromException(ex)
        throw ex
      }
    }

    /**
     * `POST /api/jobs/rerun-failed` — retry tất cả error/stopped.
     */
    async function rerunFailedJobs(): Promise<{
      created: string[]
      skipped: SkippedLine[]
    }> {
      error.value = null
      try {
        const res = await apiFetch('/jobs/rerun-failed', { method: 'POST' })
        const body = (await res.json()) as SubmitJobsResponseDTO
        skippedLastBatch.value = body.skipped ?? []
        await loadAll()
        return {
          created: body.created_job_ids ?? [],
          skipped: body.skipped ?? [],
        }
      } catch (ex) {
        setErrorFromException(ex)
        throw ex
      }
    }

    /**
     * `DELETE /api/jobs/clear?filter=<all|failed|completed|finished>` —
     * xóa hàng loạt theo filter.
     */
    function setAutoRetryFailedEnabled(enabled: boolean): void {
      autoRetryFailedEnabled.value = enabled
    }

    async function maybeAutoRetryFailed(): Promise<boolean> {
      if (!autoRetryFailedEnabled.value || autoRetryFailedInFlight) {
        return false
      }
      const stats = jobStats.value
      if (stats.running > 0) {
        return false
      }
      if (stats.error + stats.stopped === 0) {
        return false
      }

      autoRetryFailedInFlight = true
      try {
        const result = await rerunFailedJobs()
        return result.created.length > 0
      } finally {
        autoRetryFailedInFlight = false
      }
    }

    async function clearJobs(
      filter: 'all' | 'failed' | 'completed' | 'finished' = 'all',
    ): Promise<number> {
      error.value = null
      try {
        const res = await apiFetch(
          `/jobs/clear?filter=${encodeURIComponent(filter)}`,
          { method: 'DELETE' },
        )
        const body = (await res.json()) as {
          deleted_count: number
          deleted_ids: string[]
        }
        // Xóa khỏi Map ngay để UI phản hồi tức thì; loadAll không cần
        // vì backend đã xóa xong.
        for (const id of body.deleted_ids ?? []) {
          jobs.value.delete(id)
          rawAccountLines.value.delete(id)
          planStates.value.delete(id)
        }
        return body.deleted_count ?? 0
      } catch (ex) {
        setErrorFromException(ex)
        throw ex
      }
    }

    // -------------------------------------------------------------------
    // Actions — hide/unhide (Requirement 8.7, client-side only)
    // -------------------------------------------------------------------
    function hideJob(jobId: string): void {
      hiddenJobIds.value.add(jobId)
    }

    function unhideJob(jobId: string): void {
      hiddenJobIds.value.delete(jobId)
    }

    function isHidden(jobId: string): boolean {
      return hiddenJobIds.value.has(jobId)
    }

    function clearError(): void {
      error.value = null
    }

    // -------------------------------------------------------------------
    // Actions — SSE dispatch (task 28.4 useSse gọi)
    // -------------------------------------------------------------------
    /**
     * Nhận `job_status` event từ SSE. Cập nhật entry hiện có; nếu chưa có
     * entry (SSE tới trước loadAll), tạo entry tối thiểu để không mất
     * thông tin — payment_method/account_masked điền từ event nếu có, còn
     * thiếu thì để placeholder `'unknown'`/`'***'` cho đến khi `loadAll()`
     * hoặc `loadDetail()` populate đầy đủ.
     */
    function applyStatusEvent(evt: JobStatusEvent): void {
      // Backend `_broadcast_status` LUÔN include `updated_at` + `order`
      // trong payload (server-side timestamp chính xác). Fallback vẫn
      // giữ để defensive nếu 1 code path nào đó bỏ sót; ưu tiên value
      // từ event, fallback `existing.updated_at` (nếu có), cuối cùng
      // `Date.now()/1000` để tránh Invalid Date.
      const existing = jobs.value.get(evt.job_id)
      const safeUpdatedAt =
        typeof evt.updated_at === 'number' && evt.updated_at > 0
          ? evt.updated_at
          : (existing?.updated_at ?? Date.now() / 1000)

      // Clear stale `PlanState` khi job vào lại vòng đời chạy (pending /
      // running). Nếu không dọn, sau `rerun` job cũ có plan='plus' cũ →
      // khi flow chạy xong `qr_ready` → tự động rơi vào SuccessOutputPanel
      // mà user CHƯA re-verify entitlement. Semantic đúng: mỗi lần chạy
      // lại flow phải bấm Check Plus mới coi là thành công.
      if (
        (evt.status === 'pending' || evt.status === 'running') &&
        planStates.value.has(evt.job_id)
      ) {
        planStates.value.delete(evt.job_id)
      }

      if (existing) {
        // Rebuild entry (không mutate) → Map.set trigger reactive.
        // `order`: giữ nguyên `existing.order` nếu event không có
        // (một số path broadcast chưa gắn); ưu tiên `evt.order` nếu có.
        //
        // `started_at`/`finished_at`: cùng pattern — event luôn ĐI KÈM
        // giá trị mới nhất (kể cả null nghĩa là reset sau dedup reuse
        // trước RUNNING). Nếu event không set field (undefined) mới
        // giữ existing.
        //
        // `plan`: BE reset về `null` khi rerun (`submit_batch force_rerun`),
        // và set `"plus"`/`"free"` sau `check_plan_status`. SSE luôn kèm
        // giá trị hiện tại của record → BE là source of truth. Nếu
        // event undefined (BE cũ chưa broadcast), giữ existing.
        jobs.value.set(evt.job_id, {
          ...existing,
          status: evt.status,
          updated_at: safeUpdatedAt,
          error_code: evt.error_code !== undefined ? evt.error_code : existing.error_code,
          error_message: evt.error_message !== undefined ? evt.error_message : existing.error_message,
          artifact_path: evt.artifact_path !== undefined ? evt.artifact_path : existing.artifact_path,
          payment_link: evt.payment_link !== undefined ? evt.payment_link : existing.payment_link,
          payment_method: evt.payment_method || existing.payment_method,
          account_masked: evt.account_masked || existing.account_masked,
          order: typeof evt.order === 'number' ? evt.order : existing.order,
          retry_count:
            typeof evt.retry_count === 'number'
              ? evt.retry_count
              : existing.retry_count,
          started_at: evt.started_at !== undefined ? evt.started_at : existing.started_at,
          finished_at: evt.finished_at !== undefined ? evt.finished_at : existing.finished_at,
          // Server broadcast `held` sau `start_job` (chuyển true→false) và
          // khi scheduler picks job vào RUNNING (auto-clear). Server cũ
          // không có field này → giữ existing (undefined) — không phá
          // hành vi cũ.
          held: evt.held !== undefined ? evt.held : existing.held,
          plan: evt.plan !== undefined ? evt.plan : existing.plan,
        })
        return
      }

      jobs.value.set(evt.job_id, {
        job_id: evt.job_id,
        payment_method: evt.payment_method ?? 'unknown',
        account_masked: evt.account_masked ?? '***',
        status: evt.status,
        updated_at: safeUpdatedAt,
        error_code: evt.error_code ?? null,
        error_message: evt.error_message ?? null,
        artifact_path: evt.artifact_path ?? null,
        payment_link: evt.payment_link ?? null,
        order: typeof evt.order === 'number' ? evt.order : undefined,
        retry_count:
          typeof evt.retry_count === 'number' ? evt.retry_count : 0,
        started_at: evt.started_at ?? null,
        finished_at: evt.finished_at ?? null,
        held: evt.held ?? false,
        plan: evt.plan ?? null,
      })
    }

    /**
     * Nhận `job_log` event từ SSE. Append log entry vào buffer job. Nếu job
     * chưa tồn tại trong map (SSE tới trước loadAll), tạo shell với status
     * `'running'` (giả định hợp lý — chỉ job running mới sinh log) để
     * không mất log; `loadAll()`/`loadDetail()` sẽ đồng bộ đúng trạng thái
     * sau đó.
     */
    function applyLogEvent(evt: JobLogEvent): void {
      const { job_id, ts, message, ...extraRaw } = evt
      // Fallback ts nếu backend không set (defensive — sau khi SseLogHandler
      // thêm ts vào payload, mọi log_event phải có ts hợp lệ).
      const safeTs = typeof ts === 'number' && ts > 0 ? ts : Date.now() / 1000
      const existing = jobs.value.get(job_id)
      const isFocused = focusedJobId.value === job_id

      // Non-focused job: DROP log entry hoàn toàn (chỉ bump updated_at nếu
      // event mới hơn). Đây là fix RAM chính — trước đây mọi job đang chạy
      // đều tích trữ 500 log × N job trong RAM dù user chỉ xem 1 job. Backend
      // đã persist log SQLite → khi user chọn job, LogPanel gọi `loadDetail`
      // để lấy log đầy đủ. Trade-off chấp nhận: log realtime chỉ hiển thị
      // cho job đang focus; job khác cập nhật status vẫn realtime.
      if (!isFocused) {
        if (existing) {
          if (safeTs > existing.updated_at) {
            jobs.value.set(job_id, { ...existing, updated_at: safeTs })
          }
          return
        }
        // Shell entry cho SSE log tới trước loadAll — KHÔNG giữ log entry
        // để không phá invariant "non-focused = không có logs[]".
        jobs.value.set(job_id, {
          job_id,
          payment_method: 'unknown',
          account_masked: '***',
          status: 'running',
          updated_at: safeTs,
          artifact_path: null,
          error_code: null,
          error_message: null,
        })
        return
      }

      // Focused job: giữ log ring-buffer + cap `extra` payload size để
      // 1 log entry lỗi không nuốt hàng MB (VD stacktrace hoặc HTTP body).
      const entry: JobLogEntry = {
        ts: safeTs,
        message,
        extra: capExtraPayload(extraRaw as Record<string, unknown>),
      }
      if (existing) {
        const nextLogs = [...(existing.logs ?? []), entry].slice(
          -MAX_LOG_ENTRIES_PER_JOB,
        )
        jobs.value.set(job_id, {
          ...existing,
          logs: nextLogs,
          updated_at: safeTs > existing.updated_at ? safeTs : existing.updated_at,
        })
        return
      }

      jobs.value.set(job_id, {
        job_id,
        payment_method: 'unknown',
        account_masked: '***',
        status: 'running',
        updated_at: safeTs,
        logs: [entry],
        artifact_path: null,
        error_code: null,
        error_message: null,
      })
    }

    /**
     * Nhận `job_notified` event từ SSE — append 1 entry vào cuối
     * `job.telegram_notifications` và trigger reactive update.
     *
     * Job không tồn tại trong Map → no-op (KHÔNG tạo shell entry — nếu
     * BE broadcast notify cho job đã bị FE delete cục bộ, silent bỏ qua
     * tương tự pattern `handleJobNotFound`).
     */
    function applyNotifiedEvent(evt: JobNotifiedEvent): void {
      const existing = jobs.value.get(evt.job_id)
      if (!existing) {
        return
      }
      // Ring-buffer: cap notification history theo job — pattern giống
      // MAX_LOG_ENTRIES_PER_JOB để tránh RAM tăng vô hạn khi 1 job success
      // fanout nhiều chat + retry.
      const nextEntries = [
        ...(existing.telegram_notifications ?? []),
        evt.entry,
      ].slice(-MAX_TELEGRAM_NOTIFICATIONS_PER_JOB)
      jobs.value.set(evt.job_id, {
        ...existing,
        telegram_notifications: nextEntries,
      })
    }

    /**
     * Đổi job đang focus (LogPanel/JobDetailPanel đang mở). Khi user click
     * row khác trong JobList, App.vue watch selectedJobId → gọi hàm này.
     *
     * Side-effect quan trọng cho RAM: giải phóng `logs[]` của job cũ (được
     * populate bởi `applyLogEvent` khi nó đang focus). Job cũ sau đó chỉ
     * giữ compact fields; muốn xem log lại phải mở panel → LogPanel tự
     * `loadDetail()` fetch từ backend.
     *
     * Không set `logs = []` mà `delete` field để giữ semantics "chưa load
     * detail" — LogPanel biết cần loadDetail (hiện tại nó auto load mỗi
     * lần jobId đổi, nên field logs bị bỏ trống không phải vấn đề).
     */
    function setFocusedJobId(jobId: string | null): void {
      const oldId = focusedJobId.value
      if (oldId === jobId) return
      focusedJobId.value = jobId
      if (oldId !== null && oldId !== jobId) {
        const oldJob = jobs.value.get(oldId)
        if (oldJob && oldJob.logs !== undefined) {
          const { logs: _logs, ...rest } = oldJob
          void _logs
          jobs.value.set(oldId, rest as JobViewModel)
        }
      }
    }

    /**
     * Reset toàn bộ state — dùng cho logout / test / khi user đổi
     * environment. KHÔNG gọi backend.
     */
    function reset(): void {
      jobs.value = new Map<string, JobViewModel>()
      hiddenJobIds.value = new Set<string>()
      skippedLastBatch.value = []
      rawAccountLines.value = new Map<string, string>()
      planStates.value = new Map<string, PlanState>()
      focusedJobId.value = null
      loading.value = false
      error.value = null
    }

    return {
      jobs,
      hiddenJobIds,
      skippedLastBatch,
      loading,
      error,
      autoRetryFailedEnabled,
      rawAccountLines,
      planStates,
      focusedJobId,
      visibleJobs,
      plusJobs,
      freeJobs,
      noFreeOfferJobs,
      jobStats,
      loadAll,
      loadDetail,
      submitBatch,
      startJob,
      startAllHeld,
      stopJob,
      rerunJob,
      checkPlan,
      checkPlanAll,
      deleteJob,
      stopAllJobs,
      rerunFailedJobs,
      setAutoRetryFailedEnabled,
      maybeAutoRetryFailed,
      clearJobs,
      hideJob,
      unhideJob,
      isHidden,
      clearError,
      applyStatusEvent,
      applyLogEvent,
      applyNotifiedEvent,
      setFocusedJobId,
      reset,
    }
  })
