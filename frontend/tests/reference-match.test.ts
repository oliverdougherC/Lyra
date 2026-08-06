import { describe, expect, it } from 'vitest'

import { assignmentKey, looksLikeSolutions, suggestReferences } from '@/lib/reference-match'
import type { DocumentRead, DocumentState } from '@/types'

function document(id: number, filename: string, state: DocumentState = 'ready'): DocumentRead {
  return {
    id,
    class_id: 1,
    filename,
    mime: 'application/pdf',
    byte_size: 1024,
    state,
    stage_detail: null,
    pages_total: 4,
    pages_done: 4,
    pages_skipped: 0,
    pages_failed: 0,
    recognize: false,
    error_message: null,
    created_at: '2026-08-04 00:00:00',
  }
}

describe('assignmentKey', () => {
  it.each([
    ['homework_5.pdf', 'homework:5'],
    ['ECE203_homework5_solution.pdf', 'homework:5'],
    ['HW5 Solutions.pdf', 'homework:5'],
    ['Problem Set 4.pdf', 'homework:4'],
    ['pset04-answers.pdf', 'homework:4'],
    ['assignment-7.docx', 'homework:7'],
    ['ECE203_Lab0.pdf', 'lab:0'],
    ['week 12 quiz.pdf', 'week:12'],
  ])('reads %s as %s', (filename, key) => {
    expect(assignmentKey(filename)).toBe(key)
  })

  it('refuses a name with no number', () => {
    // `homework.pdf` naming a specific assignment is a guess, and a wrong one would
    // pair it with every other homework in the class.
    expect(assignmentKey('homework.pdf')).toBeNull()
    expect(assignmentKey('solutions.pdf')).toBeNull()
  })

  it('treats a leading zero as cosmetic', () => {
    expect(assignmentKey('lab01.pdf')).toBe(assignmentKey('lab1.pdf'))
  })
})

describe('looksLikeSolutions', () => {
  it.each(['hw3_solutions.pdf', 'HW3 Answer Key.pdf', 'homework3-soln.pdf', 'ps2 worked.pdf'])(
    'recognises %s',
    (filename) => {
      expect(looksLikeSolutions(filename)).toBe(true)
    },
  )

  it.each(['homework_5.pdf', 'syllabus.pdf', 'lecture-notes-3.pdf'])(
    'leaves %s alone',
    (filename) => {
      expect(looksLikeSolutions(filename)).toBe(false)
    },
  )
})

describe('suggestReferences', () => {
  const homework5 = document(1, 'homework_5.pdf')
  const solutions5 = document(2, 'ECE203_homework5_solution.pdf')
  const solutions4 = document(3, 'ECE203_homework4_solution.pdf')
  const lab5 = document(4, 'ECE203_Lab5_solution.pdf')
  const all = [homework5, solutions5, solutions4, lab5]

  it('offers the answers to the chosen set', () => {
    const suggestions = suggestReferences(all, [homework5.id], [])

    expect(suggestions).toHaveLength(1)
    expect(suggestions[0].document.id).toBe(solutions5.id)
    // The offer has to say what it matched, or the student cannot judge it.
    expect(suggestions[0].because.id).toBe(homework5.id)
  })

  it('does not pair a lab with a homework that shares its number', () => {
    // Lab 5 and homework 5 are different pieces of work. Matching on the number alone
    // would hand the solver the wrong reference and it would look plausible.
    expect(suggestReferences(all, [homework5.id], []).map((one) => one.document.id)).not.toContain(
      lab5.id,
    )
  })

  it('says nothing until a problem set is chosen', () => {
    expect(suggestReferences(all, [], [])).toEqual([])
  })

  it('leaves out what is already picked on either side', () => {
    expect(suggestReferences(all, [homework5.id], [solutions5.id])).toEqual([])
    expect(suggestReferences(all, [homework5.id, solutions5.id], [])).toEqual([])
  })

  it('does not offer a document Lyra cannot read', () => {
    const pending = [homework5, document(2, 'ECE203_homework5_solution.pdf', 'embedding')]

    expect(suggestReferences(pending, [homework5.id], [])).toEqual([])
  })

  it('offers nothing when no name matches', () => {
    expect(suggestReferences([homework5, solutions4], [homework5.id], [])).toEqual([])
  })
})
