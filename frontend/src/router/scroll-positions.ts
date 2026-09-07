/** Optional per-history-entry positions. Scrolling must never spend History API quota. */
export type ScrollPositions = Partial<Record<'main' | 'files', number>>

const PREFIX = 'lyra:route-scroll:'
const CHECKPOINT_MS = 1000
const MEMORY_ENTRIES = 64

function decode(raw: string | null): ScrollPositions | undefined {
  if (!raw) return
  try {
    const value = JSON.parse(raw)
    if (!value || typeof value !== 'object') return
    const positions: ScrollPositions = {}
    for (const key of ['main', 'files'] as const) {
      if (Number.isFinite(value[key]) && value[key] >= 0) positions[key] = value[key]
    }
    return positions
  } catch {
    return
  }
}

export function createScrollPositions() {
  // Bound memory while session storage retains older entries where available.
  const entries = new Map<string, string>()
  const pending = new Map<string, string>()
  let timer: ReturnType<typeof setTimeout> | undefined

  function remember(id: string, raw: string) {
    entries.delete(id)
    entries.set(id, raw)
    if (entries.size > MEMORY_ENTRIES) entries.delete(entries.keys().next().value!)
  }

  function read(id: string): ScrollPositions | undefined {
    let raw = entries.get(id)
    if (raw === undefined) {
      try {
        raw = sessionStorage.getItem(PREFIX + id) ?? undefined
      } catch {
        /* Optional. */
      }
      if (raw !== undefined) remember(id, raw)
    }
    return decode(raw ?? null)
  }

  function flush() {
    clearTimeout(timer)
    timer = undefined
    for (const [id, raw] of pending) {
      try {
        sessionStorage.setItem(PREFIX + id, raw)
      } catch {
        /* Navigation keeps working. */
      }
    }
    pending.clear()
  }

  function record(id: string, positions: ScrollPositions) {
    const next = JSON.stringify({ ...read(id), ...positions })
    if (entries.get(id) === next) return
    remember(id, next)
    pending.set(id, next)
    // A throttle with a trailing checkpoint also handles continuous scrolling.
    timer ??= setTimeout(flush, CHECKPOINT_MS)
  }

  return { read, record, flush }
}
