<!--
  SettingsAccordion — panel cấu hình Settings_Store.

  Layout:
  - Nhóm CƠ BẢN (mở mặc định): các setting user thay đổi thường xuyên
    (concurrency, issuer, auto-retry, session cache toggle).
  - Nhóm NÂNG CAO (collapse): setting hiếm khi đụng (refresh poll, Stripe
    timeout/retry, device profile JSON, danh sách issuer, retry code
    whitelist, session cache TTL).
  Chia ra để 90% user chỉ cần scroll 1 màn hình là tìm được setting cần.

  Commit-after-confirm (R12.7): `drafts` là buffer nội bộ; chỉ ghi Settings
  Store khi user bấm "Lưu tất cả". Save thành công thì clear drafts, fail
  thì giữ nguyên.

  Class BẮT BUỘC giữ (test snapshot):
    .settings-accordion (root)
    .settings-accordion__toggle (button, aria-expanded)
    .settings-accordion__body (chỉ khi open)
-->
<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import {
  NAlert,
  NButton,
  NCheckbox,
  NIcon,
  NInput,
  NInputNumber,
  NModal,
  NSelect,
  NSpace,
  NText,
} from 'naive-ui'

import { useSettingsStore } from '../composables/useSettingsStore'
import IssuerSelect from './IssuerSelect.vue'
import {
  IconAdjustments,
  IconSettings,
  IconTrash,
} from '../icons'

const SETTING_KEYS = Object.freeze({
  IDEAL_DEFAULT_ISSUER: 'ideal.default_issuer',
  IDEAL_KNOWN_ISSUERS: 'ideal.known_issuers',
  IDEAL_MAX_CONCURRENT: 'ideal.max_concurrent',
  IDEAL_REFRESH_POLL_MAX_ATTEMPTS: 'ideal.refresh_poll_max_attempts',
  IDEAL_REFRESH_POLL_DELAY_SECONDS: 'ideal.refresh_poll_delay_seconds',
  IDEAL_STRIPE_REQUEST_TIMEOUT_SECONDS: 'ideal.stripe_request_timeout_seconds',
  IDEAL_STRIPE_MAX_RETRY_ATTEMPTS: 'ideal.stripe_max_retry_attempts',
  IDEAL_STRIPE_RETRY_BACKOFF_SECONDS: 'ideal.stripe_retry_backoff_seconds',
  IDEAL_DEVICE_PROFILES: 'ideal.device_profiles',
  IDEAL_AUTO_RETRY_ENABLED: 'ideal.auto_retry_blocked_enabled',
  IDEAL_AUTO_RETRY_MAX: 'ideal.auto_retry_blocked_max',
  IDEAL_AUTO_RETRY_DELAY: 'ideal.auto_retry_blocked_delay_seconds',
  IDEAL_AUTO_RETRY_CODES: 'ideal.auto_retry_blocked_codes',
  IDEAL_AUTO_RETRY_MODE: 'ideal.auto_retry_mode',
  IDEAL_AUTO_RETRY_FAILED_TO_PENDING: 'ideal.auto_retry_failed_to_pending',
  UPI_LICENSE_CODES: 'upi.license_codes',
  UPI_MAX_CONCURRENT: 'upi.max_concurrent',
  UPI_RUN_TIMEOUT_SECONDS: 'upi.run_timeout_seconds',
  UPI_ELIGIBILITY_PRECHECK: 'upi.eligibility_precheck',
  OAIPAY_BASE_URL: 'oaipay.base_url',
  OAIPAY_PROXY_CHECKOUT: 'oaipay.proxy_checkout',
  OAIPAY_PROXY_PROMOTION: 'oaipay.proxy_promotion',
  OAIPAY_YESCAPTCHA_API_KEY: 'oaipay.yescaptcha_api_key',
  OAIPAY_MAX_CONCURRENT: 'oaipay.max_concurrent',
  OAIPAY_RUN_TIMEOUT_SECONDS: 'oaipay.run_timeout_seconds',
  UPI_DIRECT_PROXY_CHECKOUT: 'upi_direct.proxy_checkout',
  UPI_DIRECT_PROXY_PROMOTION: 'upi_direct.proxy_promotion',
  UPI_DIRECT_MAX_CONCURRENT: 'upi_direct.max_concurrent',
  UPI_DIRECT_RUN_TIMEOUT_SECONDS: 'upi_direct.run_timeout_seconds',
  UPI_DIRECT_STRIPE_REQUEST_TIMEOUT: 'upi_direct.stripe_request_timeout_seconds',
  UPI_DIRECT_APPROVE_ERROR_RETRIES: 'upi_direct.approve_error_retries',
  UPI_DIRECT_REQUIRE_PROMO: 'upi_direct.require_promo',
  KAKAO_DIRECT_PROXY_CHECKOUT: 'kakao_direct.proxy_checkout',
  KAKAO_DIRECT_PROXY_PROMOTION: 'kakao_direct.proxy_promotion',
  KAKAO_DIRECT_MAX_CONCURRENT: 'kakao_direct.max_concurrent',
  KAKAO_DIRECT_RUN_TIMEOUT_SECONDS: 'kakao_direct.run_timeout_seconds',
  KAKAO_DIRECT_STRIPE_REQUEST_TIMEOUT: 'kakao_direct.stripe_request_timeout_seconds',
  KAKAO_DIRECT_APPROVE_ERROR_RETRIES: 'kakao_direct.approve_error_retries',
  KAKAO_DIRECT_REQUIRE_PROMO: 'kakao_direct.require_promo',
  GCASH_DIRECT_PROXY_CHECKOUT: 'gcash_direct.proxy_checkout',
  GCASH_DIRECT_PROXY_PROMOTION: 'gcash_direct.proxy_promotion',
  GCASH_DIRECT_MAX_CONCURRENT: 'gcash_direct.max_concurrent',
  GCASH_DIRECT_RUN_TIMEOUT_SECONDS: 'gcash_direct.run_timeout_seconds',
  GCASH_DIRECT_STRIPE_REQUEST_TIMEOUT: 'gcash_direct.stripe_request_timeout_seconds',
  GCASH_DIRECT_APPROVE_ERROR_RETRIES: 'gcash_direct.approve_error_retries',
  GCASH_DIRECT_REQUIRE_PROMO: 'gcash_direct.require_promo',
  GCASH_DIRECT_BROWSER_QR_ENABLED: 'gcash_direct.browser_qr_enabled',
  GCASH_DIRECT_BROWSER_QR_CAPTURE_ENABLED: 'gcash_direct.browser_qr_capture_enabled',
  GCASH_DIRECT_BROWSER_QR_TIMEOUT_SECONDS: 'gcash_direct.browser_qr_timeout_seconds',
  GCASH_DIRECT_BROWSER_HOLD_SECONDS: 'gcash_direct.browser_hold_seconds',
  GCASH_DIRECT_BROWSER_HOLD_MAX_ACTIVE: 'gcash_direct.browser_hold_max_active',
  GCASH_DIRECT_BROWSER_HEADLESS: 'gcash_direct.browser_headless',
  GCASH_DIRECT_BROWSER_USE_PROXY: 'gcash_direct.browser_use_proxy',
  MOMO_CHECK_PROXY_CHECKOUT: 'momo_check.proxy_checkout',
  MOMO_CHECK_PROXY_PROMOTION: 'momo_check.proxy_promotion',
  MOMO_CHECK_MAX_CONCURRENT: 'momo_check.max_concurrent',
  MOMO_CHECK_RUN_TIMEOUT_SECONDS: 'momo_check.run_timeout_seconds',
  MOMO_CHECK_STRIPE_REQUEST_TIMEOUT: 'momo_check.stripe_request_timeout_seconds',
  MOMO_CHECK_REQUIRE_PROMO: 'momo_check.require_promo',
  SESSION_CACHE_ENABLED: 'session_cache.enabled',
  SESSION_CACHE_TTL_HOURS: 'session_cache.ttl_hours',
  UI_AUTO_CHECK_PLUS_ALL_ENABLED: 'ui.auto_check_plus_all_enabled',
  UI_AUTO_CHECK_PLUS_ALL_INTERVAL_SECONDS: 'ui.auto_check_plus_all_interval_seconds',
} as const)

const RETRY_MODE_OPTIONS = [
  {
    label: 'Move to next account (round-robin)',
    value: 'round_robin',
  },
  {
    label: 'Retry same account (with delay)',
    value: 'same_account',
  },
]

const settingsStore = useSettingsStore()

const isOpen = ref<boolean>(false)

const drafts = reactive<Record<string, unknown>>({})
const deviceProfilesJsonError = ref<string>('')
const savingAll = ref<boolean>(false)

