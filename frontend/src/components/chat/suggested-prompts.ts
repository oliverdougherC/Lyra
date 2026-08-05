import type { FactRead } from '@/types'

const FALLBACKS = [
  'What are the main topics in this class?',
  'Summarize the material I uploaded.',
  'What should I study first?',
]

const SUGGESTION_COUNT = 3

/** How many topics to offer, at most, before the fallbacks fill the rest. */
const TOPIC_SUGGESTIONS = 2

/**
 * Topics are pulled from document headings, which arrive in Title Case. Dropped into a
 * sentence they read as shouting, against the sentence-case rule in ui-phase-1.md. Words
 * that are already all-caps are acronyms (ROC, LTI) and keep their case.
 */
function downcaseHeading(value: string): string {
  return value
    .trim()
    .split(/(\s+)/)
    .map((word) => (/^[A-Z]{2,}$/.test(word.replace(/\W/g, '')) ? word : word.toLowerCase()))
    .join('')
}

/**
 * Active means it may enter a prompt. Mirrors `select_active_facts` in
 * `backend/core/profiles.py`, which holds the rule: not rejected, and either confirmed, or
 * marked high, or stated independently by two documents.
 */
function isActive(fact: FactRead): boolean {
  return (
    !fact.rejected && (fact.confirmed || fact.confidence === 'high' || fact.sources.length >= 2)
  )
}

/**
 * Deterministic so the same profile always yields the same three prompts. Built in order,
 * skipping anything that cannot be filled, then topped up from the fallbacks.
 *
 * Suggestions are deduplicated because the string is the list's identity, both as the React
 * key and to the reader. Two topics can render the same sentence: `downcaseHeading` folds
 * case, so a profile still holding `Continuous-Signal Processing` alongside
 * `Continuous-signal processing` offers one suggestion twice. That pairing is ordinary
 * rather than exotic, since consolidation only reaches a class on its next upload.
 */
export function buildSuggestedPrompts(facts: FactRead[]): string[] {
  const active = facts.filter(isActive)
  const suggestions: string[] = []

  const add = (suggestion: string): void => {
    if (!suggestions.includes(suggestion)) suggestions.push(suggestion)
  }

  if (active.some((fact) => fact.kind === 'deadline')) add('What is due next week?')

  // Counted against what was actually added, so a collision costs a duplicate rather than
  // the whole second topic slot.
  const topics = active.filter((candidate) => candidate.kind === 'topic')
  for (const fact of topics) {
    if (suggestions.length >= SUGGESTION_COUNT) break
    const topic = downcaseHeading(fact.value)
    if (topic) add(`Explain ${topic}`)
    if (suggestions.filter((one) => one.startsWith('Explain ')).length >= TOPIC_SUGGESTIONS) break
  }

  for (const fallback of FALLBACKS) {
    if (suggestions.length >= SUGGESTION_COUNT) break
    add(fallback)
  }

  return suggestions.slice(0, SUGGESTION_COUNT)
}
