/** Unsent composer state is private to a class/conversation and to a window writer. */
const PREFIX = 'lyra:unsent-chat:v1:'
const PREFERENCE_PREFIX = 'lyra:chat-source:v1:'
const WINDOW_ID_KEY = 'lyra:chat-draft-writer:v1'
const ACCEPTED_PREFIX = 'lyra:chat-sent-unretired:v1:'
const RETIRED_PREFIX = 'lyra:chat-draft-retired:v1:'
const MAX_VALUE_LENGTH = 64_000
const MAX_RECORDS = 128

type RecordValue = { value: string; revision: string; updatedAt: number; lineage?: string }
export type ChatDraftClearOutcome = 'retired' | 'superseded' | 'failed'

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
const draftSources = new Map<string, { key: string; revision: string }>()
const settlementVersions = new Map<string, number>()
const settlementOutcomes = new Map<string, ChatDraftClearOutcome>()
const settlementListeners = new Set<() => void>()

export function subscribeChatDraftSettlements(listener: () => void): () => void {
  settlementListeners.add(listener)
  return () => settlementListeners.delete(listener)
}

export function getChatDraftSettlementVersion(scope: string): number {
  return settlementVersions.get(scope) ?? 0
}

export function getChatDraftSettlementOutcome(scope: string): ChatDraftClearOutcome | null {
  return settlementOutcomes.get(scope) ?? null
}

function notifySettlement(scope: string, outcome: ChatDraftClearOutcome): void {
  settlementOutcomes.set(scope, outcome)
  settlementVersions.set(scope, getChatDraftSettlementVersion(scope) + 1)
  for (const listener of settlementListeners) listener()
}

function retiredKey(scope: string): string {
  return `${RETIRED_PREFIX}${encodeURIComponent(scope)}`
}

function durablyRetired(scope: string): Set<string> {
  try {
    const raw = localStorage.getItem(retiredKey(scope))
    const saved: unknown = raw ? JSON.parse(raw) : []
    return new Set(
      Array.isArray(saved)
        ? saved.filter((revision): revision is string => typeof revision === 'string')
        : [],
    )
  } catch {
    return new Set()
  }
}

function readRetired(scope: string): Set<string> {
  const revisions = new Set(acceptedUncleared.get(scope) ?? [])
  try {
    const raw = localStorage.getItem(retiredKey(scope))
    const saved: unknown = raw ? JSON.parse(raw) : []
    if (Array.isArray(saved)) {
      for (const revision of saved) {
        if (typeof revision === 'string') revisions.add(revision)
      }
    }
  } catch {
    // Keep the in-memory acknowledgement if storage is unavailable.
  }
  return revisions
}

function retireRevision(scope: string, revision: string): boolean {
  const revisions = readRetired(scope)
  revisions.add(revision)
  acceptedUncleared.set(scope, revisions)
  try {
    const present = new Set(storedRecords(scope).map(({ record }) => record.revision))
    const retained = [...revisions].filter((item) => present.has(item))
    if (retained.length > MAX_RECORDS) return false
    localStorage.setItem(retiredKey(scope), JSON.stringify(retained))
    return true
  } catch {
    return false
  }
}

function acceptedKey(scope: string): string {
  return `${ACCEPTED_PREFIX}${encodeURIComponent(scope)}:${windowId}`
}

function readAccepted(scope: string): Set<string> {
  const revisions = readRetired(scope)
  for (const kind of ['session', 'local'] as const) {
    try {
      const storage = kind === 'session' ? sessionStorage : localStorage
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

/** An accepted physical copy lacks durable cross-session suppression. */
export function hasChatDraftRetirementRisk(scope: string): boolean {
  const accepted = readAccepted(scope)
  if (accepted.size === 0) return false
  const retired = durablyRetired(scope)
  try {
    return storedRecords(scope).some(
      ({ record }) => accepted.has(record.revision) && !retired.has(record.revision),
    )
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
  try {
    const accepted = readAccepted(scope)
    let latest: { key: string; record: RecordValue } | null = null
    for (const entry of storedRecords(scope)) {
      if (accepted.has(entry.record.revision)) continue
      if (!latest || entry.record.updatedAt > latest.record.updatedAt) latest = entry
    }
    if (!latest) return null
    memory.set(scope, latest.record)
    draftSources.set(scope, { key: latest.key, revision: latest.record.revision })
    return latest.record
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
    lineage: memory.get(scope)?.lineage ?? memory.get(scope)?.revision ?? crypto.randomUUID(),
  }
  const source = draftSources.get(scope)
  memory.set(scope, record)
  if (value.length > MAX_VALUE_LENGTH) {
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
        !source &&
        count - pruneEmptyScopes() - migrateLegacySelections() >= MAX_RECORDS
      )
        return { record, durable: false }
    }
    localStorage.setItem(key, JSON.stringify(record))
    if (source && source.key !== key) {
      const previous = parse(localStorage.getItem(source.key))
      if (previous?.revision === source.revision) {
        try {
          localStorage.removeItem(source.key)
          if (
            localStorage.getItem(source.key) !== null &&
            !retireRevision(scope, source.revision)
          ) {
            return { record, durable: false }
          }
        } catch {
          if (!retireRevision(scope, source.revision)) return { record, durable: false }
        }
      }
    }
    draftSources.set(scope, { key, revision: record.revision })
    return { record, durable: true }
  } catch {
    return { record, durable: false }
  }
}

/** A settled send retires only the revision this writer actually submitted. */
export function clearChatDraftIfRevision(scope: string, revision: string): ChatDraftClearOutcome {
  if (memory.get(scope)?.revision !== revision) {
    notifySettlement(scope, 'superseded')
    return 'superseded'
  }
  let outcome: ChatDraftClearOutcome = 'retired'
  try {
    const ownKey = recordKey(scope)
    const records = storedRecords(scope)
    const source = draftSources.get(scope)
    const own = records.find(({ key }) => key === ownKey)?.record
    const foreignCopy = records.some(
      ({ key, record }) => key !== ownKey && record.revision === revision,
    )
    if (own?.revision === revision && own.updatedAt <= memory.get(scope)!.updatedAt) {
      localStorage.removeItem(ownKey)
      if (localStorage.getItem(ownKey) !== null) {
        outcome = 'failed'
      }
    }
    if (foreignCopy && !retireRevision(scope, revision)) outcome = 'failed'
    if (source && source.revision !== revision) {
      const predecessor = records.find(({ key }) => key === source.key)?.record
      if (predecessor?.revision === source.revision && !retireRevision(scope, source.revision)) {
        outcome = 'failed'
      }
    }
    if (outcome === 'failed') retireRevision(scope, revision)
    memory.delete(scope)
    draftSources.delete(scope)
  } catch {
    retireRevision(scope, revision)
    outcome = 'failed'
  }
  notifySettlement(scope, outcome)
  return outcome
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
  draftSources.clear()
  settlementVersions.clear()
  settlementOutcomes.clear()
  notifyPending()
}
