import type { DocumentRead } from '@/types'

/**
 * Spotting the answer key that belongs to a problem set.
 *
 * Students name files the way people name files. `homework_5.pdf` sits in the same folder
 * as `ECE203_homework5_solution.pdf`, and asking someone to find the second in a list of
 * seventeen after they have just picked the first is busywork a computer should do.
 *
 * The matching is deliberately narrow. It fires only when two filenames agree on both the
 * kind of assignment and its number, and only when one of them is marked as worked
 * answers. A near miss costs nothing, because nothing is ever selected on the student's
 * behalf; a false positive on a loose rule would cost them a wrong reference silently
 * steering an entire solve.
 */

/** Words that mark a file as somebody's worked answers rather than the questions. */
const SOLUTION_WORDS = new Set([
  'solution',
  'solutions',
  'soln',
  'solns',
  'sol',
  'sols',
  'solved',
  'answer',
  'answers',
  'answerkey',
  'key',
  'worked',
  'rubric',
])

/**
 * Assignment nouns, each mapped to a canonical kind so that `hw4`, `Homework 4`, and
 * `Problem Set 4` are recognised as the same assignment. Kinds stay separate from one
 * another: lab 5 and homework 5 are different pieces of work that happen to share a
 * number, and treating them as a pair is exactly the wrong guess.
 */
const KINDS: Record<string, string> = {
  homework: 'homework',
  hw: 'homework',
  hwk: 'homework',
  hmwk: 'homework',
  assignment: 'homework',
  assign: 'homework',
  asgn: 'homework',
  pset: 'homework',
  ps: 'homework',
  set: 'homework',
  lab: 'lab',
  quiz: 'quiz',
  exam: 'exam',
  midterm: 'exam',
  final: 'exam',
  chapter: 'chapter',
  ch: 'chapter',
  chap: 'chapter',
  week: 'week',
  wk: 'week',
  project: 'project',
  proj: 'project',
  worksheet: 'worksheet',
  discussion: 'discussion',
  recitation: 'recitation',
  tutorial: 'tutorial',
}

/** Multi-word names, collapsed before tokenising so they survive as one noun. */
const PHRASES: [RegExp, string][] = [
  [/\bproblem\s+set\b/g, 'pset'],
  [/\bproblem\s+sheet\b/g, 'pset'],
  [/\banswer\s+key\b/g, 'answerkey'],
  [/\bhome\s+work\b/g, 'homework'],
]

/**
 * A filename reduced to lowercase words, with the extension dropped and digits split
 * away from the letters they are glued to, so `homework5` and `homework_5` tokenise
 * alike.
 */
function tokenize(filename: string): string[] {
  let text = filename
    .replace(/\.[^.]+$/, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .replace(/([a-z])(\d)/g, '$1 $2')
    .replace(/(\d)([a-z])/g, '$1 $2')
  for (const [pattern, replacement] of PHRASES) {
    text = text.replace(pattern, replacement)
  }
  return text.split(' ').filter(Boolean)
}

/**
 * The assignment a filename refers to, as `kind:number`, or null when it does not name
 * one. The number is required: without it `homework.pdf` would claim kinship with every
 * other homework in the class.
 */
export function assignmentKey(filename: string): string | null {
  const tokens = tokenize(filename)
  for (let index = 0; index < tokens.length - 1; index += 1) {
    const kind = KINDS[tokens[index]]
    if (kind === undefined) continue
    const next = tokens[index + 1]
    if (!/^\d+$/.test(next)) continue
    // Leading zeros are cosmetic: `lab01` and `lab1` are one lab.
    return `${kind}:${Number(next)}`
  }
  return null
}

/** Whether a filename presents itself as worked answers. */
export function looksLikeSolutions(filename: string): boolean {
  return tokenize(filename).some((token) => SOLUTION_WORDS.has(token))
}

export type ReferenceSuggestion = {
  /** The solutions file being offered. */
  document: DocumentRead
  /** The chosen problem set it appears to answer, so the offer can explain itself. */
  because: DocumentRead
}

/**
 * Solutions files that appear to answer the chosen problem sets.
 *
 * Only documents Lyra can actually read are offered, and anything already picked on
 * either side is left out. The result is a suggestion: the caller presents it, and the
 * student decides.
 */
export function suggestReferences(
  documents: readonly DocumentRead[],
  problemSet: readonly number[],
  reference: readonly number[],
): ReferenceSuggestion[] {
  const chosen = problemSet
    .map((id) => documents.find((document) => document.id === id))
    .filter((document): document is DocumentRead => document !== undefined)
  if (chosen.length === 0) return []

  const taken = new Set([...problemSet, ...reference])
  const suggestions: ReferenceSuggestion[] = []
  const offered = new Set<number>()

  for (const document of documents) {
    if (taken.has(document.id) || offered.has(document.id)) continue
    if (document.state !== 'ready') continue
    if (!looksLikeSolutions(document.filename)) continue

    const key = assignmentKey(document.filename)
    if (key === null) continue

    const because = chosen.find((candidate) => assignmentKey(candidate.filename) === key)
    if (because === undefined) continue

    offered.add(document.id)
    suggestions.push({ document, because })
  }

  return suggestions
}