function currentValue<T>(key: string, fallback: T): T {
  // QUAN TRỌNG: dùng `key in drafts` — Vue 3 Proxy có `has` trap track
  // operator này. `Object.prototype.hasOwnProperty.call(...)` KHÔNG đi
  // qua Proxy trap nên getter sẽ KHÔNG re-eval khi drafts[key] được
  // set/delete → bug "gõ input không đổi" (setter chạy nhưng UI vẫn
  // hiển thị value cũ).
  if (key in drafts) return drafts[key] as T
  const stored = settingsStore.settings[key]
  return (stored === undefined ? fallback : stored) as T
}

// ---- iDEAL bindings --------------------------------------------------------
const defaultIssuer = computed<string>({
  get: () => currentValue<string>(SETTING_KEYS.IDEAL_DEFAULT_ISSUER, ''),
  set: (v) => { drafts[SETTING_KEYS.IDEAL_DEFAULT_ISSUER] = v },
})

const knownIssuers = computed<string[]>({
  get: () => currentValue<string[]>(SETTING_KEYS.IDEAL_KNOWN_ISSUERS, []),
  set: (v) => { drafts[SETTING_KEYS.IDEAL_KNOWN_ISSUERS] = v },
})

const knownIssuersText = computed<string>({
  get: () => knownIssuers.value.join('\n'),
  set: (v) => {
    const lines = v.split('\n').map((l) => l.trim()).filter((l) => l.length > 0)
    drafts[SETTING_KEYS.IDEAL_KNOWN_ISSUERS] = lines
  },
})

/**
 * Helper đưa vào setter của mọi NInputNumber binding: khi user xóa hết
 * input (naive-ui emit `null`) HOẶC gõ ký tự lạ (emit `undefined`),
 * setter DELETE draft entry thay vì set = 0 hay NaN.
 *
 * Root cause bug "gõ nhảy về giá trị cũ":
 *   1. Ban đầu value = 20 (từ Settings Store).
 *   2. User select-all + gõ "5":
 *      - Emit null trước (input transient rỗng).
 *      - Nếu setter set `drafts[key] = Number(null) = 0` → getter trả 0.
 *      - NInputNumber :min="1" reject 0 → revert prop value về 20.
 *      - Emit "5" tiếp theo có thể lỡ nhịp render.
 *   3. Kết quả: user gõ mà UI cứ nhảy về value cũ.
 *
 * Fix: guard null/undefined/NaN → delete draft (revert về stored) thay
 * vì tạo giá trị 0/NaN không hợp lệ.
 */
function setNumberDraft(key: string, v: number | null | undefined): void {
  if (v === null || v === undefined) {
    delete drafts[key]
    return
  }
  const num = Number(v)
  if (Number.isNaN(num)) {
    delete drafts[key]
    return
  }
  drafts[key] = num
}

const maxConcurrent = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.IDEAL_MAX_CONCURRENT, 5),
  set: (v) => setNumberDraft(SETTING_KEYS.IDEAL_MAX_CONCURRENT, v),
})

const refreshPollMaxAttempts = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.IDEAL_REFRESH_POLL_MAX_ATTEMPTS, 10),
  set: (v) => setNumberDraft(SETTING_KEYS.IDEAL_REFRESH_POLL_MAX_ATTEMPTS, v),
})

const refreshPollDelaySeconds = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.IDEAL_REFRESH_POLL_DELAY_SECONDS, 1),
  set: (v) => setNumberDraft(SETTING_KEYS.IDEAL_REFRESH_POLL_DELAY_SECONDS, v),
})

const stripeRequestTimeoutSeconds = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.IDEAL_STRIPE_REQUEST_TIMEOUT_SECONDS, 30),
  set: (v) => setNumberDraft(SETTING_KEYS.IDEAL_STRIPE_REQUEST_TIMEOUT_SECONDS, v),
})

const stripeMaxRetryAttempts = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.IDEAL_STRIPE_MAX_RETRY_ATTEMPTS, 3),
  set: (v) => setNumberDraft(SETTING_KEYS.IDEAL_STRIPE_MAX_RETRY_ATTEMPTS, v),
})

const stripeRetryBackoffSeconds = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.IDEAL_STRIPE_RETRY_BACKOFF_SECONDS, 0.5),
  set: (v) => setNumberDraft(SETTING_KEYS.IDEAL_STRIPE_RETRY_BACKOFF_SECONDS, v),
})

// ---- Auto-retry bindings ---------------------------------------------------
const autoRetryEnabled = computed<boolean>({
  get: () => currentValue<boolean>(SETTING_KEYS.IDEAL_AUTO_RETRY_ENABLED, true),
  set: (v) => { drafts[SETTING_KEYS.IDEAL_AUTO_RETRY_ENABLED] = v },
})

const autoRetryMode = computed<string>({
  get: () => currentValue<string>(SETTING_KEYS.IDEAL_AUTO_RETRY_MODE, 'round_robin'),
  set: (v) => { drafts[SETTING_KEYS.IDEAL_AUTO_RETRY_MODE] = v },
})

const autoRetryMax = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.IDEAL_AUTO_RETRY_MAX, 3),
  set: (v) => setNumberDraft(SETTING_KEYS.IDEAL_AUTO_RETRY_MAX, v),
})

/**
 * `failed_to_pending`: sau khi 1 job hết `autoRetryMax` lượt vẫn fail,
 * ON = job quay về PENDING (reset counter) → scheduler nhặt vòng mới;
 * OFF = job kết thúc ERROR, user tự bấm "Retry failed" bulk. Default
 * ON tương ứng backend seed default.
 */
const autoRetryFailedToPending = computed<boolean>({
  get: () =>
    currentValue<boolean>(SETTING_KEYS.IDEAL_AUTO_RETRY_FAILED_TO_PENDING, true),
  set: (v) => {
    drafts[SETTING_KEYS.IDEAL_AUTO_RETRY_FAILED_TO_PENDING] = v
  },
})

const autoRetryDelaySeconds = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.IDEAL_AUTO_RETRY_DELAY, 15),
  set: (v) => setNumberDraft(SETTING_KEYS.IDEAL_AUTO_RETRY_DELAY, v),
})

const autoRetryCodes = computed<string[]>({
  get: () => currentValue<string[]>(SETTING_KEYS.IDEAL_AUTO_RETRY_CODES, []),
  set: (v) => { drafts[SETTING_KEYS.IDEAL_AUTO_RETRY_CODES] = v },
})

const autoRetryCodesText = computed<string>({
  get: () => autoRetryCodes.value.join('\n'),
  set: (v) => {
    const lines = v.split('\n').map((l) => l.trim()).filter((l) => l.length > 0)
    drafts[SETTING_KEYS.IDEAL_AUTO_RETRY_CODES] = lines
  },
})

// Delay field chỉ hữu ích cho mode `same_account`; ẩn khi round_robin
// để giảm noise (round_robin không dùng delay).
const showDelayField = computed<boolean>(
  () => autoRetryMode.value === 'same_account',
)

// ---- Device profiles JSON --------------------------------------------------
const deviceProfilesText = ref<string>('')

function syncDeviceProfilesText(): void {
  const value = settingsStore.settings[SETTING_KEYS.IDEAL_DEVICE_PROFILES]
  deviceProfilesText.value = Array.isArray(value)
    ? JSON.stringify(value, null, 2)
    : '[]'
}

function handleDeviceProfilesInput(v: string): void {
  deviceProfilesText.value = v
  try {
    const parsed = JSON.parse(v)
    if (!Array.isArray(parsed)) {
      deviceProfilesJsonError.value = 'device_profiles must be a JSON array'
      delete drafts[SETTING_KEYS.IDEAL_DEVICE_PROFILES]
      return
    }
    deviceProfilesJsonError.value = ''
    drafts[SETTING_KEYS.IDEAL_DEVICE_PROFILES] = parsed
  } catch (err) {
    deviceProfilesJsonError.value = `Invalid JSON: ${(err as Error).message}`
    delete drafts[SETTING_KEYS.IDEAL_DEVICE_PROFILES]
  }
}

// ---- UPI bindings ----------------------------------------------------------
// License codes lưu dạng list-of-string ở backend (`upi.license_codes`);
// textarea edit 1 mã / dòng, mirror `knownIssuersText`. Trim + drop dòng rỗng
// để không đẩy chuỗi trắng vào pool.
const upiLicenseCodes = computed<string[]>({
  get: () => currentValue<string[]>(SETTING_KEYS.UPI_LICENSE_CODES, []),
  set: (v) => { drafts[SETTING_KEYS.UPI_LICENSE_CODES] = v },
})

