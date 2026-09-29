import { beforeEach, expect, it } from 'vitest'

import { readSavedDocumentSelection } from '@/app/classes/[id]/chat/page'
import { resetChatDraftMemory, writeChatDraft } from '@/lib/chat-draft-store'

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  resetChatDraftMemory()
})

it('restores an explicit class document selection after a reload', () => {
  sessionStorage.setItem('lyra:class:1:chat-selected-document', '42')
  expect(readSavedDocumentSelection('lyra:class:1:chat-selected-document')).toBe(42)
  expect(readSavedDocumentSelection('lyra:class:2:chat-selected-document')).toBeNull()
})

it('restores source scope after session storage disappears on relaunch', () => {
  const key = 'lyra:class:1:chat-selected-document'
  writeChatDraft(`source:${key}`, '42')
  sessionStorage.clear()
  resetChatDraftMemory()
  expect(readSavedDocumentSelection(key)).toBe(42)
})

it('does not restore a cleared or invalid selection', () => {
  const key = 'lyra:class:1:chat-selected-document'
  sessionStorage.setItem(key, 'all')
  expect(readSavedDocumentSelection(key)).toBeNull()
  sessionStorage.setItem(key, 'bad')
  expect(readSavedDocumentSelection(key)).toBeNull()
})
