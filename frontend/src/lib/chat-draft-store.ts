/** Unsent composer state is private to a class/conversation and to a window writer. */
const PREFIX = 'lyra:unsent-chat:v1:'
const PREFERENCE_PREFIX = 'lyra:chat-source:v1:'
const WINDOW_ID_KEY = 'lyra:chat-draft-writer:v1'
const ACCEPTED_PREFIX = 'lyra:chat-sent-unretired:v1:'
const MAX_VALUE_LENGTH = 64_000
const MAX_RECORDS = 128

type RecordValue = { value: string; revision: string; updatedAt: number }

function currentWindowId(): string {
  try {
    const existing = sessionStorage.getItem(WINDOW_ID_KEY)
    if (existing) return existing
    const created = crypto.randomUUID()
    sessionStorage.setItem(WINDOW_ID_KEY, created)
    return created
  } catch {
    return crypto.randomUUID()
  }
}

// A tab retains its writer key through JS reloads. Other tabs have separate session stores.
const windowId = currentWindowId()
const memory = new Map<string, RecordValue>()
const pendingSends = new Map<string, symbol>()
const pendingListeners = new Set<() => void>()
const acceptedUncleared = new Map<string, Set<string>>()

function acceptedKey(scope: string): string {
  return `${ACCEPTED_PREFIX}${encodeURIComponent(scope)}:${windowId}`
}

function readAccepted(scope: string): Set<string> {
  const revisions = new Set(acceptedUncleared.get(scope) ?? [])
  for (const storage of [sessionStorage, localStorage]) {
    try {
      const raw = storage.getItem(acceptedKey(scope))
      if (!raw) continue
      const saved: unknown = JSON.parse(raw)
      if (Array.isArray(saved)) {
        for (const revision of saved) {
          if (typeof revision === 'string') revisions.add(revision)
        }
      }
    } catch {
      // The other store or current heap may still have the acknowledgement.
    }
  }
  return revisions
}

function markAcceptedUncleared(scope: string, revision: string): boolean {
  let present: Set<string>
  try {
    present = new Set(storedRecords(scope).map(({ record }) => record.revision))
  } catch {
    acceptedUncleared.set(scope, new Set([...readAccepted(scope), revision]))
    return false
  }
  const revisions = new Set([...readAccepted(scope)].filter((item) => present.has(item)))
  revisions.add(revision)
  acceptedUncleared.set(scope, revisions)
  if (revisions.size > MAX_RECORDS) return false
  const value = JSON.stringify([...revisions])
  let durable = false
  try {
    sessionStorage.setItem(acceptedKey(scope), value)
    durable = true
  } catch {
    // Another store may still accept the acknowledgement.
  }
  try {
    localStorage.setItem(acceptedKey(scope), value)
    durable = true
  } catch {
    // The visible warning explains when neither store can retain it.
  }
  return durable
}

export function isChatDraftAccepted(scope: string, revision: string): boolean {
  return readAccepted(scope).has(revision)
}

export function hasChatDraftAccepted(scope: string): boolean {
  const accepted = readAccepted(scope)
  if (accepted.size === 0) return false
  try {
    return storedRecords(scope).some(({ record }) => accepted.has(record.revision))
  } catch {
    return true
  }
}

function notifyPending(): void {
  for (const listener of pendingListeners) listener()
}

export function subscribeChatDraftSends(listener: () => void): () => void {
  pendingListeners.add(listener)
  return () => pendingListeners.delete(listener)
}

export function isChatDraftSendPending(scope: string): boolean {
  return pendingSends.has(scope)
}

export function beginChatDraftSend(scope: string): symbol | null {
  if (pendingSends.has(scope)) return null
  const token = Symbol(scope)
  pendingSends.set(scope, token)
  notifyPending()
  return token
}

export function moveChatDraftSend(scope: string, nextScope: string, token: symbol): void {
  if (pendingSends.get(scope) !== token) return
  pendingSends.delete(scope)
  pendingSends.set(nextScope, token)
  notifyPending()
}

export function finishChatDraftSend(token: symbol): void {
  for (const [scope, pending] of pendingSends) {
    if (pending !== token) continue
    pendingSends.delete(scope)
    notifyPending()
    return
  }
}

function recordKey(scope: string): string {
  return `${PREFIX}${encodeURIComponent(scope)}:${windowId}`
}

function scopePrefix(scope: string): string {
  return `${PREFIX}${encodeURIComponent(scope)}:`
}

