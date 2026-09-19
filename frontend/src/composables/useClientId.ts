/**
 * useClientId — id ngắn nhận diện tab/session hiện tại.
 *
 * Sinh 1 lần khi module load, KHÔNG persist localStorage (R11.5 —
 * không dùng localStorage cho runtime state). Mỗi tab web = 1 client_id
 * riêng, mỗi CLI process = 1 client_id riêng. Server đính kèm client_id
 * vào SSE `setting_updated` event → client tự lọc echo của chính mình
 * (tránh feedback loop khi typing textarea).
 *
 * Format: 12 hex ký tự — đủ entropy để không collision giữa vài chục
 * tab đồng thời (2^{48} space).
 */

function _generate(): string {
  // `crypto.randomUUID()` không có trên Node/vitest jsdom cũ, fallback
  // Math.random() (đủ cho non-security identifier).
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    const bytes = new Uint8Array(6)
    crypto.getRandomValues(bytes)
    return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
  }
  const chars = '0123456789abcdef'
  let out = ''
  for (let i = 0; i < 12; i += 1) {
    out += chars[Math.floor(Math.random() * 16)]
  }
  return out
}

const _CLIENT_ID = _generate()

export function useClientId(): string {
  return _CLIENT_ID
}
