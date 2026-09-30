import { describe, expect, it } from 'vitest'

import {
  documentKind,
  effectiveDocumentName,
  documentNameNote,
  matchesDocumentName,
  sortDocuments,
} from '@/lib/document-display'
import type { DocumentRead } from '@/types'

const row = (id: number, filename: string, values: Partial<DocumentRead> = {}) =>
  ({
    id,
    filename,
    nickname: null,
    display_name: filename,
    mime: 'application/pdf',
    created_at: '2026-09-29 00:00:00',
    ...values,
  }) as DocumentRead

describe('document presentation identity', () => {
  it('uses a nonblank nickname and preserves the original for kind and search', () => {
    const document = row(1, 'LADW_2026_08-31.pdf', {
      nickname: 'Textbook',
      display_name: 'Textbook',
    })
    expect(effectiveDocumentName(document)).toBe('Textbook')
    expect(matchesDocumentName(document, 'Textbook')).toBe(true)
    expect(matchesDocumentName(document, 'LADW_2026')).toBe(true)
    expect(documentKind(row(2, 'notes.pdf', { nickname: 'notes.txt' }))).toBe('PDF')
  })

  it('adds an original-name note only for collisions and an ID fallback for equal originals', () => {
    const first = row(1, 'one.pdf', { nickname: 'Textbook' })
    const second = row(2, 'two.pdf', { nickname: 'Textbook' })
    expect(documentNameNote(first, [first, second])).toBe('Original: one.pdf')
    expect(documentNameNote(first, [first])).toBeNull()
    const duplicate = row(3, 'one.pdf', { nickname: 'Textbook' })
    expect(documentNameNote(first, [first, duplicate])).toMatch(
      /^Original: one\.pdf · Added .+ · Document #1$/,
    )
  })

  it('sorts naturally by effective name with stable identity ties', () => {
    const input = [
      row(3, 'chapter10.pdf'),
      row(2, 'chapter2.pdf'),
      row(1, 'CHAPTER2.pdf'),
      row(4, 'opaque.pdf', { nickname: 'Álgebra' }),
    ]
    expect(sortDocuments(input, 'alphabetical').map((doc) => doc.id)).toEqual([4, 1, 2, 3])
    expect(input.map((doc) => doc.id)).toEqual([3, 2, 1, 4])
  })

  it('uses actual creation time and file kind with deterministic invalid-date ties', () => {
    const input = [
      row(3, 'later.md', { mime: 'text/markdown', created_at: '2026-09-30 00:00:00' }),
      row(2, 'notes.pdf', { nickname: 'notes.txt', created_at: 'invalid' }),
      row(1, 'photo.jpeg', { mime: 'image/jpeg', created_at: '2026-09-29 00:00:00' }),
      row(4, 'photo.jpg', { mime: 'image/jpeg', created_at: 'invalid' }),
      row(5, 'legacy.txt', { mime: 'text/plain', created_at: undefined }),
    ]
    expect(sortDocuments(input, 'date-added').map((doc) => doc.id)).toEqual([3, 1, 2, 4, 5])
    expect(sortDocuments(input, 'kind').map((doc) => doc.id)).toEqual([1, 4, 3, 2, 5])
  })

  it('breaks duplicate aliases and equal timestamps by document ID', () => {
    const input = [
      row(9, 'opaque-9.pdf', { nickname: 'Textbook' }),
      row(3, 'opaque-3.pdf', { nickname: 'Textbook' }),
    ]
    expect(sortDocuments(input, 'alphabetical').map((doc) => doc.id)).toEqual([3, 9])
    expect(sortDocuments(input, 'date-added').map((doc) => doc.id)).toEqual([3, 9])
  })
})
