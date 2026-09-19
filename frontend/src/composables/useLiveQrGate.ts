/**
 * useLiveQrGate — Pinia store for Live QR capacity gate.
 *
 * Snapshot: enabled, max_per_chat, live, capacity, blocked, per_chat.
 * No resume API — slots free on plus/timeout/lifecycle/TTL sweep.
 */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

const API_BASE = '/api'

export interface LiveQrGateSnapshot {
  enabled: boolean
  max_per_chat: number
  live: number
  capacity: number
  blocked: boolean
  per_chat: Record<string, number>
}

const DEFAULT_SNAPSHOT: LiveQrGateSnapshot = {
  enabled: false,
  max_per_chat: 5,
  live: 0,
  capacity: 0,
  blocked: false,
  per_chat: {},
}

export const useLiveQrGateStore = defineStore('liveQrGate', () => {
  const snapshot = ref<LiveQrGateSnapshot>({ ...DEFAULT_SNAPSHOT })
  const loading = ref(false)
  const error = ref<string | null>(null)

  /** Badge when live mode is on (progress or full). Not a Resume button. */
  const shouldShowLiveBadge = computed(
    () => snapshot.value.enabled,
  )

  async function load(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const res = await fetch(`${API_BASE}/notifications/telegram/live-qr-gate`, {
        method: 'GET',
      })
      if (!res.ok) {
        if (res.status === 503) {
          console.warn('[liveQrGate] service not ready (503)')
          return
        }
        throw new Error(`HTTP ${res.status}`)
      }
      const body = (await res.json()) as LiveQrGateSnapshot
      snapshot.value = normalise(body)
    } catch (ex) {
      error.value = ex instanceof Error ? ex.message : String(ex)
    } finally {
      loading.value = false
    }
  }

  function applyLiveQrGateEvent(payload: unknown): void {
    if (typeof payload !== 'object' || payload === null) return
    snapshot.value = normalise(payload as Record<string, unknown>)
  }

  return {
    snapshot,
    loading,
    error,
    shouldShowLiveBadge,
    load,
    applyLiveQrGateEvent,
  }
})

function normalise(
  raw: Record<string, unknown> | LiveQrGateSnapshot,
): LiveQrGateSnapshot {
  const source = raw as Record<string, unknown>
  const maxRaw = source.max_per_chat
  const max_per_chat =
    typeof maxRaw === 'number' && Number.isInteger(maxRaw) && maxRaw >= 1
      ? maxRaw
      : DEFAULT_SNAPSHOT.max_per_chat
  const liveRaw = source.live
  const live =
    typeof liveRaw === 'number' && Number.isInteger(liveRaw) && liveRaw >= 0
      ? liveRaw
      : 0
  const capRaw = source.capacity
  const capacity =
    typeof capRaw === 'number' && Number.isInteger(capRaw) && capRaw >= 0
      ? capRaw
      : 0
  const per = source.per_chat
  const per_chat =
    typeof per === 'object' && per !== null && !Array.isArray(per)
      ? (per as Record<string, number>)
      : {}
  return {
    enabled: typeof source.enabled === 'boolean' ? source.enabled : false,
    max_per_chat,
    live,
    capacity,
    blocked: typeof source.blocked === 'boolean' ? source.blocked : false,
    per_chat,
  }
}
