/** Unsent composer state is private to a class/conversation and to a window writer. */
const PREFIX = 'lyra:unsent-chat:v1:'
const REVISION_PREFIX = 'lyra:unsent-chat:v2:'
const PREFERENCE_PREFIX = 'lyra:chat-source:v1:'
const WINDOW_ID_KEY = 'lyra:chat-draft-writer:v1'
const ACCEPTED_PREFIX = 'lyra:chat-sent-unretired:v1:'
const RETIRED_PREFIX = 'lyra:chat-draft-retired:v1:'
const REVISION_RETIRED_PREFIX = 'lyra:chat-draft-retired:v2:'
const MAX_VALUE_LENGTH = 64_000
const MAX_RECORDS = 128

type Ancestor = { key: string; revision: string }
type RecordValue = {
  value: string
  revision: string
  updatedAt: number
  lineage?: string
  ancestors?: Ancestor[]
}
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

function revisionRetiredKey(scope: string, revision: string): string {
  return `${REVISION_RETIRED_PREFIX}${encodeURIComponent(scope)}:${encodeURIComponent(revision)}`
}

function revisionRetiredPrefix(scope: string): string {
  return `${REVISION_RETIRED_PREFIX}${encodeURIComponent(scope)}:`
}

function isRevisionKey(key: string): boolean {
  return key.startsWith(REVISION_PREFIX)
}

function durablyRetired(scope: string): Set<string> {
  const retired = new Set<string>()
  try {
    const raw = localStorage.getItem(retiredKey(scope))
    const saved: unknown = raw ? JSON.parse(raw) : []
    if (Array.isArray(saved)) {
      for (const revision of saved) if (typeof revision === 'string') retired.add(revision)
    }
    const prefix = revisionRetiredPrefix(scope)
    for (let index = 0; index < localStorage.length; index += 1) {
      const key = localStorage.key(index)
      if (key?.startsWith(prefix)) retired.add(decodeURIComponent(key.slice(prefix.length)))
    }
  } catch {
    // Keep any markers already found if storage becomes unavailable mid-scan.
  }
  return retired
}

function isDurablyRetired(scope: string, revision: string): boolean {
  try {
    if (localStorage.getItem(revisionRetiredKey(scope, revision)) !== null) return true
    const raw = localStorage.getItem(retiredKey(scope))
    const saved: unknown = raw ? JSON.parse(raw) : []
    return Array.isArray(saved) && saved.includes(revision)
  } catch {
    return false
  }
}

function readRetired(scope: string): Set<string> {
  const revisions = new Set(acceptedUncleared.get(scope) ?? [])
  for (const revision of durablyRetired(scope)) revisions.add(revision)
  return revisions
}

function retireRevision(scope: string, revision: string, retainOnFailure = false): boolean {
  const revisions = readRetired(scope)
  revisions.add(revision)
  try {
    // One immutable marker per revision: simultaneous acknowledgements never replace one another.
    localStorage.setItem(revisionRetiredKey(scope, revision), '1')
    acceptedUncleared.set(scope, revisions)
    reclaimRetiredScope(scope)
    return true
  } catch {
    if (retainOnFailure) acceptedUncleared.set(scope, revisions)
    return false
  }
}

/** The marker must reach storage before an acknowledged copy is removed. */
function reclaimRetiredScope(scope: string): number {
  const retired = durablyRetired(scope)
  if (retired.size === 0) return 0
  let removed = 0
  try {
    for (const { key, record } of storedRecords(scope)) {
      if (!retired.has(record.revision)) continue
      // Legacy writer slots can still be replaced by an older live context. Never delete them.
      if (!isRevisionKey(key)) continue
      localStorage.removeItem(key)
      if (localStorage.getItem(key) === null) removed += 1
    }
    const present = new Set(storedRecords(scope).map(({ record }) => record.revision))
    for (const revision of retired) {
      if (!present.has(revision)) localStorage.removeItem(revisionRetiredKey(scope, revision))
    }
    const needed = [...retired].filter((revision) => present.has(revision))
    if (needed.length === 0) acceptedUncleared.delete(scope)
    else acceptedUncleared.set(scope, new Set(needed))
  } catch {
    // Keep the durable marker for copies that storage would not let us remove.
  }
  return removed
}

