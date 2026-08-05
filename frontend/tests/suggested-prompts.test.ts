import { describe, expect, it } from 'vitest'

import { buildSuggestedPrompts } from '@/components/chat/suggested-prompts'
import type { FactKind, FactRead } from '@/types'

function fact(overrides: Partial<FactRead> & { kind: FactKind; value: string }): FactRead {
  return {
    id: 1,
    class_id: 1,
    label: 'Topic',
    confidence: 'high',
    confirmed: false,
    rejected: false,
    edited: false,
    source_document_id: 1,
    source_filename: 'homework_1.pdf',
    sources: ['homework_1.pdf'],
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

describe('buildSuggestedPrompts', () => {
  it('never offers the same suggestion twice', () => {
    // The string is the list's identity, as the React key and to the reader. Two topics
    // that differ only in case render one sentence, which is an ordinary state for a
    // profile whose consolidation pass has not run since the duplicates arrived.
    const suggestions = buildSuggestedPrompts([
      fact({ kind: 'topic', value: 'Continuous-Signal Processing', id: 1 }),
      fact({ kind: 'topic', value: 'Continuous-signal processing', id: 2 }),
    ])

    expect(new Set(suggestions).size).toBe(suggestions.length)
    expect(suggestions).toContain('Explain continuous-signal processing')
  })

  it('fills the second topic slot from the next distinct topic', () => {
    const suggestions = buildSuggestedPrompts([
      fact({ kind: 'topic', value: 'Convolution', id: 1 }),
      fact({ kind: 'topic', value: 'convolution', id: 2 }),
      fact({ kind: 'topic', value: 'Laplace transform', id: 3 }),
    ])

    expect(suggestions).toEqual([
      'Explain convolution',
      'Explain laplace transform',
      'What are the main topics in this class?',
    ])
  })

  it('offers at most two topics, leaving room for a fallback', () => {
    const suggestions = buildSuggestedPrompts(
      ['Convolution', 'Sampling', 'Aliasing'].map((value, index) =>
        fact({ kind: 'topic', value, id: index + 1 }),
      ),
    )

    expect(suggestions.filter((one) => one.startsWith('Explain '))).toHaveLength(2)
    expect(suggestions).toHaveLength(3)
  })

  it('treats a fact two documents corroborate as active', () => {
    // The backend promotes it, so a suggestion built from it is one the tutor can answer
    // with the same fact in its own context.
    const suggestions = buildSuggestedPrompts([
      fact({
        kind: 'topic',
        value: 'Region of convergence',
        confidence: 'low',
        sources: ['homework_8.pdf', 'ECE203_homework8_solution.pdf'],
      }),
    ])

    expect(suggestions[0]).toBe('Explain region of convergence')
  })

  it('leaves an unconfirmed single-source low-confidence fact out', () => {
    const suggestions = buildSuggestedPrompts([
      fact({ kind: 'topic', value: 'Homework Assignment 5', confidence: 'low' }),
    ])

    expect(suggestions.some((one) => one.includes('Homework Assignment'))).toBe(false)
  })

  it('keeps acronyms in their own case', () => {
    const suggestions = buildSuggestedPrompts([fact({ kind: 'topic', value: 'LTI Systems' })])

    expect(suggestions[0]).toBe('Explain LTI systems')
  })

  it('falls back entirely on an empty profile', () => {
    expect(buildSuggestedPrompts([])).toEqual([
      'What are the main topics in this class?',
      'Summarize the material I uploaded.',
      'What should I study first?',
    ])
  })
})