const upiLicenseCodesText = computed<string>({
  get: () => upiLicenseCodes.value.join('\n'),
  set: (v) => {
    const lines = v.split('\n').map((l) => l.trim()).filter((l) => l.length > 0)
    drafts[SETTING_KEYS.UPI_LICENSE_CODES] = lines
  },
})

const upiMaxConcurrent = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.UPI_MAX_CONCURRENT, 3),
  set: (v) => setNumberDraft(SETTING_KEYS.UPI_MAX_CONCURRENT, v),
})

const upiRunTimeoutSeconds = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.UPI_RUN_TIMEOUT_SECONDS, 120),
  set: (v) => setNumberDraft(SETTING_KEYS.UPI_RUN_TIMEOUT_SECONDS, v),
})

const upiEligibilityPrecheck = computed<boolean>({
  get: () => currentValue<boolean>(SETTING_KEYS.UPI_ELIGIBILITY_PRECHECK, false),
  set: (v) => { drafts[SETTING_KEYS.UPI_ELIGIBILITY_PRECHECK] = v },
})

// ---- OaiPay (UPI no CDK) bindings ------------------------------------------
const oaipayBaseUrl = computed<string>({
  get: () => currentValue<string>(SETTING_KEYS.OAIPAY_BASE_URL, ''),
  set: (v) => { drafts[SETTING_KEYS.OAIPAY_BASE_URL] = v },
})

const oaipayProxyCheckout = computed<string[]>({
  get: () => currentValue<string[]>(SETTING_KEYS.OAIPAY_PROXY_CHECKOUT, []),
  set: (v) => { drafts[SETTING_KEYS.OAIPAY_PROXY_CHECKOUT] = v },
})

const oaipayProxyCheckoutText = computed<string>({
  get: () => oaipayProxyCheckout.value.join('\n'),
  set: (v) => {
    const lines = v.split('\n').map((l) => l.trim()).filter((l) => l.length > 0)
    drafts[SETTING_KEYS.OAIPAY_PROXY_CHECKOUT] = lines
  },
})

const oaipayProxyPromotion = computed<string[]>({
  get: () => currentValue<string[]>(SETTING_KEYS.OAIPAY_PROXY_PROMOTION, []),
  set: (v) => { drafts[SETTING_KEYS.OAIPAY_PROXY_PROMOTION] = v },
})

const oaipayProxyPromotionText = computed<string>({
  get: () => oaipayProxyPromotion.value.join('\n'),
  set: (v) => {
    const lines = v.split('\n').map((l) => l.trim()).filter((l) => l.length > 0)
    drafts[SETTING_KEYS.OAIPAY_PROXY_PROMOTION] = lines
  },
})

const oaipayYescaptchaApiKey = computed<string>({
  get: () => currentValue<string>(SETTING_KEYS.OAIPAY_YESCAPTCHA_API_KEY, ''),
  set: (v) => { drafts[SETTING_KEYS.OAIPAY_YESCAPTCHA_API_KEY] = v },
})

const oaipayMaxConcurrent = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.OAIPAY_MAX_CONCURRENT, 3),
  set: (v) => setNumberDraft(SETTING_KEYS.OAIPAY_MAX_CONCURRENT, v),
})

const oaipayRunTimeoutSeconds = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.OAIPAY_RUN_TIMEOUT_SECONDS, 300),
  set: (v) => setNumberDraft(SETTING_KEYS.OAIPAY_RUN_TIMEOUT_SECONDS, v),
})

// ---- UPI Direct (in-house ChatGPT+Stripe) bindings -------------------------
const upiDirectProxyCheckout = computed<string[]>({
  get: () => currentValue<string[]>(SETTING_KEYS.UPI_DIRECT_PROXY_CHECKOUT, []),
  set: (v) => { drafts[SETTING_KEYS.UPI_DIRECT_PROXY_CHECKOUT] = v },
})

const upiDirectProxyCheckoutText = computed<string>({
  get: () => upiDirectProxyCheckout.value.join('\n'),
  set: (v) => {
    const lines = v.split('\n').map((l) => l.trim()).filter((l) => l.length > 0)
    drafts[SETTING_KEYS.UPI_DIRECT_PROXY_CHECKOUT] = lines
  },
})

const upiDirectProxyPromotion = computed<string[]>({
  get: () => currentValue<string[]>(SETTING_KEYS.UPI_DIRECT_PROXY_PROMOTION, []),
  set: (v) => { drafts[SETTING_KEYS.UPI_DIRECT_PROXY_PROMOTION] = v },
})

const upiDirectProxyPromotionText = computed<string>({
  get: () => upiDirectProxyPromotion.value.join('\n'),
  set: (v) => {
    const lines = v.split('\n').map((l) => l.trim()).filter((l) => l.length > 0)
    drafts[SETTING_KEYS.UPI_DIRECT_PROXY_PROMOTION] = lines
  },
})

const upiDirectMaxConcurrent = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.UPI_DIRECT_MAX_CONCURRENT, 3),
  set: (v) => setNumberDraft(SETTING_KEYS.UPI_DIRECT_MAX_CONCURRENT, v),
})

const upiDirectRunTimeoutSeconds = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.UPI_DIRECT_RUN_TIMEOUT_SECONDS, 300),
  set: (v) => setNumberDraft(SETTING_KEYS.UPI_DIRECT_RUN_TIMEOUT_SECONDS, v),
})

const upiDirectStripeRequestTimeout = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.UPI_DIRECT_STRIPE_REQUEST_TIMEOUT, 30),
  set: (v) => setNumberDraft(SETTING_KEYS.UPI_DIRECT_STRIPE_REQUEST_TIMEOUT, v),
})

const upiDirectApproveErrorRetries = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.UPI_DIRECT_APPROVE_ERROR_RETRIES, 1),
  set: (v) => setNumberDraft(SETTING_KEYS.UPI_DIRECT_APPROVE_ERROR_RETRIES, v),
})

const upiDirectRequirePromo = computed<boolean>({
  get: () => currentValue<boolean>(SETTING_KEYS.UPI_DIRECT_REQUIRE_PROMO, true),
  set: (v) => { drafts[SETTING_KEYS.UPI_DIRECT_REQUIRE_PROMO] = v },
})

// ---- Kakao Direct (ChatGPT+Stripe Kakao Pay link) bindings -----------------
const kakaoDirectProxyCheckout = computed<string[]>({
  get: () => currentValue<string[]>(SETTING_KEYS.KAKAO_DIRECT_PROXY_CHECKOUT, []),
  set: (v) => { drafts[SETTING_KEYS.KAKAO_DIRECT_PROXY_CHECKOUT] = v },
})

const kakaoDirectProxyCheckoutText = computed<string>({
  get: () => kakaoDirectProxyCheckout.value.join('\n'),
  set: (v) => {
    const lines = v.split('\n').map((l) => l.trim()).filter((l) => l.length > 0)
    drafts[SETTING_KEYS.KAKAO_DIRECT_PROXY_CHECKOUT] = lines
  },
})

const kakaoDirectProxyPromotion = computed<string[]>({
  get: () => currentValue<string[]>(SETTING_KEYS.KAKAO_DIRECT_PROXY_PROMOTION, []),
  set: (v) => { drafts[SETTING_KEYS.KAKAO_DIRECT_PROXY_PROMOTION] = v },
})

const kakaoDirectProxyPromotionText = computed<string>({
  get: () => kakaoDirectProxyPromotion.value.join('\n'),
  set: (v) => {
    const lines = v.split('\n').map((l) => l.trim()).filter((l) => l.length > 0)
    drafts[SETTING_KEYS.KAKAO_DIRECT_PROXY_PROMOTION] = lines
  },
})

const kakaoDirectMaxConcurrent = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.KAKAO_DIRECT_MAX_CONCURRENT, 3),
  set: (v) => setNumberDraft(SETTING_KEYS.KAKAO_DIRECT_MAX_CONCURRENT, v),
})

const kakaoDirectRunTimeoutSeconds = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.KAKAO_DIRECT_RUN_TIMEOUT_SECONDS, 300),
  set: (v) => setNumberDraft(SETTING_KEYS.KAKAO_DIRECT_RUN_TIMEOUT_SECONDS, v),
})

const kakaoDirectStripeRequestTimeout = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.KAKAO_DIRECT_STRIPE_REQUEST_TIMEOUT, 30),
  set: (v) => setNumberDraft(SETTING_KEYS.KAKAO_DIRECT_STRIPE_REQUEST_TIMEOUT, v),
})

