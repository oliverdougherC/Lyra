/** Unsent composer state is private to a class/conversation and to a window writer. */
const PREFIX = 'lyra:unsent-chat:v1:'
const MAX_VALUE_LENGTH = 64_000
const MAX_RECORDS = 128

type RecordValue = { value: string; revision: string; updatedAt: number }

const windowId = crypto.randomUUID()
const memory = new Map<string, RecordValue>()

function recordKey(scope: string): string {
  return `${PREFIX}${encodeURIComponent(scope)}:${windowId}`
}

function parse(raw: string | null): RecordValue | null {
  if (!raw) return null
  try {
    const value: unknown = JSON.parse(raw)
    if (
      typeof value === 'object' &&
      value !== null &&
      'value' in value &&
      typeof value.value === 'string' &&
      value.value.length <= MAX_VALUE_LENGTH &&
      'revision' in value &&
      typeof value.revision === 'string' &&
      'updatedAt' in value &&
      typeof value.updatedAt === 'number' &&
      Number.isFinite(value.updatedAt)
    ) {
      return value as RecordValue
    }
  } catch {
    // A malformed or unavailable entry cannot replace a valid draft.
  }
  return null
}

function pruneEmptyScopes(): number {
  const scopes = new Map<string, { keys: string[]; empty: boolean }>()
  for (let index = 0; index < localStorage.length; index += 1) {
    const key = localStorage.key(index)
    if (!key?.startsWith(PREFIX)) continue
    const scope = key.slice(PREFIX.length).split(':')[0]
    const group = scopes.get(scope) ?? { keys: [], empty: true }
    group.keys.push(key)
    group.empty &&= parse(localStorage.getItem(key))?.value === ''
    scopes.set(scope, group)
  }
  let removed = 0
  for (const group of scopes.values()) {
    if (!group.empty) continue
    for (const key of group.keys) {
      localStorage.removeItem(key)
      removed += 1
    }
  }
  return removed
}

export function readChatDraft(scope: string): RecordValue | null {
  const own = memory.get(scope)
  if (own) return own
  if (typeof localStorage === 'undefined') return null
  try {
    const prefix = `${PREFIX}${encodeURIComponent(scope)}:`
    let latest: RecordValue | null = null
    for (let index = 0; index < localStorage.length; index += 1) {
      const key = localStorage.key(index)
      if (!key?.startsWith(prefix)) continue
      const item = parse(localStorage.getItem(key))
      if (item && (!latest || item.updatedAt > latest.updatedAt)) latest = item
    }
    return latest
  } catch {
    return null
  }
}

/** Returns a revision even when storage refuses the write, so UI race guards still work. */
export function writeChatDraft(
  scope: string,
  value: string,
): { record: RecordValue; durable: boolean } {
  const record = {
    value,
    revision: crypto.randomUUID(),
    updatedAt: Math.max(Date.now(), (memory.get(scope)?.updatedAt ?? 0) + 1),
  }
  memory.set(scope, record)
  if (value.length > MAX_VALUE_LENGTH || typeof localStorage === 'undefined') {
    return { record, durable: false }
  }
  try {
    const key = recordKey(scope)
    if (localStorage.getItem(key) === null) {
      let count = 0
      for (let index = 0; index < localStorage.length; index += 1) {
        if (localStorage.key(index)?.startsWith(PREFIX)) count += 1
      }
      if (count >= MAX_RECORDS && count - pruneEmptyScopes() >= MAX_RECORDS)
        return { record, durable: false }
    }
    localStorage.setItem(key, JSON.stringify(record))
    return { record, durable: true }
  } catch {
    return { record, durable: false }
  }
}

/** A settled send clears only the revision it submitted in this window. */
export function clearChatDraftIfRevision(scope: string, revision: string): boolean {
  if (memory.get(scope)?.revision !== revision) return false
  writeChatDraft(scope, '')
  return true
}

/** Reset the in-window fallback between isolated test fixtures. */
export function resetChatDraftMemory(): void {
  memory.clear()
}