function storedRecords(scope: string): { key: string; record: RecordValue }[] {
  const prefix = scopePrefix(scope)
  const records: { key: string; record: RecordValue }[] = []
  for (let index = 0; index < localStorage.length; index += 1) {
    const key = localStorage.key(index)
    if (!key?.startsWith(prefix)) continue
    const record = parse(localStorage.getItem(key))
    if (record) records.push({ key, record })
  }
  return records
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

function migrateLegacySelections(): number {
  const groups = new Map<string, { keys: string[]; latest: RecordValue | null }>()
  for (let index = 0; index < localStorage.length; index += 1) {
    const key = localStorage.key(index)
    if (!key?.startsWith(PREFIX)) continue
    const encodedScope = key.slice(PREFIX.length).split(':')[0]
    let scope: string
    try {
      scope = decodeURIComponent(encodedScope)
    } catch {
      continue
    }
    if (!scope.startsWith('source:')) continue
    const record = parse(localStorage.getItem(key))
    const group = groups.get(scope) ?? { keys: [], latest: null }
    group.keys.push(key)
    if (record && (!group.latest || record.updatedAt > group.latest.updatedAt)) {
      group.latest = record
    }
    groups.set(scope, group)
  }
  let removed = 0
  for (const [scope, group] of groups) {
    const choice = group.latest?.value
    if (choice !== 'all' && !(choice && /^[1-9]\d*$/.test(choice))) continue
    const preferenceKey = `${PREFERENCE_PREFIX}${encodeURIComponent(scope.slice(7))}`
    try {
      if (localStorage.getItem(preferenceKey) === null) {
        localStorage.setItem(preferenceKey, choice)
      }
      for (const key of group.keys) {
        localStorage.removeItem(key)
        if (localStorage.getItem(key) === null) removed += 1
      }
    } catch {
      // Leave the old records intact if either migration step is refused.
    }
  }
  return removed
}

export function readChatDraft(scope: string): RecordValue | null {
  const own = memory.get(scope)
  if (own && !isChatDraftAccepted(scope, own.revision)) return own
  if (typeof localStorage === 'undefined') return null
  try {
    const accepted = readAccepted(scope)
    let latest: RecordValue | null = null
    for (const entry of storedRecords(scope)) {
      if (accepted.has(entry.record.revision)) continue
      if (!latest || entry.record.updatedAt > latest.updatedAt) latest = entry.record
    }
    if (!latest) return null
    memory.set(scope, latest)
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
      if (
        count >= MAX_RECORDS &&
        count - pruneEmptyScopes() - migrateLegacySelections() >= MAX_RECORDS
      )
        return { record, durable: false }
    }
    localStorage.setItem(key, JSON.stringify(record))
    return { record, durable: true }
  } catch {
    return { record, durable: false }
  }
}

/** A settled send retires only the revision this writer actually submitted. */
export function clearChatDraftIfRevision(scope: string, revision: string): boolean {
  if (memory.get(scope)?.revision !== revision) return false
  if (typeof localStorage === 'undefined') return false
  try {
    const ownKey = recordKey(scope)
    const records = storedRecords(scope)
    const own = records.find(({ key }) => key === ownKey)?.record
    const foreignCopy = records.some(
      ({ key, record }) => key !== ownKey && record.revision === revision,
    )
    if (own && own.updatedAt <= memory.get(scope)!.updatedAt) {
      localStorage.removeItem(ownKey)
      if (localStorage.getItem(ownKey) !== null) {
        markAcceptedUncleared(scope, revision)
        return false
      }
    }
    if (foreignCopy && !markAcceptedUncleared(scope, revision)) return false
    memory.delete(scope)
    return true
  } catch {
    markAcceptedUncleared(scope, revision)
    return false
  }
}

/** Replaceable source choice lives outside the capped unsent-question store. */
export function readChatSourceSelection(key: string): string | null {
  try {
    const current = localStorage.getItem(`${PREFERENCE_PREFIX}${encodeURIComponent(key)}`)
    if (current !== null) return current
    let latest: RecordValue | null = null
    for (const { record } of storedRecords(`source:${key}`)) {
      if (!latest || record.updatedAt > latest.updatedAt) latest = record
    }
    return latest?.value ?? null
  } catch {
    return null
  }
}

export function writeChatSourceSelection(key: string, value: string): boolean {
  if (value !== 'all' && !/^[1-9]\d*$/.test(value)) return false
  try {
    localStorage.setItem(`${PREFERENCE_PREFIX}${encodeURIComponent(key)}`, value)
    // The old selection records are replaceable preferences, including other windows'.
    for (const { key: oldKey } of storedRecords(`source:${key}`)) {
      localStorage.removeItem(oldKey)
    }
    return true
  } catch {
    return false
  }
}

/** Reset the in-window fallback between isolated test fixtures. */
export function resetChatDraftMemory(): void {
  memory.clear()
  pendingSends.clear()
  acceptedUncleared.clear()
  notifyPending()
}