/** Run the full sweep only when a new record would meet the unsent cap. */
function reclaimRetiredRecords(): number {
  const scopes = new Set<string>()
  for (let index = 0; index < localStorage.length; index += 1) {
    const key = localStorage.key(index)
    if (!key?.startsWith(RETIRED_PREFIX) && !key?.startsWith(REVISION_RETIRED_PREFIX)) continue
    try {
      const suffix = key.startsWith(RETIRED_PREFIX)
        ? key.slice(RETIRED_PREFIX.length)
        : key.slice(REVISION_RETIRED_PREFIX.length).split(':')[0]
      scopes.add(decodeURIComponent(suffix))
    } catch {
      // Malformed keys are not evidence that a draft was acknowledged.
    }
  }
  let removed = 0
  for (const scope of scopes) removed += reclaimRetiredScope(scope)
  return removed
}

function acceptedKey(scope: string): string {
  return `${ACCEPTED_PREFIX}${encodeURIComponent(scope)}:${windowId}`
}

function readAccepted(scope: string): Set<string> {
  const revisions = new Set(acceptedUncleared.get(scope) ?? [])
  try {
    const raw = localStorage.getItem(retiredKey(scope))
    const saved: unknown = raw ? JSON.parse(raw) : []
    if (Array.isArray(saved)) {
      for (const revision of saved) if (typeof revision === 'string') revisions.add(revision)
    }
  } catch {
    // The current heap can still suppress a send after denied storage.
  }
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
  if (readAccepted(scope).has(revision)) return true
  try {
    return localStorage.getItem(revisionRetiredKey(scope, revision)) !== null
  } catch {
    return false
  }
}

export function hasChatDraftAccepted(scope: string): boolean {
  try {
    return storedRecords(scope).some(({ record }) => isChatDraftAccepted(scope, record.revision))
  } catch {
    return readAccepted(scope).size > 0
  }
}

