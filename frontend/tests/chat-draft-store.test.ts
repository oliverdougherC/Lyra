import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  clearChatDraftIfRevision,
  readChatDraft,
  resetChatDraftMemory,
  writeChatDraft,
} from '@/lib/chat-draft-store'

beforeEach(() => {
  localStorage.clear()
  resetChatDraftMemory()
})

describe('durable chat drafts', () => {
  it('keeps the newer follow-up when an older send settles', () => {
    const first = writeChatDraft('class:1:session:7', 'Solve part a')
    const followUp = writeChatDraft('class:1:session:7', 'What about part b?')

    expect(clearChatDraftIfRevision('class:1:session:7', first.record.revision)).toBe(false)
    expect(readChatDraft('class:1:session:7')).toEqual(followUp.record)
    resetChatDraftMemory()
    expect(readChatDraft('class:1:session:7')?.value).toBe('What about part b?')
  })

  it('writes a settled tombstone so a relaunch does not resurrect the submitted words', () => {
    const submitted = writeChatDraft('class:1:new', 'Explain this')
    expect(clearChatDraftIfRevision('class:1:new', submitted.record.revision)).toBe(true)
    resetChatDraftMemory()
    expect(readChatDraft('class:1:new')?.value).toBe('')
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
