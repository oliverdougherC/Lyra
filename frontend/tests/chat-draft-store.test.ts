import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  clearChatDraftIfRevision,
  isChatDraftAccepted,
  readChatDraft,
  readChatSourceSelection,
  resetChatDraftMemory,
  writeChatDraft,
  writeChatSourceSelection,
} from '@/lib/chat-draft-store'

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  resetChatDraftMemory()
})

describe('durable chat drafts', () => {
  it('adopts a restored revision and retires it after unchanged acceptance across module loads', async () => {
    const scope = 'class:1:writer:9:0:7'
    const first = writeChatDraft(scope, 'A saved question')
    vi.resetModules()
    const restored = await import('@/lib/chat-draft-store')
    expect(restored.readChatDraft(scope)?.revision).toBe(first.record.revision)
    expect(restored.clearChatDraftIfRevision(scope, first.record.revision)).toBe(true)
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
    expect(clearChatDraftIfRevision(scope, 'shared')).toBe(true)
    expect(localStorage.getItem(foreignKey)).toContain('Shared restored question')
    resetChatDraftMemory()
    expect(readChatDraft(scope)).toBeNull()
    sessionStorage.clear()
    vi.resetModules()
    const otherWindow = await import('@/lib/chat-draft-store')
    expect(otherWindow.readChatDraft(scope)?.value).toBe('Shared restored question')
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
      expect(clearChatDraftIfRevision(scope, revision)).toBe(true)
    }
    const markerKey = Array.from({ length: localStorage.length }, (_, index) =>
      localStorage.key(index),
    ).find((key) => key?.startsWith('lyra:chat-sent-unretired:v1:'))
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
    expect(clearChatDraftIfRevision(scope, 'shared')).toBe(true)
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
      expect(clearChatDraftIfRevision(scope, submitted.record.revision)).toBe(false)
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
    expect(clearChatDraftIfRevision(scope, first.record.revision)).toBe(true)
    expect(localStorage.getItem(otherKey)).toContain('Other unsent question')
    const followUp = writeChatDraft(scope, 'Follow-up')
    expect(clearChatDraftIfRevision(scope, first.record.revision)).toBe(false)
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

    expect(clearChatDraftIfRevision('class:1:session:7', first.record.revision)).toBe(false)
    expect(readChatDraft('class:1:session:7')).toEqual(followUp.record)
    resetChatDraftMemory()
    expect(readChatDraft('class:1:session:7')?.value).toBe('What about part b?')
  })

  it('retires an accepted revision so a relaunch does not resurrect the submitted words', () => {
    const submitted = writeChatDraft('class:1:new', 'Explain this')
    expect(clearChatDraftIfRevision('class:1:new', submitted.record.revision)).toBe(true)
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
