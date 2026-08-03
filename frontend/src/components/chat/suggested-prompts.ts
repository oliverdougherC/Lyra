import type { FactRead } from '@/types'

const FALLBACKS = [
  'What are the main topics in this class?',
  'Summarize the material I uploaded.',
  'What should I study first?',
]

const SUGGESTION_COUNT = 3

/** Active means it may enter a prompt: confirmed, or high confidence and not rejected. */
function isActive(fact: FactRead): boolean {
  return !fact.rejected && (fact.confirmed || fact.confidence === 'high')
}

/**
 * Deterministic so the same profile always yields the same three prompts. Built in order,
 * skipping anything that cannot be filled, then topped up from the fallbacks.
 */
export function buildSuggestedPrompts(facts: FactRead[]): string[] {
  const active = facts.filter(isActive)
  const suggestions: string[] = []

  if (active.some((fact) => fact.kind === 'deadline')) suggestions.push('What is due next week?')

  for (const fact of active.filter((candidate) => candidate.kind === 'topic').slice(0, 2)) {
    const topic = fact.value.trim()
    if (topic) suggestions.push(`Explain ${topic}`)
  }

  for (const fallback of FALLBACKS) {
    if (suggestions.length >= SUGGESTION_COUNT) break
    if (!suggestions.includes(fallback)) suggestions.push(fallback)
  }

  return suggestions.slice(0, SUGGESTION_COUNT)
}