/** An accepted physical copy lacks durable cross-session suppression. */
export function hasChatDraftRetirementRisk(scope: string): boolean {
  const accepted = readAccepted(scope)
  try {
    return storedRecords(scope).some(({ record }) => {
      if (!accepted.has(record.revision) && !isChatDraftAccepted(scope, record.revision))
        return false
      return !isDurablyRetired(scope, record.revision)
    })
  } catch {
    return accepted.size > 0
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

function recordKey(scope: string, revision: string): string {
  return `${REVISION_PREFIX}${encodeURIComponent(scope)}:${windowId}:${revision}`
}

function scopePrefixes(scope: string): string[] {
  const encoded = encodeURIComponent(scope)
  return [`${PREFIX}${encoded}:`, `${REVISION_PREFIX}${encoded}:`]
}

function storedRecords(scope: string): { key: string; record: RecordValue }[] {
  const prefixes = scopePrefixes(scope)
  const records: { key: string; record: RecordValue }[] = []
  for (let index = 0; index < localStorage.length; index += 1) {
    const key = localStorage.key(index)
    if (!key || !prefixes.some((prefix) => key.startsWith(prefix))) continue
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
      const record = value as RecordValue
      if (
        record.ancestors !== undefined &&
        (!Array.isArray(record.ancestors) ||
          record.ancestors.length > MAX_RECORDS ||
          record.ancestors.some(
            (item) =>
              !item ||
              typeof item.key !== 'string' ||
              typeof item.revision !== 'string' ||
              (!item.key.startsWith(PREFIX) && !item.key.startsWith(REVISION_PREFIX)),
          ))
      ) {
        return null
      }
      return record
    }
  } catch {
    // A malformed or unavailable entry cannot replace a valid draft.
  }
  return null
}

function allStoredRecords(): { key: string; record: RecordValue; scope: string }[] {
  const records: { key: string; record: RecordValue; scope: string }[] = []
  for (let index = 0; index < localStorage.length; index += 1) {
    const key = localStorage.key(index)
    if (!key) continue
    const prefix = key.startsWith(REVISION_PREFIX)
      ? REVISION_PREFIX
      : key.startsWith(PREFIX)
        ? PREFIX
        : null
    if (!prefix) continue
    const record = parse(localStorage.getItem(key))
    if (!record) continue
    try {
      records.push({
        key,
        record,
        scope: decodeURIComponent(key.slice(prefix.length).split(':')[0]),
      })
    } catch {
      // Malformed scope keys do not enter capacity decisions.
    }
  }
  return records
}

function capacityRecordCount(): number {
  const records = allStoredRecords()
  const supersededLegacy = new Set<string>()
  for (const { key, record } of records) {
    if (!isRevisionKey(key)) continue
    for (const ancestor of record.ancestors ?? []) {
      if (!isRevisionKey(ancestor.key))
        supersededLegacy.add(`${ancestor.key}\0${ancestor.revision}`)
    }
  }
  let count = 0
  for (const { key, record, scope } of records) {
    if (scope.startsWith('source:')) continue
    if (isRevisionKey(key)) {
      count += 1
      continue
    }
    if (
      record.value !== '' &&
      !isDurablyRetired(scope, record.revision) &&
      !supersededLegacy.has(`${key}\0${record.revision}`)
    )
      count += 1
  }
  return count
}

function pruneEmptyScopes(): number {
  const scopes = new Map<string, { keys: string[]; empty: boolean }>()
  for (const { key, record, scope } of allStoredRecords()) {
    const group = scopes.get(scope) ?? { keys: [], empty: true }
    if (isRevisionKey(key)) group.keys.push(key)
    group.empty &&= record.value === ''
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
  const groups = new Map<string, RecordValue>()
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
    const latest = groups.get(scope)
    if (record && (!latest || record.updatedAt > latest.updatedAt)) groups.set(scope, record)
  }
  for (const [scope, latest] of groups) {
    const choice = latest.value
    if (choice !== 'all' && !(choice && /^[1-9]\d*$/.test(choice))) continue
    const preferenceKey = `${PREFERENCE_PREFIX}${encodeURIComponent(scope.slice(7))}`
    try {
      if (localStorage.getItem(preferenceKey) === null) {
        localStorage.setItem(preferenceKey, choice)
      }
    } catch {
      // Leave the old records intact if either migration step is refused.
    }
  }
  // Legacy writer slots remain physically intact because an old context may still replace one.
  return 0
}

export function readChatDraft(scope: string): RecordValue | null {
  const own = memory.get(scope)
  if (own && !isChatDraftAccepted(scope, own.revision)) {
    const source = draftSources.get(scope)
    if (!source || source.revision !== own.revision) return own
    try {
      if (parse(localStorage.getItem(source.key))?.revision === own.revision) return own
      memory.delete(scope)
      draftSources.delete(scope)
    } catch {
      return own
    }
  }
  try {
    let latest: { key: string; record: RecordValue } | null = null
    let ownLatest: { key: string; record: RecordValue } | null = null
    const ownPrefix = `${REVISION_PREFIX}${encodeURIComponent(scope)}:${windowId}:`
    const ownLegacyKey = `${PREFIX}${encodeURIComponent(scope)}:${windowId}`
    for (const entry of storedRecords(scope)) {
      if (isChatDraftAccepted(scope, entry.record.revision)) continue
      if (!latest || entry.record.updatedAt > latest.record.updatedAt) latest = entry
      if (
        (entry.key.startsWith(ownPrefix) || entry.key === ownLegacyKey) &&
        (!ownLatest || entry.record.updatedAt > ownLatest.record.updatedAt)
      )
        ownLatest = entry
    }
    latest = ownLatest ?? latest
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
  if (!memory.has(scope)) {
    try {
      const ownPrefix = `${REVISION_PREFIX}${encodeURIComponent(scope)}:${windowId}:`
      const ownLegacyKey = `${PREFIX}${encodeURIComponent(scope)}:${windowId}`
      const own = storedRecords(scope)
        .filter(
          ({ key, record }) =>
            (key.startsWith(ownPrefix) || key === ownLegacyKey) &&
            !isChatDraftAccepted(scope, record.revision),
        )
        .sort((a, b) => b.record.updatedAt - a.record.updatedAt)[0]
      if (own) {
        memory.set(scope, own.record)
        draftSources.set(scope, { key: own.key, revision: own.record.revision })
      }
    } catch {
      // The in-memory write below remains available when storage is denied.
    }
  }
  const previous = memory.get(scope)
  const source = draftSources.get(scope)
  const record: RecordValue = {
    value,
    revision: crypto.randomUUID(),
    updatedAt: Math.max(Date.now(), (previous?.updatedAt ?? 0) + 1),
    lineage: previous?.lineage ?? previous?.revision ?? crypto.randomUUID(),
  }
  memory.set(scope, record)
  if (value.length > MAX_VALUE_LENGTH) {
    return { record, durable: false }
  }
  try {
    const candidates: Ancestor[] = [...(previous?.ancestors ?? [])]
    if (source) candidates.push(source)
    if (previous && previous.revision !== source?.revision) {
      candidates.push({ key: recordKey(scope, previous.revision), revision: previous.revision })
    }
    const seen = new Set<string>()
    const ancestors = candidates.filter(({ key, revision }) => {
      if (seen.has(key)) return false
      seen.add(key)
      return parse(localStorage.getItem(key))?.revision === revision
    })
    if (ancestors.length > MAX_RECORDS) return { record, durable: false }
    if (ancestors.length) record.ancestors = ancestors
    const key = recordKey(scope, record.revision)
    const replacing = source && !isChatDraftAccepted(scope, source.revision)
    const occupied = localStorage.length >= MAX_RECORDS ? capacityRecordCount() : 0
    if ((!replacing && occupied >= MAX_RECORDS) || (replacing && occupied > MAX_RECORDS)) {
      if (
        source &&
        previous &&
        source.revision === previous.revision &&
        parse(localStorage.getItem(source.key))?.revision === previous.revision
      ) {
        for (const ancestor of previous.ancestors ?? []) {
          if (!isRevisionKey(ancestor.key)) continue
          try {
            localStorage.removeItem(ancestor.key)
          } catch {
            /* Keep the recoverable copy. */
          }
        }
      }
      reclaimRetiredRecords()
      pruneEmptyScopes()
      migrateLegacySelections()
      if (
        (!replacing && capacityRecordCount() >= MAX_RECORDS) ||
        (replacing && capacityRecordCount() > MAX_RECORDS)
      )
        return { record, durable: false }
    }
    if (localStorage.getItem(key) !== null) return { record, durable: false }
    localStorage.setItem(key, JSON.stringify(record))
    for (const ancestor of ancestors) {
      if (!isRevisionKey(ancestor.key)) continue
      try {
        localStorage.removeItem(ancestor.key)
      } catch {
        // The immutable new record retains this predecessor until a later cleanup or send.
      }
    }
    if (localStorage.length > MAX_RECORDS && capacityRecordCount() > MAX_RECORDS) {
      reclaimRetiredRecords()
      pruneEmptyScopes()
      const immediateStillStored =
        !previous ||
        (source?.revision === previous.revision
          ? parse(localStorage.getItem(source.key))?.revision === previous.revision
          : parse(localStorage.getItem(recordKey(scope, previous.revision)))?.revision ===
            previous.revision)
      if (capacityRecordCount() > MAX_RECORDS && immediateStillStored) {
        try {
          localStorage.removeItem(key)
        } catch {
          // The new revision remains recoverable; later writes see the occupied cap.
        }
        return { record, durable: false }
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
  const current = memory.get(scope)
  if (current?.revision !== revision) {
    notifySettlement(scope, 'superseded')
    return 'superseded'
  }
  let outcome: ChatDraftClearOutcome = 'retired'
  try {
    const source = draftSources.get(scope)
    const ancestors = [...(current.ancestors ?? [])]
    if (source && source.revision !== revision) ancestors.push(source)
    const seen = new Set<string>()
    for (const ancestor of ancestors) {
      if (seen.has(ancestor.revision)) continue
      seen.add(ancestor.revision)
      if (parse(localStorage.getItem(ancestor.key))?.revision !== ancestor.revision) continue
      if (!retireRevision(scope, ancestor.revision, true)) {
        outcome = 'failed'
        break
      }
    }
    if (outcome === 'retired') {
      if (!retireRevision(scope, revision, true)) outcome = 'failed'
      else if (
        storedRecords(scope).some(
          ({ key, record }) => isRevisionKey(key) && record.revision === revision,
        )
      )
        outcome = 'failed'
    }
    if (outcome === 'failed') {
      const remembered = readRetired(scope)
      remembered.add(revision)
      for (const ancestor of ancestors) remembered.add(ancestor.revision)
      acceptedUncleared.set(scope, remembered)
    }
    if (memory.get(scope)?.revision === revision) memory.delete(scope)
    if (draftSources.get(scope)?.revision === revision) draftSources.delete(scope)
  } catch {
    const remembered = readRetired(scope)
    remembered.add(revision)
    for (const ancestor of current.ancestors ?? []) remembered.add(ancestor.revision)
    acceptedUncleared.set(scope, remembered)
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
    for (const entry of storedRecords(`source:${key}`)) {
      if (isRevisionKey(entry.key)) localStorage.removeItem(entry.key)
    }
    // Legacy mutable slots stay intact; a still-open older context can replace one.
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