const kakaoDirectApproveErrorRetries = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.KAKAO_DIRECT_APPROVE_ERROR_RETRIES, 1),
  set: (v) => setNumberDraft(SETTING_KEYS.KAKAO_DIRECT_APPROVE_ERROR_RETRIES, v),
})

const kakaoDirectRequirePromo = computed<boolean>({
  get: () => currentValue<boolean>(SETTING_KEYS.KAKAO_DIRECT_REQUIRE_PROMO, true),
  set: (v) => { drafts[SETTING_KEYS.KAKAO_DIRECT_REQUIRE_PROMO] = v },
})

// ---- GCash Direct (ChatGPT+Stripe GCash link) bindings ---------------------
const gcashDirectProxyCheckout = computed<string[]>({
  get: () => currentValue<string[]>(SETTING_KEYS.GCASH_DIRECT_PROXY_CHECKOUT, []),
  set: (v) => { drafts[SETTING_KEYS.GCASH_DIRECT_PROXY_CHECKOUT] = v },
})

const gcashDirectProxyCheckoutText = computed<string>({
  get: () => gcashDirectProxyCheckout.value.join('\n'),
  set: (v) => {
    const lines = v.split('\n').map((l) => l.trim()).filter((l) => l.length > 0)
    drafts[SETTING_KEYS.GCASH_DIRECT_PROXY_CHECKOUT] = lines
  },
})

const gcashDirectProxyPromotion = computed<string[]>({
  get: () => currentValue<string[]>(SETTING_KEYS.GCASH_DIRECT_PROXY_PROMOTION, []),
  set: (v) => { drafts[SETTING_KEYS.GCASH_DIRECT_PROXY_PROMOTION] = v },
})

const gcashDirectProxyPromotionText = computed<string>({
  get: () => gcashDirectProxyPromotion.value.join('\n'),
  set: (v) => {
    const lines = v.split('\n').map((l) => l.trim()).filter((l) => l.length > 0)
    drafts[SETTING_KEYS.GCASH_DIRECT_PROXY_PROMOTION] = lines
  },
})

const gcashDirectMaxConcurrent = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.GCASH_DIRECT_MAX_CONCURRENT, 3),
  set: (v) => setNumberDraft(SETTING_KEYS.GCASH_DIRECT_MAX_CONCURRENT, v),
})

const gcashDirectRunTimeoutSeconds = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.GCASH_DIRECT_RUN_TIMEOUT_SECONDS, 300),
  set: (v) => setNumberDraft(SETTING_KEYS.GCASH_DIRECT_RUN_TIMEOUT_SECONDS, v),
})

const gcashDirectStripeRequestTimeout = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.GCASH_DIRECT_STRIPE_REQUEST_TIMEOUT, 30),
  set: (v) => setNumberDraft(SETTING_KEYS.GCASH_DIRECT_STRIPE_REQUEST_TIMEOUT, v),
})

const gcashDirectApproveErrorRetries = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.GCASH_DIRECT_APPROVE_ERROR_RETRIES, 1),
  set: (v) => setNumberDraft(SETTING_KEYS.GCASH_DIRECT_APPROVE_ERROR_RETRIES, v),
})

const gcashDirectRequirePromo = computed<boolean>({
  get: () => currentValue<boolean>(SETTING_KEYS.GCASH_DIRECT_REQUIRE_PROMO, true),
  set: (v) => { drafts[SETTING_KEYS.GCASH_DIRECT_REQUIRE_PROMO] = v },
})

const gcashDirectBrowserQrEnabled = computed<boolean>({
  get: () => currentValue<boolean>(SETTING_KEYS.GCASH_DIRECT_BROWSER_QR_ENABLED, true),
  set: (v) => { drafts[SETTING_KEYS.GCASH_DIRECT_BROWSER_QR_ENABLED] = v },
})

const gcashDirectBrowserQrCaptureEnabled = computed<boolean>({
  get: () => currentValue<boolean>(
    SETTING_KEYS.GCASH_DIRECT_BROWSER_QR_CAPTURE_ENABLED,
    true,
  ),
  set: (v) => { drafts[SETTING_KEYS.GCASH_DIRECT_BROWSER_QR_CAPTURE_ENABLED] = v },
})

const gcashDirectBrowserQrTimeoutSeconds = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.GCASH_DIRECT_BROWSER_QR_TIMEOUT_SECONDS, 35),
  set: (v) => setNumberDraft(SETTING_KEYS.GCASH_DIRECT_BROWSER_QR_TIMEOUT_SECONDS, v),
})

const gcashDirectBrowserHoldSeconds = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.GCASH_DIRECT_BROWSER_HOLD_SECONDS, 300),
  set: (v) => setNumberDraft(SETTING_KEYS.GCASH_DIRECT_BROWSER_HOLD_SECONDS, v),
})

const gcashDirectBrowserHoldMaxActive = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.GCASH_DIRECT_BROWSER_HOLD_MAX_ACTIVE, 5),
  set: (v) => setNumberDraft(SETTING_KEYS.GCASH_DIRECT_BROWSER_HOLD_MAX_ACTIVE, v),
})

const gcashDirectBrowserHeadless = computed<boolean>({
  get: () => currentValue<boolean>(SETTING_KEYS.GCASH_DIRECT_BROWSER_HEADLESS, true),
  set: (v) => { drafts[SETTING_KEYS.GCASH_DIRECT_BROWSER_HEADLESS] = v },
})

const gcashDirectBrowserUseProxy = computed<boolean>({
  get: () => currentValue<boolean>(SETTING_KEYS.GCASH_DIRECT_BROWSER_USE_PROXY, false),
  set: (v) => { drafts[SETTING_KEYS.GCASH_DIRECT_BROWSER_USE_PROXY] = v },
})

// ---- MoMo check bindings ---------------------------------------------------
const momoCheckProxyCheckout = computed<string[]>({
  get: () => currentValue<string[]>(SETTING_KEYS.MOMO_CHECK_PROXY_CHECKOUT, []),
  set: (v) => { drafts[SETTING_KEYS.MOMO_CHECK_PROXY_CHECKOUT] = v },
})

const momoCheckProxyCheckoutText = computed<string>({
  get: () => momoCheckProxyCheckout.value.join('\n'),
  set: (v) => {
    const lines = v.split('\n').map((l) => l.trim()).filter((l) => l.length > 0)
    drafts[SETTING_KEYS.MOMO_CHECK_PROXY_CHECKOUT] = lines
  },
})

const momoCheckProxyPromotion = computed<string[]>({
  get: () => currentValue<string[]>(SETTING_KEYS.MOMO_CHECK_PROXY_PROMOTION, []),
  set: (v) => { drafts[SETTING_KEYS.MOMO_CHECK_PROXY_PROMOTION] = v },
})

const momoCheckProxyPromotionText = computed<string>({
  get: () => momoCheckProxyPromotion.value.join('\n'),
  set: (v) => {
    const lines = v.split('\n').map((l) => l.trim()).filter((l) => l.length > 0)
    drafts[SETTING_KEYS.MOMO_CHECK_PROXY_PROMOTION] = lines
  },
})

const momoCheckMaxConcurrent = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.MOMO_CHECK_MAX_CONCURRENT, 5),
  set: (v) => setNumberDraft(SETTING_KEYS.MOMO_CHECK_MAX_CONCURRENT, v),
})

const momoCheckRunTimeoutSeconds = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.MOMO_CHECK_RUN_TIMEOUT_SECONDS, 300),
  set: (v) => setNumberDraft(SETTING_KEYS.MOMO_CHECK_RUN_TIMEOUT_SECONDS, v),
})

const momoCheckStripeRequestTimeout = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.MOMO_CHECK_STRIPE_REQUEST_TIMEOUT, 30),
  set: (v) => setNumberDraft(SETTING_KEYS.MOMO_CHECK_STRIPE_REQUEST_TIMEOUT, v),
})

const momoCheckRequirePromo = computed<boolean>({
  get: () => currentValue<boolean>(SETTING_KEYS.MOMO_CHECK_REQUIRE_PROMO, true),
  set: (v) => { drafts[SETTING_KEYS.MOMO_CHECK_REQUIRE_PROMO] = v },
})

// ---- Session cache bindings ------------------------------------------------
const sessionCacheEnabled = computed<boolean>({
  get: () => currentValue<boolean>(SETTING_KEYS.SESSION_CACHE_ENABLED, true),
  set: (v) => { drafts[SETTING_KEYS.SESSION_CACHE_ENABLED] = v },
})

const sessionCacheTtlHours = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.SESSION_CACHE_TTL_HOURS, 24),
  set: (v) => setNumberDraft(SETTING_KEYS.SESSION_CACHE_TTL_HOURS, v),
})

