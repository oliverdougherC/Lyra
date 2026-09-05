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
  operation: ReviewOperation | null
}

const rating = z.enum(['again', 'hard', 'good', 'easy'])
const schedule = z.enum(['new', 'learning', 'relearning', 'review'])
const recoverySchema = z.object({
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
        reps: z.number(),
        lapses: z.number(),
        state: schedule,
        last_review_at: z.string().nullable(),
        bucket: z.enum(['new', 'learning', 'mastered']),
      }),
    }),
  ),
  total: z.number().int().nonnegative(),
  ratings: z.object({ again: z.number(), hard: z.number(), good: z.number(), easy: z.number() }),
  states: z.array(
    z.tuple([
      z.number().int(),
      z.object({
        dueAt: z.date(),
        stability: z.number(),
        difficulty: z.number(),
        reps: z.number(),
        lapses: z.number(),
        state: schedule,
        lastReviewAt: z.date().nullable(),
      }),
    ]),
  ),
  operation: z.object({ id: z.string().min(1), rating }).nullable(),
})

const key = (deckId: number) => `lyra:study-session:v1:${deckId}`

/** A tab's session survives reloads, including an unacknowledged operation. */
export function readSessionRecovery(deckId: number): SessionRecovery | null {
  const value = sessionStorage.getItem(key(deckId))
  if (!value) return null
  // Invalid/unavailable storage must fail closed: silently dropping a pending key could
  // turn a committed-but-unacknowledged review into a second review after reload.
  return recoverySchema.parse(
    JSON.parse(value, (name, item) =>
      (name === 'dueAt' || name === 'lastReviewAt') && item !== null ? new Date(item) : item,
    ),
  )
}

export function saveSessionRecovery(deckId: number, value: SessionRecovery): void {
  sessionStorage.setItem(key(deckId), JSON.stringify(value))
}
