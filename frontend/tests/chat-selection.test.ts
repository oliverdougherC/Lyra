import { beforeEach, expect, it } from 'vitest'

import { readSavedDocumentSelection } from '@/app/classes/[id]/chat/page'

beforeEach(() => sessionStorage.clear())

it('restores an explicit class document selection after a reload', () => {
  sessionStorage.setItem('lyra:class:1:chat-selected-document', '42')
  expect(readSavedDocumentSelection('lyra:class:1:chat-selected-document')).toBe(42)
  expect(readSavedDocumentSelection('lyra:class:2:chat-selected-document')).toBeNull()
})

it('does not restore a cleared or invalid selection', () => {
  const key = 'lyra:class:1:chat-selected-document'
  sessionStorage.setItem(key, 'all')
  expect(readSavedDocumentSelection(key)).toBeNull()
  sessionStorage.setItem(key, 'bad')
  expect(readSavedDocumentSelection(key)).toBeNull()
})