const clearingSessionCache = ref<boolean>(false)
const sessionCacheActionError = ref<string>('')
const sessionCacheActionNotice = ref<string>('')

async function clearSessionCache(): Promise<void> {
  if (clearingSessionCache.value) return
  sessionCacheActionError.value = ''
  sessionCacheActionNotice.value = ''
  if (!window.confirm('Clear all cached sessions?')) return

  clearingSessionCache.value = true
  try {
    const res = await fetch('/api/session-cache', { method: 'DELETE' })
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`)
    }
    sessionCacheActionNotice.value = 'Cached sessions cleared.'
  } catch (err) {
    sessionCacheActionError.value = `Failed to clear session cache: ${(err as Error).message}`
  } finally {
    clearingSessionCache.value = false
  }
}

// ---- Auto Check Plus All bindings ------------------------------------------
// Bật/tắt: FE tự setInterval gọi bulk check-plan (JobList.vue). Default `false`
// để giữ backward-compat với user chưa bật tính năng này.
const autoCheckPlusAllEnabled = computed<boolean>({
  get: () => currentValue<boolean>(SETTING_KEYS.UI_AUTO_CHECK_PLUS_ALL_ENABLED, false),
  set: (v) => { drafts[SETTING_KEYS.UI_AUTO_CHECK_PLUS_ALL_ENABLED] = v },
})

// Interval (giây) — default 60. Backend constraint: [5, 3600].
const autoCheckPlusAllIntervalSeconds = computed<number>({
  get: () => currentValue<number>(SETTING_KEYS.UI_AUTO_CHECK_PLUS_ALL_INTERVAL_SECONDS, 60),
  set: (v) => setNumberDraft(SETTING_KEYS.UI_AUTO_CHECK_PLUS_ALL_INTERVAL_SECONDS, v),
})

// ---- Actions ---------------------------------------------------------------
async function handleSaveAll(): Promise<void> {
  if (savingAll.value || Object.keys(drafts).length === 0) return
  savingAll.value = true
  try {
    const payload: Record<string, unknown> = { ...drafts }
    const ok = await settingsStore.bulkUpdate(payload)
    if (ok) {
      for (const k of Object.keys(payload)) delete drafts[k]
      if (Object.prototype.hasOwnProperty.call(payload, SETTING_KEYS.IDEAL_DEVICE_PROFILES)) {
        syncDeviceProfilesText()
      }
    }
  } finally {
    savingAll.value = false
  }
}

function handleDismissError(): void {
  settingsStore.clearError()
}

const hasDrafts = computed<boolean>(() => Object.keys(drafts).length > 0)
const draftCount = computed<number>(() => Object.keys(drafts).length)

onMounted(async () => {
  await settingsStore.loadAll()
  syncDeviceProfilesText()
})
</script>

<template>
  <section class="settings-accordion" aria-label="Settings configuration">
    <button
      type="button"
      class="settings-accordion__toggle"
      :aria-expanded="isOpen"
      :title="hasDrafts ? `${draftCount} unsaved change(s)` : 'Open settings'"
      @click="isOpen = true"
    >
      <n-icon :component="IconSettings" size="15" class="toggle-icon" />
      <span class="toggle-label">Settings</span>
      <span
        v-if="hasDrafts"
        class="toggle-badge"
        :aria-label="`${draftCount} unsaved change(s)`"
      >
        {{ draftCount }}
      </span>
    </button>

    <n-modal
      v-model:show="isOpen"
      preset="card"
      title="Settings"
      class="settings-modal"
      :bordered="false"
      :segmented="{ content: 'soft', footer: 'soft' }"
      :closable="true"
      :mask-closable="false"
      :close-on-esc="true"
      style="width: 640px; max-width: 95vw"
    >
      <div class="settings-accordion__body">
        <n-alert
          v-if="settingsStore.error"
          type="error"
          size="small"
          :show-icon="false"
        >
          <template #header>
            <strong class="mono">{{ settingsStore.error.key }}</strong>
          </template>
          <div class="alert-body">
            <span>{{ settingsStore.error.reason }}</span>
            <n-button size="tiny" quaternary @click="handleDismissError">
              Close
            </n-button>
          </div>
        </n-alert>

        <!-- ═══════════════ BASIC ═══════════════ -->
        <fieldset class="group">
          <legend class="group__legend">
            <n-icon :component="IconAdjustments" size="14" />
            <span>Job execution</span>
          </legend>

          <div class="grid-2">
            <div class="field">
              <label>Concurrent jobs</label>
              <n-input-number
                v-model:value="maxConcurrent"
                :min="1"
                :max="200"
                :show-button="false"
                placeholder="5"
              />
            </div>
            <div class="field">
              <label>Default bank</label>
              <IssuerSelect
                v-model="defaultIssuer"
                :known-issuers="knownIssuers"
              />
            </div>
          </div>
        </fieldset>

        <fieldset class="group">
          <legend class="group__legend">
            <n-icon :component="IconAdjustments" size="14" />
            <span>MoMo check</span>
          </legend>

          <div class="field">
            <label for="setting-momo-check-proxy-checkout">
              Checkout proxy pool A (one per line)
            </label>
            <n-input
              id="setting-momo-check-proxy-checkout"
              v-model:value="momoCheckProxyCheckoutText"
              type="textarea"
              class="mono"
              placeholder="socks5://user:pass@host:port"
              :autosize="{ minRows: 3, maxRows: 8 }"
              spellcheck="false"
            />
            <p class="field-hint field-hint--tight">
              Tuy chon: rong se chay direct; neu co proxy VN thi tool se dung cho checkout VN/VND va Stripe elements.
            </p>
          </div>

          <div class="field">
            <label for="setting-momo-check-proxy-promotion">
              Promotion proxy pool B (one per line)
            </label>
            <n-input
              id="setting-momo-check-proxy-promotion"
              v-model:value="momoCheckProxyPromotionText"
              type="textarea"
              class="mono"
              placeholder="socks5://user:pass@host:port"
              :autosize="{ minRows: 3, maxRows: 8 }"
              spellcheck="false"
            />
            <p class="field-hint field-hint--tight">
              Tuy chon: pool B chi cho promo update. Rong se skip promo co log.
            </p>
          </div>

          <div class="grid-2">
            <div class="field">
              <label>Max concurrent</label>
              <n-input-number
                v-model:value="momoCheckMaxConcurrent"
                :min="1"
                :max="50"
                :show-button="false"
                placeholder="5"
              />
            </div>
            <div class="field">
              <label>Run timeout (seconds)</label>
              <n-input-number
                v-model:value="momoCheckRunTimeoutSeconds"
                :min="5"
                :max="3600"
                :show-button="false"
                placeholder="300"
              />
            </div>
          </div>

          <div class="grid-2">
            <div class="field">
              <label>Stripe request timeout (s)</label>
              <n-input-number
                v-model:value="momoCheckStripeRequestTimeout"
                :min="1"
                :max="120"
                :show-button="false"
                placeholder="30"
              />
            </div>
            <div class="field field--checkbox field--checkbox-inline">
              <n-checkbox v-model:checked="momoCheckRequirePromo">
                Require free promo (amount 0 VND)
              </n-checkbox>
            </div>
          </div>
        </fieldset>

        <fieldset class="group">
          <legend class="group__legend">
            <n-icon :component="IconAdjustments" size="14" />
            <span>Auto-retry on transient errors</span>
          </legend>

          <div class="field field--checkbox">
            <n-checkbox v-model:checked="autoRetryEnabled">
              Enable auto-retry
            </n-checkbox>
          </div>

          <div class="field">
            <label>Retry mode</label>
            <n-select
              v-model:value="autoRetryMode"
              :options="RETRY_MODE_OPTIONS"
              :disabled="!autoRetryEnabled"
            />
            <p class="field-hint">
              <strong>Round-robin</strong>: failed job is pushed to the
              end of the queue, next account runs, and it comes back
              around after the list. <strong>Same account</strong>:
              sleep then retry the same account with increasing backoff.
            </p>
          </div>

          <div class="grid-2">
            <div class="field">
              <label>Max retry attempts</label>
              <n-input-number
                v-model:value="autoRetryMax"
                :min="1"
                :max="1000"
                :show-button="false"
                :disabled="!autoRetryEnabled"
                placeholder="3"
              />
            </div>
            <div v-if="showDelayField" class="field">
              <label>Base delay (seconds)</label>
              <n-input-number
                v-model:value="autoRetryDelaySeconds"
                :min="5"
                :max="300"
                :step="5"
                :show-button="false"
                :disabled="!autoRetryEnabled"
                placeholder="15"
              />
            </div>
          </div>

          <div class="field field--checkbox">
            <n-checkbox
              v-model:checked="autoRetryFailedToPending"
              :disabled="!autoRetryEnabled"
            >
              Loop back to <em>pending</em> after max retries
            </n-checkbox>
            <p class="field-hint field-hint--tight">
              On: cycles forever until success. Off: ends in <em>error</em>.
            </p>
          </div>
        </fieldset>

        <fieldset class="group">
          <legend class="group__legend">
            <n-icon :component="IconAdjustments" size="14" />
            <span>Session cache</span>
          </legend>

          <div class="field field--checkbox">
            <n-checkbox v-model:checked="sessionCacheEnabled">
              Enable session cache
            </n-checkbox>
            <p class="field-hint field-hint--tight">
              Avoid re-login between runs by reusing cached cookies.
            </p>
          </div>

          <div class="field">
            <n-button
              type="error"
              secondary
              size="small"
              :loading="clearingSessionCache"
              @click="clearSessionCache"
            >
              <template #icon>
                <n-icon :component="IconTrash" size="14" />
              </template>
              Clear cached sessions
            </n-button>
            <p class="field-hint field-hint--tight">
              Deletes all cached ChatGPT sessions on disk.
            </p>
            <p v-if="sessionCacheActionNotice" class="field-hint field-hint--tight">
              {{ sessionCacheActionNotice }}
            </p>
            <p v-if="sessionCacheActionError" class="field-error">
              {{ sessionCacheActionError }}
            </p>
          </div>
        </fieldset>

        <fieldset class="group">
          <legend class="group__legend">
            <n-icon :component="IconAdjustments" size="14" />
            <span>UPI</span>
          </legend>

          <div class="field">
            <label for="setting-upi-license-codes">
              License codes (one PK-XXXX code per line)
            </label>
            <n-input
              id="setting-upi-license-codes"
              v-model:value="upiLicenseCodesText"
              type="textarea"
              class="mono"
              placeholder="PK-XXXXXXXX&#10;PK-YYYYYYYY"
              :autosize="{ minRows: 4, maxRows: 10 }"
              spellcheck="false"
            />
          </div>

          <div class="grid-2">
            <div class="field">
              <label>Max concurrent</label>
              <n-input-number
                v-model:value="upiMaxConcurrent"
                :min="1"
                :max="10"
                :show-button="false"
                placeholder="3"
              />
            </div>
            <div class="field">
              <label>Run timeout (seconds)</label>
              <n-input-number
                v-model:value="upiRunTimeoutSeconds"
                :min="1"
                :max="3600"
                :show-button="false"
                placeholder="120"
              />
            </div>
          </div>

          <div class="field field--checkbox">
            <n-checkbox v-model:checked="upiEligibilityPrecheck">
              Eligibility precheck
            </n-checkbox>
          </div>
        </fieldset>

        <fieldset class="group">
          <legend class="group__legend">
            <n-icon :component="IconAdjustments" size="14" />
            <span>UPI (no CDK)</span>
          </legend>

          <div class="field">
            <label for="setting-oaipay-base-url">Base URL</label>
            <n-input
              id="setting-oaipay-base-url"
              v-model:value="oaipayBaseUrl"
              class="mono"
              placeholder="https://oaipay.12001234.xyz"
              spellcheck="false"
            />
          </div>

          <div class="field">
            <label for="setting-oaipay-proxy-checkout">
              Checkout proxy pool (one per line)
            </label>
            <n-input
              id="setting-oaipay-proxy-checkout"
              v-model:value="oaipayProxyCheckoutText"
              type="textarea"
              class="mono"
              placeholder="socks5://user:pass@host:port"
              :autosize="{ minRows: 3, maxRows: 8 }"
              spellcheck="false"
            />
            <p class="field-hint field-hint--tight">
              Bắt buộc: cả hai pool checkout + promotion. Đây là proxy RIÊNG cho
              OaiPay, khác với pool proxy chung của tool.
            </p>
          </div>

          <div class="field">
            <label for="setting-oaipay-proxy-promotion">
              Promotion proxy pool (one per line)
            </label>
            <n-input
              id="setting-oaipay-proxy-promotion"
              v-model:value="oaipayProxyPromotionText"
              type="textarea"
              class="mono"
              placeholder="socks5://user:pass@host:port"
              :autosize="{ minRows: 3, maxRows: 8 }"
              spellcheck="false"
            />
            <p class="field-hint field-hint--tight">
              Bắt buộc: cả hai pool checkout + promotion. Đây là proxy RIÊNG cho
              OaiPay, khác với pool proxy chung của tool.
            </p>
          </div>

          <div class="field">
            <label for="setting-oaipay-yescaptcha-api-key">YesCaptcha API key</label>
            <n-input
              id="setting-oaipay-yescaptcha-api-key"
              v-model:value="oaipayYescaptchaApiKey"
              type="password"
              show-password-on="click"
              class="mono"
              placeholder="YesCaptcha client key"
              spellcheck="false"
            />
          </div>

          <div class="grid-2">
            <div class="field">
              <label>Max concurrent</label>
              <n-input-number
                v-model:value="oaipayMaxConcurrent"
                :min="1"
                :max="10"
                :show-button="false"
                placeholder="3"
              />
            </div>
            <div class="field">
              <label>Run timeout (seconds)</label>
              <n-input-number
                v-model:value="oaipayRunTimeoutSeconds"
                :min="1"
                :max="3600"
                :show-button="false"
                placeholder="300"
              />
            </div>
          </div>
        </fieldset>

        <fieldset class="group">
          <legend class="group__legend">
            <n-icon :component="IconAdjustments" size="14" />
            <span>UPI direct</span>
          </legend>

          <div class="field">
            <label for="setting-upi-direct-proxy-checkout">
              Checkout proxy pool A (one per line)
            </label>
            <n-input
              id="setting-upi-direct-proxy-checkout"
              v-model:value="upiDirectProxyCheckoutText"
              type="textarea"
              class="mono"
              placeholder="socks5://user:pass@host:port"
              :autosize="{ minRows: 3, maxRows: 8 }"
              spellcheck="false"
            />
            <p class="field-hint field-hint--tight">
              Bắt buộc cho UPI direct: pool A dùng checkout/Stripe/approve/QR.
              Proxy RIÊNG, khác pool chung của tool.
            </p>
          </div>

          <div class="field">
            <label for="setting-upi-direct-proxy-promotion">
              Promotion proxy pool B (one per line)
            </label>
            <n-input
              id="setting-upi-direct-proxy-promotion"
              v-model:value="upiDirectProxyPromotionText"
              type="textarea"
              class="mono"
              placeholder="socks5://user:pass@host:port"
              :autosize="{ minRows: 3, maxRows: 8 }"
              spellcheck="false"
            />
            <p class="field-hint field-hint--tight">
              Tùy chọn: pool B chỉ cho promo update. Rỗng → skip promo có log.
            </p>
          </div>

          <div class="grid-2">
            <div class="field">
              <label>Max concurrent</label>
              <n-input-number
                v-model:value="upiDirectMaxConcurrent"
                :min="1"
                :max="50"
                :show-button="false"
                placeholder="3"
              />
            </div>
            <div class="field">
              <label>Run timeout (seconds)</label>
              <n-input-number
                v-model:value="upiDirectRunTimeoutSeconds"
                :min="5"
                :max="3600"
                :show-button="false"
                placeholder="300"
              />
            </div>
          </div>

          <div class="grid-2">
            <div class="field">
              <label>Stripe request timeout (s)</label>
              <n-input-number
                v-model:value="upiDirectStripeRequestTimeout"
                :min="1"
                :max="120"
                :show-button="false"
                placeholder="30"
              />
            </div>
            <div class="field">
              <label>Approve error calls</label>
              <n-input-number
                id="setting-upi-direct-approve-error-retries"
                v-model:value="upiDirectApproveErrorRetries"
                :min="1"
                :max="50"
                :show-button="false"
                placeholder="1"
              />
            </div>
          </div>

          <div class="field field--checkbox">
            <n-checkbox v-model:checked="upiDirectRequirePromo">
              Require free promo (amount 0 INR)
            </n-checkbox>
          </div>
        </fieldset>

        <fieldset class="group">
          <legend class="group__legend">
            <n-icon :component="IconAdjustments" size="14" />
            <span>Kakao direct</span>
          </legend>

          <div class="field">
            <label for="setting-kakao-direct-proxy-checkout">
              Checkout proxy pool A (one per line)
            </label>
            <n-input
              id="setting-kakao-direct-proxy-checkout"
              v-model:value="kakaoDirectProxyCheckoutText"
              type="textarea"
              class="mono"
              placeholder="socks5://user:pass@host:port"
              :autosize="{ minRows: 3, maxRows: 8 }"
              spellcheck="false"
            />
            <p class="field-hint field-hint--tight">
              Required for Kakao direct: pool A handles checkout/Stripe/approve/link.
            </p>
          </div>

          <div class="field">
            <label for="setting-kakao-direct-proxy-promotion">
              Promotion proxy pool B (one per line)
            </label>
            <n-input
              id="setting-kakao-direct-proxy-promotion"
              v-model:value="kakaoDirectProxyPromotionText"
              type="textarea"
              class="mono"
              placeholder="socks5://user:pass@host:port"
              :autosize="{ minRows: 3, maxRows: 8 }"
              spellcheck="false"
            />
            <p class="field-hint field-hint--tight">
              Optional: pool B only runs promo update. Empty means skip with log.
            </p>
          </div>

          <div class="grid-2">
            <div class="field">
              <label>Max concurrent</label>
              <n-input-number
                v-model:value="kakaoDirectMaxConcurrent"
                :min="1"
                :max="50"
                :show-button="false"
                placeholder="3"
              />
            </div>
            <div class="field">
              <label>Run timeout (seconds)</label>
              <n-input-number
                v-model:value="kakaoDirectRunTimeoutSeconds"
                :min="5"
                :max="3600"
                :show-button="false"
                placeholder="300"
              />
            </div>
          </div>

          <div class="grid-2">
            <div class="field">
              <label>Stripe request timeout (s)</label>
              <n-input-number
                v-model:value="kakaoDirectStripeRequestTimeout"
                :min="1"
                :max="120"
                :show-button="false"
                placeholder="30"
              />
            </div>
            <div class="field">
              <label>Approve error calls</label>
              <n-input-number
                v-model:value="kakaoDirectApproveErrorRetries"
                :min="1"
                :max="50"
                :show-button="false"
                placeholder="1"
              />
            </div>
          </div>

          <div class="field field--checkbox">
            <n-checkbox v-model:checked="kakaoDirectRequirePromo">
              Require free promo (amount 0 KRW)
            </n-checkbox>
          </div>
        </fieldset>

        <fieldset class="group">
          <legend class="group__legend">
            <n-icon :component="IconAdjustments" size="14" />
            <span>GCash direct</span>
          </legend>

          <div class="field">
            <label for="setting-gcash-direct-proxy-checkout">
              Checkout proxy pool A (one per line)
            </label>
            <n-input
              id="setting-gcash-direct-proxy-checkout"
              v-model:value="gcashDirectProxyCheckoutText"
              type="textarea"
              class="mono"
              placeholder="socks5://user:pass@host:port"
              :autosize="{ minRows: 3, maxRows: 8 }"
              spellcheck="false"
            />
            <p class="field-hint field-hint--tight">
              Pool A is used for GCash confirm/retry only. Blank uses direct/VPN for confirm.
            </p>
          </div>

          <div class="field">
            <label for="setting-gcash-direct-proxy-promotion">
              Promotion proxy pool B (one per line)
            </label>
            <n-input
              id="setting-gcash-direct-proxy-promotion"
              v-model:value="gcashDirectProxyPromotionText"
              type="textarea"
              class="mono"
              placeholder="socks5://user:pass@host:port"
              :autosize="{ minRows: 3, maxRows: 8 }"
              spellcheck="false"
            />
            <p class="field-hint field-hint--tight">
              Pool B is the main create/promo path. Blank uses the current job proxy or direct/VPN.
            </p>
          </div>

          <div class="grid-2">
            <div class="field">
              <label>Max concurrent</label>
              <n-input-number
                v-model:value="gcashDirectMaxConcurrent"
                :min="1"
                :max="50"
                :show-button="false"
                placeholder="3"
              />
            </div>
            <div class="field">
              <label>Run timeout (seconds)</label>
              <n-input-number
                v-model:value="gcashDirectRunTimeoutSeconds"
                :min="5"
                :max="3600"
                :show-button="false"
                placeholder="300"
              />
            </div>
          </div>

          <div class="grid-2">
            <div class="field">
              <label>Stripe request timeout (s)</label>
              <n-input-number
                v-model:value="gcashDirectStripeRequestTimeout"
                :min="1"
                :max="120"
                :show-button="false"
                placeholder="30"
              />
            </div>
            <div class="field">
              <label>Approve error calls</label>
              <n-input-number
                v-model:value="gcashDirectApproveErrorRetries"
                :min="1"
                :max="50"
                :show-button="false"
                placeholder="1"
              />
            </div>
          </div>

          <div class="field field--checkbox">
            <n-checkbox v-model:checked="gcashDirectRequirePromo">
              Require free promo (amount 0 PHP)
            </n-checkbox>
          </div>

          <div class="grid-2">
            <div class="field field--checkbox">
              <n-checkbox v-model:checked="gcashDirectBrowserQrEnabled">
                Resolve GCash link in browser session
              </n-checkbox>
            </div>
            <div class="field field--checkbox">
              <n-checkbox
                v-model:checked="gcashDirectBrowserHeadless"
                :disabled="!gcashDirectBrowserQrEnabled"
              >
                Headless browser
              </n-checkbox>
            </div>
          </div>

          <div class="field field--checkbox">
            <n-checkbox
              v-model:checked="gcashDirectBrowserQrCaptureEnabled"
              :disabled="!gcashDirectBrowserQrEnabled"
            >
              Capture QR from #qrcode image
            </n-checkbox>
            <p class="field-hint field-hint--tight">
              Reads the PNG data URI rendered by the GCash checkout page.
            </p>
          </div>

          <div class="field field--checkbox">
            <n-checkbox
              v-model:checked="gcashDirectBrowserUseProxy"
              :disabled="!gcashDirectBrowserQrEnabled"
            >
              Use promotion proxy for browser QR
            </n-checkbox>
            <p class="field-hint field-hint--tight">
              Default off: open the payment link in the local ChatGPT browser session.
            </p>
          </div>

          <div class="field">
            <label>Browser QR timeout (seconds)</label>
            <n-input-number
              v-model:value="gcashDirectBrowserQrTimeoutSeconds"
              :min="5"
              :max="120"
              :show-button="false"
              :disabled="!gcashDirectBrowserQrEnabled"
              placeholder="35"
            />
          </div>

          <div class="field">
            <label>Keep browser alive after QR (seconds)</label>
            <n-input-number
              v-model:value="gcashDirectBrowserHoldSeconds"
              :min="0"
              :max="1800"
              :step="30"
              :show-button="false"
              :disabled="!gcashDirectBrowserQrEnabled"
              placeholder="300"
            />
          </div>

          <div class="field">
            <label>Max live checkout browsers</label>
            <n-input-number
              v-model:value="gcashDirectBrowserHoldMaxActive"
              :min="0"
              :max="100"
              :show-button="false"
              :disabled="!gcashDirectBrowserQrEnabled"
              placeholder="5"
            />
          </div>
        </fieldset>

        <fieldset class="group">
          <legend class="group__legend">
            <n-icon :component="IconAdjustments" size="14" />
            <span>Auto check Plus All</span>
          </legend>

          <div class="field field--checkbox">
            <n-checkbox v-model:checked="autoCheckPlusAllEnabled">
              Enable auto reload Check Plus All
            </n-checkbox>
            <p class="field-hint field-hint--tight">
              Tự chạy "Check Plus All" theo chu kỳ khi có job qr_ready.
              Tab ẩn hoặc đang chạy bulk check → skip vòng đó.
            </p>
          </div>

          <div class="field">
            <label>Interval (seconds)</label>
            <n-input-number
              v-model:value="autoCheckPlusAllIntervalSeconds"
              :min="5"
              :max="3600"
              :step="5"
              :show-button="false"
              :disabled="!autoCheckPlusAllEnabled"
              placeholder="60"
            />
          </div>
        </fieldset>

        <!-- ═══════════════ NÂNG CAO ═══════════════ -->
        <!--
          Dùng <details> HTML native — không state Vue, không JS, gọn nhất.
          Mặc định collapsed vì user 90% không đụng tới các setting bên
          trong. Mở ra khi cần fine-tune anti-fraud / debug Stripe retry.
        -->
        <details class="advanced">
          <summary class="advanced__summary">
            <n-icon :component="IconAdjustments" size="14" />
            <span>Advanced</span>
            <span class="advanced__hint">
              Refresh poll · Stripe retry · Device profile · Session TTL · Retry codes · Issuer list
            </span>
          </summary>

          <div class="advanced__body">
            <fieldset class="group">
              <legend class="group__legend">
                <span>Stripe · Refresh poll</span>
              </legend>
              <div class="grid-2">
                <div class="field">
                  <label>Refresh poll · max attempts</label>
                  <n-input-number
                    v-model:value="refreshPollMaxAttempts"
                    :min="1"
                    :show-button="false"
                    placeholder="20"
                  />
                </div>
                <div class="field">
                  <label>Refresh poll · delay (seconds)</label>
                  <n-input-number
                    v-model:value="refreshPollDelaySeconds"
                    :min="0.1"
                    :step="0.1"
                    :show-button="false"
                    placeholder="1"
                  />
                </div>
                <div class="field">
                  <label>Stripe timeout (seconds)</label>
                  <n-input-number
                    v-model:value="stripeRequestTimeoutSeconds"
                    :min="1"
                    :step="0.5"
                    :show-button="false"
                    placeholder="60"
                  />
                </div>
                <div class="field">
                  <label>Stripe retry attempts</label>
                  <n-input-number
                    v-model:value="stripeMaxRetryAttempts"
                    :min="1"
                    :show-button="false"
                    placeholder="100"
                  />
                </div>
                <div class="field">
                  <label>Stripe retry backoff (seconds)</label>
                  <n-input-number
                    v-model:value="stripeRetryBackoffSeconds"
                    :min="0.1"
                    :step="0.1"
                    :show-button="false"
                    placeholder="1"
                  />
                </div>
                <div class="field">
                  <label>Session cache TTL (hours)</label>
                  <n-input-number
                    v-model:value="sessionCacheTtlHours"
                    :min="1"
                    :max="720"
                    :show-button="false"
                    placeholder="24"
                  />
                </div>
              </div>
            </fieldset>

            <fieldset class="group">
              <legend class="group__legend">
                <span>Retry code whitelist</span>
              </legend>
              <div class="field">
                <label for="setting-auto-retry-codes">
                  Only retry jobs whose error_code is in this list (one code per line)
                </label>
                <n-input
                  id="setting-auto-retry-codes"
                  v-model:value="autoRetryCodesText"
                  type="textarea"
                  class="mono"
                  placeholder="approve_blocked&#10;address_validation_rejected&#10;refresh_poll_exhausted"
                  :autosize="{ minRows: 4, maxRows: 10 }"
                  spellcheck="false"
                />
                <p class="field-hint">
                  Do NOT add permanent codes (e.g. <code>invalid_credential</code>,
                  <code>approve_needs_review</code>) — retrying would be
                  pointless or could further harm the account.
                </p>
              </div>
            </fieldset>

            <fieldset class="group">
              <legend class="group__legend">
                <span>iDEAL bank</span>
              </legend>
              <div class="field">
                <label for="setting-known-issuers">
                  Valid BIC list (one code per line)
                </label>
                <n-input
                  id="setting-known-issuers"
                  v-model:value="knownIssuersText"
                  type="textarea"
                  class="mono"
                  placeholder="ASNBNL21&#10;RABONL2U&#10;ABNANL2A"
                  :autosize="{ minRows: 4, maxRows: 8 }"
                  spellcheck="false"
                />
              </div>
            </fieldset>

            <fieldset class="group">
              <legend class="group__legend">
                <span>Device profile (JSON)</span>
              </legend>
              <div class="field">
                <label>Browser fingerprint profile list</label>
                <n-input
                  :value="deviceProfilesText"
                  type="textarea"
                  class="mono"
                  :autosize="{ minRows: 6, maxRows: 12 }"
                  spellcheck="false"
                  @update:value="handleDeviceProfilesInput"
                />
                <p
                  v-if="deviceProfilesJsonError"
                  class="field-error"
                  role="alert"
                >
                  {{ deviceProfilesJsonError }}
                </p>
              </div>
            </fieldset>
          </div>
        </details>
      </div>

      <template #footer>
        <div class="footer">
          <n-text depth="3" style="font-size: 12px">
            {{ hasDrafts ? `${draftCount} unsaved change(s)` : 'No changes' }}
          </n-text>
          <n-space :size="8">
            <n-button
              size="small"
              quaternary
              :disabled="settingsStore.loading"
              @click="isOpen = false"
            >
              Close
            </n-button>
            <n-button
              type="primary"
              size="small"
              :loading="savingAll"
              :disabled="!hasDrafts || settingsStore.loading"
              @click="handleSaveAll"
            >
              Save all
            </n-button>
          </n-space>
        </div>
      </template>
    </n-modal>
  </section>
</template>

<style scoped>
.settings-accordion {
  display: inline-flex;
  align-items: center;
}

.settings-accordion__toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 30px;
  padding: 0 12px;
  font-family: inherit;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-1);
  background: var(--surface-2);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  cursor: pointer;
  transition: background var(--transition-fast), border-color var(--transition-fast);
  position: relative;
}

.settings-accordion__toggle:hover {
  background: var(--surface-3);
  border-color: var(--brand-primary);
}

.settings-accordion__toggle:focus-visible {
  outline: 2px solid var(--brand-primary);
  outline-offset: 2px;
}

.toggle-icon {
  color: var(--brand-primary);
  flex-shrink: 0;
}

.toggle-label {
  white-space: nowrap;
}

.toggle-badge {
  position: absolute;
  top: -6px;
  right: -6px;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--accent-yellow);
  color: #1a1200;
  font-size: 10px;
  font-weight: 700;
  line-height: 18px;
  text-align: center;
  border: 2px solid var(--surface-1);
  font-variant-numeric: tabular-nums;
}

.settings-accordion__body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.group {
  border: 1px solid var(--border-color);
  border-radius: 8px;
  padding: 14px 16px 16px;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  background: var(--surface-1);
  transition: border-color var(--transition-fast);
}

.group:hover {
  border-color: var(--brand-primary);
}

.group__legend {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 2px 10px;
  font-size: 11px;
  font-weight: 700;
  color: var(--text-1);
  text-transform: uppercase;
  letter-spacing: 0.6px;
  background: var(--surface-2);
  border: 1px solid var(--border-color);
  border-radius: 999px;
}

.group__legend :deep(.n-icon) {
  color: var(--brand-primary);
}

.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.field label {
  font-size: 11.5px;
  font-weight: 500;
  color: var(--n-text-color-2);
}

.field-error {
  margin: 0;
  font-size: 11px;
  color: var(--n-error-color);
}

.field-hint {
  margin: 4px 0 0;
  font-size: 11px;
  line-height: 1.5;
  color: var(--n-text-color-3);
}

/* Checkbox field — align items sát trái, hint mini nhét ngay dưới checkbox
   để cluster liên quan (checkbox + short helper) không lệch layout. */
.field--checkbox {
  gap: 2px;
}
.field--checkbox :deep(.n-checkbox__label) {
  font-size: 12.5px;
  color: var(--n-text-color);
}
.field-hint--tight {
  margin: 0 0 0 22px; /* thẳng cột với chữ checkbox (bỏ qua ô vuông) */
  font-size: 10.5px;
}

.field-hint code {
  font-family: var(--n-font-family-mono);
  font-size: 10.5px;
  padding: 0 3px;
  background: var(--surface-2);
  border-radius: 3px;
}

.grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

@media (max-width: 480px) {
  .grid-2 {
    grid-template-columns: 1fr;
  }
}

/*
 * Advanced <details> block — collapse mặc định. Chi tiết summary style
 * match với legend của group để user thấy đây là section, không phải
 * button. Marker mặc định của browser giữ nguyên (arrow trước summary).
 */
.advanced {
  border: 1px dashed var(--border-color);
  border-radius: 8px;
  background: transparent;
}

.advanced__summary {
  cursor: pointer;
  padding: 10px 14px;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--n-text-color-2);
  user-select: none;
  transition: background var(--transition-fast);
}

.advanced__summary:hover {
  background: var(--surface-2);
  border-radius: 8px;
}

.advanced__summary :deep(.n-icon) {
  color: var(--brand-primary);
}

.advanced__hint {
  font-size: 10.5px;
  font-weight: 400;
  color: var(--n-text-color-3);
  margin-left: auto;
  padding-left: 8px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.advanced[open] .advanced__summary {
  border-bottom: 1px dashed var(--border-color);
}

.advanced__body {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 12px 12px 14px;
}

.footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
}

.settings-modal :deep(.n-card__content) {
  max-height: 70vh;
  overflow-y: auto;
}

.alert-body {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  font-size: 12px;
}

.mono {
  font-family: var(--n-font-family-mono);
}

.mono :deep(textarea),
.mono :deep(input) {
  font-family: var(--n-font-family-mono);
  font-size: 11.5px;
}
</style>
