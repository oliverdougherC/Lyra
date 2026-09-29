import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  clearChatDraftIfRevision,
  getChatDraftSettlementVersion,
  isChatDraftAccepted,
  readChatDraft,
  readChatSourceSelection,
  resetChatDraftMemory,
  subscribeChatDraftSettlements,
  writeChatDraft,
  writeChatSourceSelection,
} from '@/lib/chat-draft-store'

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  resetChatDraftMemory()
})

describe('durable chat drafts', () => {
  it('retires an adopted draft across fresh page sessions without resurrecting the first writer', async () => {
    const scope = 'class:50:writer:3:0:new'
    const original = writeChatDraft(scope, 'A question left behind')
    sessionStorage.clear()
    vi.resetModules()
    const returned = await import('@/lib/chat-draft-store')
    expect(returned.readChatDraft(scope)?.revision).toBe(original.record.revision)
    expect(returned.clearChatDraftIfRevision(scope, original.record.revision)).toBe('retired')
    sessionStorage.clear()
    vi.resetModules()
    const next = await import('@/lib/chat-draft-store')
    expect(next.readChatDraft(scope)).toBeNull()
  })

  it('does not expose pre-edit wording after an adopted edit is sent', async () => {
    const scope = 'class:51:writer:3:0:new'
    writeChatDraft(scope, 'Original wording')
    sessionStorage.clear()
    vi.resetModules()
    const returned = await import('@/lib/chat-draft-store')
    expect(returned.readChatDraft(scope)?.value).toBe('Original wording')
    const edited = returned.writeChatDraft(scope, 'Edited wording')
    expect(edited.durable).toBe(true)
    expect(returned.clearChatDraftIfRevision(scope, edited.record.revision)).toBe('retired')
    sessionStorage.clear()
    vi.resetModules()
    const next = await import('@/lib/chat-draft-store')
    expect(next.readChatDraft(scope)).toBeNull()
  })

  it('reports failed retirement when an adopted edit could not replace its predecessor', async () => {
    const scope = 'class:56:writer:3:0:new'
    writeChatDraft(scope, 'Original unsent wording')
    sessionStorage.clear()
    vi.resetModules()
    const returned = await import('@/lib/chat-draft-store')
    expect(returned.readChatDraft(scope)?.value).toBe('Original unsent wording')
    const save = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('Storage refused', 'QuotaExceededError')
    })
    const edited = returned.writeChatDraft(scope, 'Edited wording sent')
    expect(edited.durable).toBe(false)
    expect(returned.clearChatDraftIfRevision(scope, edited.record.revision)).toBe('failed')
    save.mockRestore()
    sessionStorage.clear()
    vi.resetModules()
    const next = await import('@/lib/chat-draft-store')
    expect(next.readChatDraft(scope)?.value).toBe('Original unsent wording')
  })

  it('retires an adopted predecessor when storage recovers before send settlement', async () => {
    const scope = 'class:57:writer:3:0:new'
    writeChatDraft(scope, 'Original unsent wording')
    sessionStorage.clear()
    vi.resetModules()
    const returned = await import('@/lib/chat-draft-store')
    expect(returned.readChatDraft(scope)?.value).toBe('Original unsent wording')
    const save = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('Storage refused', 'QuotaExceededError')
    })
    const edited = returned.writeChatDraft(scope, 'Edited wording sent')
    expect(edited.durable).toBe(false)
    save.mockRestore()
    expect(returned.clearChatDraftIfRevision(scope, edited.record.revision)).toBe('retired')
    sessionStorage.clear()
    vi.resetModules()
    const next = await import('@/lib/chat-draft-store')
    expect(next.readChatDraft(scope)).toBeNull()
  })

  it('does not fill the cap through hundreds of fresh-session adoptions', async () => {
    const scope = 'class:52:writer:3:0:new'
    writeChatDraft(scope, 'First wording')
    for (let index = 0; index < 240; index += 1) {
      sessionStorage.clear()
      vi.resetModules()
      const lifetime = await import('@/lib/chat-draft-store')
      expect(lifetime.readChatDraft(scope)).not.toBeNull()
      expect(lifetime.writeChatDraft(scope, `Question ${index}`).durable).toBe(true)
    }
    expect(writeChatDraft('class:53:new', 'Another prompt').durable).toBe(true)
    expect(
      Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index)).filter(
        (key) => key?.startsWith(`lyra:unsent-chat:v1:${encodeURIComponent(scope)}:`),
      ),
    ).toHaveLength(1)
  })

  it('preserves a live writer’s different revision while an adopted copy is edited and sent', async () => {
    const scope = 'class:56:new'
    const original = writeChatDraft(scope, 'Shared starting point')
    sessionStorage.clear()
    vi.resetModules()
    const adoptingWindow = await import('@/lib/chat-draft-store')
    expect(adoptingWindow.readChatDraft(scope)?.revision).toBe(original.record.revision)
    const activeFollowUp = writeChatDraft(scope, 'A’s different unsent work')
    const adoptedEdit = adoptingWindow.writeChatDraft(scope, 'B’s sent edit')
    expect(adoptedEdit.durable).toBe(true)
    expect(adoptingWindow.clearChatDraftIfRevision(scope, adoptedEdit.record.revision)).toBe(
      'retired',
    )
    expect(readChatDraft(scope)).toEqual(activeFollowUp.record)
    sessionStorage.clear()
    vi.resetModules()
    const next = await import('@/lib/chat-draft-store')
    expect(next.readChatDraft(scope)?.value).toBe('A’s different unsent work')
  })

  it('notifies subscribers of settlement outcomes without notifying for typing', () => {
    const scope = 'class:57:new'
    const notifications = vi.fn()
    const unsubscribe = subscribeChatDraftSettlements(notifications)
    const sent = writeChatDraft(scope, 'Sent wording')
    expect(notifications).not.toHaveBeenCalled()
    writeChatDraft(scope, 'Follow-up wording')
    expect(clearChatDraftIfRevision(scope, sent.record.revision)).toBe('superseded')
    expect(getChatDraftSettlementVersion(scope)).toBe(1)
    expect(notifications).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('treats a protected newer draft as supersession rather than a retirement failure', () => {
    const scope = 'class:54:new'
    const first = writeChatDraft(scope, 'Sent wording')
    const next = writeChatDraft(scope, 'New unsent wording')
    expect(clearChatDraftIfRevision(scope, first.record.revision)).toBe('superseded')
    expect(readChatDraft(scope)).toEqual(next.record)
  })

  it('uses in-memory acknowledgement when storage properties themselves deny access', () => {
    const scope = 'class:55:new'
    const draft = writeChatDraft(scope, 'Sent while storage locks')
    const local = vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => {
      throw new DOMException('Storage denied', 'SecurityError')
    })
    const session = vi.spyOn(window, 'sessionStorage', 'get').mockImplementation(() => {
      throw new DOMException('Storage denied', 'SecurityError')
    })
    try {
      expect(clearChatDraftIfRevision(scope, draft.record.revision)).toBe('failed')
      expect(isChatDraftAccepted(scope, draft.record.revision)).toBe(true)
      expect(readChatDraft(scope)).toBeNull()
      expect(writeChatDraft(scope, 'A recoverable follow-up').durable).toBe(false)
      expect(readChatDraft(scope)?.value).toBe('A recoverable follow-up')
    } finally {
      local.mockRestore()
      session.mockRestore()
    }
  })

  it('adopts a restored revision and retires it after unchanged acceptance across module loads', async () => {
    const scope = 'class:1:writer:9:0:7'
    const first = writeChatDraft(scope, 'A saved question')
    vi.resetModules()
    const restored = await import('@/lib/chat-draft-store')
    expect(restored.readChatDraft(scope)?.revision).toBe(first.record.revision)
    expect(restored.clearChatDraftIfRevision(scope, first.record.revision)).toBe('retired')
    vi.resetModules()
    const relaunched = await import('@/lib/chat-draft-store')
    expect(relaunched.readChatDraft(scope)?.value ?? '').toBe('')
  })

  it('keeps one writer record across hundreds of module lifetimes and leaves capacity for new prompts', async () => {
    for (let index = 0; index < 240; index += 1) {
      vi.resetModules()
      const lifetime = await import('@/lib/chat-draft-store')
      expect(lifetime.writeChatDraft('class:1:writer:9:0:new', `Question ${index}`).durable).toBe(
        true,
      )
    }
    expect(localStorage.length).toBe(1)
    expect(writeChatDraft('class:2:tutor:0:new', 'Another class').durable).toBe(true)
  })

  it('acknowledges an adopted revision without deleting an active foreign writer', async () => {
    const scope = 'class:2:writer:15:0:new'
    const foreignKey = `lyra:unsent-chat:v1:${encodeURIComponent(scope)}:other-window`
    localStorage.setItem(
      foreignKey,
      JSON.stringify({ value: 'Shared restored question', revision: 'shared', updatedAt: 1 }),
    )
    expect(readChatDraft(scope)?.value).toBe('Shared restored question')
    expect(clearChatDraftIfRevision(scope, 'shared')).toBe('retired')
    expect(localStorage.getItem(foreignKey)).toContain('Shared restored question')
    resetChatDraftMemory()
    expect(readChatDraft(scope)).toBeNull()
    sessionStorage.clear()
    vi.resetModules()
    const otherWindow = await import('@/lib/chat-draft-store')
    expect(otherWindow.readChatDraft(scope)).toBeNull()
    localStorage.setItem(
      foreignKey,
      JSON.stringify({ value: 'Other window follow-up', revision: 'follow-up', updatedAt: 2 }),
    )
    resetChatDraftMemory()
    expect(readChatDraft(scope)?.value).toBe('Other window follow-up')
    otherWindow.resetChatDraftMemory()
    expect(otherWindow.readChatDraft(scope)?.value).toBe('Other window follow-up')
  })

  it('keeps shared-window acknowledgements bounded to revisions still stored', () => {
    const scope = 'class:11:tutor:0:7'
    const foreignKey = `lyra:unsent-chat:v1:${encodeURIComponent(scope)}:foreign-window`
    for (let index = 0; index < 210; index += 1) {
      const revision = `shared-${index}`
      localStorage.setItem(
        foreignKey,
        JSON.stringify({ value: `Question ${index}`, revision, updatedAt: index + 1 }),
      )
      expect(readChatDraft(scope)?.revision).toBe(revision)
      expect(clearChatDraftIfRevision(scope, revision)).toBe('retired')
    }
    const markerKey = Array.from({ length: localStorage.length }, (_, index) =>
      localStorage.key(index),
    ).find((key) => key?.startsWith('lyra:chat-draft-retired:v1:'))
    expect(markerKey).toBeDefined()
    expect(JSON.parse(localStorage.getItem(markerKey!) ?? '[]')).toEqual(['shared-209'])
    expect(localStorage.getItem(foreignKey)).toContain('Question 209')
  })

  it('keeps a newer local follow-up visible after accepting a foreign restored revision', () => {
    const scope = 'class:12:tutor:0:7'
    const foreignKey = `lyra:unsent-chat:v1:${encodeURIComponent(scope)}:foreign-window`
    localStorage.setItem(
      foreignKey,
      JSON.stringify({ value: 'Accepted', revision: 'shared', updatedAt: 1 }),
    )
    expect(readChatDraft(scope)?.value).toBe('Accepted')
    expect(clearChatDraftIfRevision(scope, 'shared')).toBe('retired')
    const followUp = writeChatDraft(scope, 'A separate follow-up')
    resetChatDraftMemory()
    expect(readChatDraft(scope)).toEqual(followUp.record)
    expect(localStorage.getItem(foreignKey)).toContain('Accepted')
  })

  it('reports a refused retirement and keeps the accepted record without offering a resend', () => {
    const scope = 'class:3:new'
    const submitted = writeChatDraft(scope, 'Do not lose this')
    const remove = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new DOMException('Storage refused', 'SecurityError')
    })
    try {
      expect(clearChatDraftIfRevision(scope, submitted.record.revision)).toBe('failed')
      expect(
        Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index)).some(
          (key) => key?.startsWith(`lyra:unsent-chat:v1:${encodeURIComponent(scope)}:`),
        ),
      ).toBe(true)
      expect(readChatDraft(scope)).toBeNull()
      expect(isChatDraftAccepted(scope, submitted.record.revision)).toBe(true)
    } finally {
      remove.mockRestore()
    }
  })

  it('keeps another active writer and a newer follow-up while retiring only the accepted revision', () => {
    const scope = 'class:4:tutor:0:7'
    const first = writeChatDraft(scope, 'Accepted question')
    const otherKey = 'lyra:unsent-chat:v1:class%3A4%3Atutor%3A0%3A7:other-window'
    localStorage.setItem(
      otherKey,
      JSON.stringify({ value: 'Other unsent question', revision: 'other', updatedAt: 1 }),
    )
    expect(clearChatDraftIfRevision(scope, first.record.revision)).toBe('retired')
    expect(localStorage.getItem(otherKey)).toContain('Other unsent question')
    const followUp = writeChatDraft(scope, 'Follow-up')
    expect(clearChatDraftIfRevision(scope, first.record.revision)).toBe('superseded')
    expect(readChatDraft(scope)).toEqual(followUp.record)
  })

  it('keeps source choices outside the unsent cap across hundreds of lifetimes and migrates legacy choices', async () => {
    const key = 'lyra:class:1:chat-selected-document'
    writeChatDraft(`source:${key}`, '42')
    expect(readChatSourceSelection(key)).toBe('42')
    for (let index = 0; index < 240; index += 1) {
      vi.resetModules()
      const lifetime = await import('@/lib/chat-draft-store')
      expect(lifetime.writeChatSourceSelection(key, String(index + 1))).toBe(true)
    }
    expect(localStorage.length).toBe(1)
    expect(readChatSourceSelection(key)).toBe('240')
    expect(writeChatDraft('class:8:new', 'Still room for a prompt').durable).toBe(true)
  })

  it('leaves the old selection readable when preference storage is refused', () => {
    const key = 'lyra:class:9:chat-selected-document'
    writeChatDraft(`source:${key}`, '12')
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('Quota exceeded', 'QuotaExceededError')
    })
    try {
      expect(writeChatSourceSelection(key, '13')).toBe(false)
      expect(readChatSourceSelection(key)).toBe('12')
    } finally {
      setItem.mockRestore()
    }
  })

  it('reclaims a full legacy selection store without touching another window’s prompt', () => {
    for (let index = 0; index < 127; index += 1) {
      localStorage.setItem(
        `lyra:unsent-chat:v1:${encodeURIComponent(`source:lyra:class:${index}:chat-selected-document`)}:old-window`,
        JSON.stringify({ value: 'all', revision: `choice-${index}`, updatedAt: index }),
      )
    }
    const other = 'lyra:unsent-chat:v1:class%3A6%3Anew:other-window'
    localStorage.setItem(
      other,
      JSON.stringify({ value: 'Another window’s words', revision: 'other', updatedAt: 128 }),
    )
    expect(writeChatDraft('class:7:new', 'My new prompt').durable).toBe(true)
    expect(localStorage.getItem(other)).toContain('Another window’s words')
    expect(readChatSourceSelection('lyra:class:41:chat-selected-document')).toBe('all')
  })

  it('refuses a full store of foreign unsent questions without deleting any of them', () => {
    for (let index = 0; index < 128; index += 1) {
      localStorage.setItem(
        `lyra:unsent-chat:v1:${encodeURIComponent(`class:${index}:new`)}:foreign-window`,
        JSON.stringify({
          value: `Unsent ${index}`,
          revision: `foreign-${index}`,
          updatedAt: index,
        }),
      )
    }
    const saved = writeChatDraft('class:129:new', 'Keep this in the window')
    expect(saved.durable).toBe(false)
    expect(readChatDraft('class:129:new')?.value).toBe('Keep this in the window')
    expect(localStorage.length).toBe(128)
    expect(localStorage.getItem('lyra:unsent-chat:v1:class%3A0%3Anew:foreign-window')).toContain(
      'Unsent 0',
    )
  })
  it('keeps the newer follow-up when an older send settles', () => {
    const first = writeChatDraft('class:1:session:7', 'Solve part a')
    const followUp = writeChatDraft('class:1:session:7', 'What about part b?')

    expect(clearChatDraftIfRevision('class:1:session:7', first.record.revision)).toBe('superseded')
    expect(readChatDraft('class:1:session:7')).toEqual(followUp.record)
    resetChatDraftMemory()
    expect(readChatDraft('class:1:session:7')?.value).toBe('What about part b?')
  })

  it('retires an accepted revision so a relaunch does not resurrect the submitted words', () => {
    const submitted = writeChatDraft('class:1:new', 'Explain this')
    expect(clearChatDraftIfRevision('class:1:new', submitted.record.revision)).toBe('retired')
    resetChatDraftMemory()
    expect(readChatDraft('class:1:new')?.value ?? '').toBe('')
  })

  it('preserves the current window draft in memory when storage refuses writes', () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('Quota exceeded', 'QuotaExceededError')
    })
    try {
      const saved = writeChatDraft('class:2:new', 'Keep these words')
      expect(saved.durable).toBe(false)
      expect(readChatDraft('class:2:new')?.value).toBe('Keep these words')
    } finally {
      setItem.mockRestore()
    }
  })

  it('uses separate keys for independent window writers', () => {
    localStorage.setItem(
      'lyra:unsent-chat:v1:class%3A4%3Anew:another-window',
      JSON.stringify({ value: 'Other window', revision: 'other', updatedAt: 1 }),
    )
    const own = writeChatDraft('class:4:new', 'This window')
    expect(own.durable).toBe(true)
    expect(localStorage.getItem('lyra:unsent-chat:v1:class%3A4%3Anew:another-window')).toContain(
      'Other window',
    )
    expect(readChatDraft('class:4:new')?.value).toBe('This window')
  })

  it('reclaims settled scopes before the record cap without deleting unsent text', () => {
    for (let index = 0; index < 127; index += 1) {
      localStorage.setItem(
        `lyra:unsent-chat:v1:settled-${index}:another-window`,
        JSON.stringify({ value: '', revision: `empty-${index}`, updatedAt: index }),
      )
    }
    localStorage.setItem(
      'lyra:unsent-chat:v1:class%3A4%3Anew:another-window',
      JSON.stringify({ value: 'Keep this prompt', revision: 'live', updatedAt: 128 }),
    )
    expect(writeChatDraft('class:5:new', 'New prompt').durable).toBe(true)
    expect(localStorage.getItem('lyra:unsent-chat:v1:class%3A4%3Anew:another-window')).toContain(
      'Keep this prompt',
    )
  })
})
