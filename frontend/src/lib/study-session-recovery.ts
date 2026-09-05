import { z } from 'zod'

import type { CardState } from '@/lib/scheduler'
import type { Rating, SessionCard } from '@/types'

export interface ReviewOperation {
  id: string
  rating: Rating
}

export interface SessionRecovery {
  queue: SessionCard[]
  total: number
  ratings: Record<Rating, number>
  states: [number, CardState][]
  unresolved?: { partId: number; operation: ReviewOperation }[]
  operation: ReviewOperation | null
}

const rating = z.enum(['again', 'hard', 'good', 'easy'])
const schedule = z.enum(['new', 'learning', 'relearning', 'review'])
const recoverySchema = z
  .object({
    queue: z.array(
      z.object({
        part_id: z.number().int(),
        label: z.string().nullable(),
        card: z.object({ front: z.string(), back: z.string(), topic: z.string() }),
        due: z.boolean(),
        card_state: z.object({
          due_at: z.string(),
          stability: z.number(),
          difficulty: z.number(),
          reps: z.number().int().nonnegative(),
          lapses: z.number().int().nonnegative(),
          state: schedule,
          last_review_at: z.string().nullable(),
          bucket: z.enum(['new', 'learning', 'mastered']),
        }),
      }),
    ),
    total: z.number().int().nonnegative(),
    ratings: z.object({
      again: z.number().int().nonnegative(),
      hard: z.number().int().nonnegative(),
      good: z.number().int().nonnegative(),
      easy: z.number().int().nonnegative(),
    }),
    states: z.array(
      z.tuple([
        z.number().int(),
        z.object({
          dueAt: z.date(),
          stability: z.number(),
          difficulty: z.number(),
          reps: z.number().int().nonnegative(),
          lapses: z.number().int().nonnegative(),
          state: schedule,
          lastReviewAt: z.date().nullable(),
        }),
      ]),
    ),
    unresolved: z
      .array(
        z.object({
          partId: z.number().int(),
          operation: z.object({ id: z.string().min(1), rating }),
        }),
      )
      .optional(),
    operation: z.object({ id: z.string().min(1), rating }).nullable(),
  })
  .superRefine((value, context) => {
    const invalid = (message: string) => context.addIssue({ code: 'custom', message })
    if (value.operation && !value.queue.length) invalid('Pending operation requires a queue head')
    if (new Set(value.queue.map((card) => card.part_id)).size !== value.queue.length)
      invalid('Duplicate queue member')
    if (new Set(value.states.map(([id]) => id)).size !== value.states.length)
      invalid('Duplicate scheduler member')
    const rated = Object.values(value.ratings).reduce((sum, count) => sum + count, 0)
    if (value.total !== value.queue.length + rated) invalid('Inconsistent session counts')
    if (value.unresolved?.some((item) => value.queue.some((card) => card.part_id === item.partId)))
      invalid('Unresolved card cannot receive a new operation')
  })

const key = (deckId: number) => `lyra:study-session:v1:${deckId}`

/** Parse and retain the exact revision: claiming ownership must validate this same read. */
export function readSessionRecoveryRecord(deckId: number): {
  revision: string | null
  recovery: SessionRecovery | null
} {
  const revision = sessionStorage.getItem(key(deckId))
  if (revision === null) return { revision, recovery: null }
  // Invalid/unavailable storage must fail closed: silently dropping a pending key could
  // turn a committed-but-unacknowledged review into a second review after reload.
  const recovery = recoverySchema.parse(
    JSON.parse(revision, (name, item) =>
      (name === 'dueAt' || name === 'lastReviewAt') && item !== null ? new Date(item) : item,
    ),
  )
  return { revision, recovery }
}

/** A tab's session survives reloads, including an unacknowledged operation. */
export function readSessionRecovery(deckId: number): SessionRecovery | null {
  return readSessionRecoveryRecord(deckId).recovery
}

// A mounted run owns this deck in this document. Compare the serialized revision too:
// writes from another owner or storage editor must never be overwritten by a continuation.
const owners = new Map<number, symbol>()
export function ownSessionRecovery(deckId: number, expectedRevision: string | null) {
  const owner = Symbol('study run')
  let revision = sessionStorage.getItem(key(deckId))
  if (revision !== expectedRevision)
    throw new Error('Saved recovery changed. Restore it before continuing.')
  owners.set(deckId, owner)
  return {
    current: () => owners.get(deckId) === owner,
    release: () => {
      if (owners.get(deckId) === owner) owners.delete(deckId)
    },
    save: (value: SessionRecovery) => {
      if (owners.get(deckId) !== owner) throw new Error('This study session has another owner.')
      if (sessionStorage.getItem(key(deckId)) !== revision)
        throw new Error('Saved recovery changed. Restore it before continuing.')
      const next = JSON.stringify(value)
      sessionStorage.setItem(key(deckId), next)
      revision = next
    },
  }
}
