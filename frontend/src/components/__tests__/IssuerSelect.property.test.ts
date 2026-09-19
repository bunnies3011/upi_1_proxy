/**
 * Property test cho `resolveIssuerDisplayName` (Property 19).
 *
 * Validates: Requirements 6.8, 6.9
 *
 * Property 19 gồm 2 mệnh đề bất biến của cùng 1 hàm pure:
 *   1. Với mọi `id` có trong `ISSUER_DISPLAY_NAME_MAP`,
 *      `resolveIssuerDisplayName(id)` trả về đúng tên hiển thị thân
 *      thiện đã khai báo trong bảng ánh xạ (Requirement 6.8).
 *   2. Với mọi `id` non-empty KHÔNG có trong bảng ánh xạ, hàm fallback
 *      về chính `id` — không trả rỗng, không raise (Requirement 6.9).
 *
 * Dùng `fast-check` sinh input; `numRuns` giữ nhỏ (30 — fast profile) để
 * test chạy nhanh trong CI — hàm pure, deterministic, không cần iteration
 * count lớn.
 */
import { describe, it, expect } from 'vitest'
import fc from 'fast-check'

import {
  ISSUER_DISPLAY_NAME_MAP,
  resolveIssuerDisplayName,
} from '../IssuerSelect.vue'

describe('resolveIssuerDisplayName — Property 19', () => {
  it('returns mapped display name for known issuer id (Requirement 6.8)', () => {
    const knownIds = Object.keys(ISSUER_DISPLAY_NAME_MAP)
    // Guard: bảng ánh xạ phải không rỗng, nếu không property vô nghĩa.
    expect(knownIds.length).toBeGreaterThan(0)

    fc.assert(
      fc.property(fc.constantFrom(...knownIds), (id) => {
        expect(resolveIssuerDisplayName(id)).toBe(ISSUER_DISPLAY_NAME_MAP[id])
      }),
      { numRuns: 30 },
    )
  })

  it('falls back to raw id when not in map (Requirement 6.9)', () => {
    fc.assert(
      fc.property(fc.string({ minLength: 1, maxLength: 20 }), (randomId) => {
        // Bỏ qua case trùng mã đã có trong map — property này chỉ cover
        // nhánh fallback.
        fc.pre(!(randomId in ISSUER_DISPLAY_NAME_MAP))
        expect(resolveIssuerDisplayName(randomId)).toBe(randomId)
      }),
      { numRuns: 30 },
    )
  })
})
