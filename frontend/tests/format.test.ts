import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  formatCount,
  formatFileSize,
  formatRelativeTime,
  formatSessionFallbackTitle,
  initialsFor,
  parseTimestamp,
  truncateMiddle,
} from '@/lib/format'

describe('parseTimestamp', () => {
  it('reads a naive SQLite timestamp as UTC', () => {
    // Without the appended Z this is read as local time and every timestamp shows hours off.
    expect(parseTimestamp('2026-08-03 12:00:00').toISOString()).toBe('2026-08-03T12:00:00.000Z')
  })

  it('leaves an explicit UTC offset alone', () => {
    expect(parseTimestamp('2026-08-03T12:00:00Z').toISOString()).toBe('2026-08-03T12:00:00.000Z')
  })

  it('respects a numeric offset rather than reinterpreting it', () => {
    expect(parseTimestamp('2026-08-03T12:00:00-05:00').toISOString()).toBe(
      '2026-08-03T17:00:00.000Z',
    )
  })
})

describe('formatRelativeTime', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-03T12:00:00Z'))
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('describes the current moment without a unit count', () => {
    expect(formatRelativeTime('2026-08-03 12:00:00')).toBe('now')
  })

  it('describes minutes', () => {
    expect(formatRelativeTime('2026-08-03 11:30:00')).toBe('30 minutes ago')
  })

  it('describes hours', () => {
    expect(formatRelativeTime('2026-08-03 09:00:00')).toBe('3 hours ago')
  })

  it('describes days', () => {
    expect(formatRelativeTime('2026-08-01 12:00:00')).toBe('2 days ago')
  })

  it('describes months', () => {
    expect(formatRelativeTime('2026-05-03 12:00:00')).toBe('3 months ago')
  })

  it('handles a future timestamp without dropping the direction', () => {
    expect(formatRelativeTime('2026-08-03 13:00:00')).toBe('in 1 hour')
  })
})

describe('formatSessionFallbackTitle', () => {
  it('names an untitled conversation by date, not by position', () => {
    // Positional names renumber whenever a conversation is added or removed.
    expect(formatSessionFallbackTitle('2026-08-03 12:00:00')).toBe('Chat from Aug 3')
  })
})

describe('formatFileSize', () => {
  it.each([
    [0, '0 B'],
    [512, '512 B'],
    [1023, '1023 B'],
    [1024, '1 KB'],
    [1536, '2 KB'],
    [1048576, '1.0 MB'],
    [5242880, '5.0 MB'],
  ])('formats %i bytes as %s', (bytes, expected) => {
    expect(formatFileSize(bytes)).toBe(expected)
  })
})

describe('formatCount', () => {
  it('uses the singular for exactly one', () => {
    expect(formatCount(1, 'document')).toBe('1 document')
  })

  it('uses the plural for zero', () => {
    expect(formatCount(0, 'document')).toBe('0 documents')
  })

  it('accepts an irregular plural', () => {
    expect(formatCount(3, 'entry', 'entries')).toBe('3 entries')
  })
})

describe('truncateMiddle', () => {
  it('leaves a short name alone', () => {
    expect(truncateMiddle('notes.pdf')).toBe('notes.pdf')
  })

  it('keeps the extension visible, which is what identifies the row', () => {
    const result = truncateMiddle('a-very-long-homework-filename-indeed.pdf')
    expect(result).toContain('...')
    expect(result.endsWith('.pdf')).toBe(true)
  })

  it('never exceeds the maximum, counting the ellipsis', () => {
    // The ellipsis is three characters and has to come out of the budget, not sit beside it.
    for (const max of [4, 10, 20, 32]) {
      expect(truncateMiddle('abcdefghijklmnopqrstuvwxyz0123456789', max)).toHaveLength(max)
    }
  })
})

describe('initialsFor', () => {
  it('takes the subject prefix of a course code', () => {
    // `ECE 203` marks as `ECE`. First-letter-per-word would render `E2`, which identifies
    // nothing and collides with every other course in the department.
    expect(initialsFor('Continuous-Time Signals', 'ECE 203')).toBe('ECE')
  })

  it('caps the prefix at three characters', () => {
    expect(initialsFor('Thermodynamics', 'MECHENG 240')).toBe('MEC')
  })

  it('uppercases a lowercase code', () => {
    expect(initialsFor('Signals', 'ece 203')).toBe('ECE')
  })

  it('falls back to name initials when there is no code', () => {
    expect(initialsFor('Linear Algebra', null)).toBe('LA')
  })

  it('falls back to name initials when the code is blank', () => {
    expect(initialsFor('Linear Algebra', '   ')).toBe('LA')
  })

  it('uses the first two letters of a single-word name', () => {
    expect(initialsFor('Thermodynamics', null)).toBe('TH')
  })

  it('handles a code that starts with digits', () => {
    expect(initialsFor('Calculus', '203')).toBe('203')
  })
})
