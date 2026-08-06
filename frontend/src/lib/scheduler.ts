/**
 * The TypeScript mirror of backend/core/scheduler.py: same constants, same math. The
 * session screen shows each rating's next interval, and a frontend that drifted from the
 * backend would be lying about when a card comes back. The Python contract cases are
 * ported in tests/scheduler.test.ts so the two cannot drift quietly.
 *
 * Pure functions, no I/O, and no ambient clock: every function takes `now` from the
 * caller, so a test pins time and the interface owns the wall clock.
 */

import { parseTimestamp } from '@/lib/format'
import type { CardBucket, CardSchedulingState, CardStateRead, Rating } from '@/types'

export const RATINGS: readonly Rating[] = ['again', 'hard', 'good', 'easy']

export const NEW: CardSchedulingState = 'new'
export const LEARNING: CardSchedulingState = 'learning'
export const RELEARNING: CardSchedulingState = 'relearning'
export const REVIEW: CardSchedulingState = 'review'

export const INITIAL_DIFFICULTY = 5.0
export const MIN_DIFFICULTY = 1.0
export const MAX_DIFFICULTY = 10.0

// How one rating moves the display knob. `again` teaches the least per review, so it
// pushes hardest toward difficult; `easy` is the one rating that lowers it.
export const DIFFICULTY_DELTAS: Record<Rating, number> = {
  again: 1.0,
  hard: 0.5,
  good: -0.1,
  easy: -0.5,
}

// Applied before scaling, so a fresh card never multiplies zero: the first success on a
// new card schedules it a day out rather than never.
export const STABILITY_SEED_FLOOR = 1.0
export const STABILITY_FACTORS: Record<Exclude<Rating, 'again'>, number> = {
  hard: 1.2,
  good: 2.0,
  easy: 2.8,
}

// A lapse keeps a fifth of the strength the card had earned, but never schedules inside
// half a day: the floor is what stops a well-worn card from collapsing to "again in an
// hour" after one slip months in.
export const LAPSE_DECAY = 0.2
export const LAPSE_FLOOR_DAYS = 0.5

/** `again` re-shows the card in the same session rather than tomorrow: ten minutes. */
export const RELEARN_INTERVAL_MS = 10 * 60 * 1000

// A card is mastered when it is in the long-term state and its interval has reached three
// weeks: past that, forgetting is slow enough that the deck stops being daily work.
export const MASTERED_STABILITY_DAYS = 21.0

const MINUTE_MS = 60 * 1000
const DAY_MS = 24 * 60 * 60 * 1000

/** One card's scheduling state, as values; updates return a new one. */
export interface CardState {
  /** When the card next wants review. New cards are due immediately. */
  dueAt: Date
  /** Estimated memory strength in days. */
  stability: number
  /** Display knob in [1, 10]; not a scheduling input. */
  difficulty: number
  /** Completed reviews. */
  reps: number
  /** Times a graduated or learning card was rated `again`. */
  lapses: number
  state: CardSchedulingState
  /** When the last review happened, if any. */
  lastReviewAt: Date | null
}

/** A fresh card: due immediately, nothing known about it yet. */
export function newCardState(now: Date): CardState {
  return {
    dueAt: now,
    stability: 0,
    difficulty: INITIAL_DIFFICULTY,
    reps: 0,
    lapses: 0,
    state: NEW,
    lastReviewAt: null,
  }
}

/** The API's storage strings as the scheduler's value type, with real dates. */
export function cardStateFromRead(read: CardStateRead): CardState {
  return {
    dueAt: parseTimestamp(read.due_at),
    stability: read.stability,
    difficulty: read.difficulty,
    reps: read.reps,
    lapses: read.lapses,
    state: read.state,
    lastReviewAt: read.last_review_at === null ? null : parseTimestamp(read.last_review_at),
  }
}

/**
 * Apply one rating and return the next state. The input is not mutated.
 *
 * Throws on an unknown rating, as the Python does: a rating the scheduler does not know
 * is a programming error, not a scheduling decision.
 */
export function reviewCard(card: CardState, rating: Rating, now: Date): CardState {
  if (!RATINGS.includes(rating)) throw new Error(`Unknown rating: ${rating}`)

  const difficulty = Math.min(
    MAX_DIFFICULTY,
    Math.max(MIN_DIFFICULTY, card.difficulty + DIFFICULTY_DELTAS[rating]),
  )

  if (rating === 'again') {
    // A card that never graduated has nothing to lapse out of, so it stays in learning;
    // only a card that reached review (or was already relearning) enters relearning.
    const state = card.state === REVIEW || card.state === RELEARNING ? RELEARNING : LEARNING
    return {
      dueAt: new Date(now.getTime() + RELEARN_INTERVAL_MS),
      stability: Math.max(card.stability * LAPSE_DECAY, LAPSE_FLOOR_DAYS),
      difficulty,
      reps: card.reps + 1,
      lapses: card.lapses + 1,
      state,
      lastReviewAt: now,
    }
  }

  const stability = Math.max(card.stability, STABILITY_SEED_FLOOR) * STABILITY_FACTORS[rating]
  // A new card rated `easy` is one the learner already knows, so it fast-tracks to
  // review; any other first success still wants a near-term second look.
  const state = rating === 'easy' || card.state !== NEW ? REVIEW : LEARNING
  return {
    dueAt: new Date(now.getTime() + stability * DAY_MS),
    stability,
    difficulty,
    reps: card.reps + 1,
    lapses: card.lapses,
    state,
    lastReviewAt: now,
  }
}

/** The deck-panel grouping: new, still being learned, or mastered. */
export function bucket(card: CardState): CardBucket {
  if (card.reps === 0 || card.state === NEW) return 'new'
  if (card.state === REVIEW && card.stability >= MASTERED_STABILITY_DAYS) return 'mastered'
  return 'learning'
}

// A card never seen is the most valuable thing a session can serve, and a review card can
// absorb a delay better than a learning one.
const STATE_PRIORITY: Record<CardSchedulingState, number> = {
  new: 0,
  learning: 1,
  relearning: 1,
  review: 2,
}

/**
 * Part ids in the order a session should serve them.
 *
 * Due cards first, ordered new before learning before review, ties by soonest due.
 * Not-yet-due cards follow by soonest due, so a session queue never runs dry. Part id is
 * the final tiebreak, so one queue is the same queue on every call.
 */
export function studyOrder(states: Map<number, CardState>, now: Date): number[] {
  const entries = [...states.entries()]
  const due = entries.filter(([, card]) => card.dueAt.getTime() <= now.getTime())
  const upcoming = entries.filter(([, card]) => card.dueAt.getTime() > now.getTime())
  due.sort(
    ([aId, a], [bId, b]) =>
      STATE_PRIORITY[a.state] - STATE_PRIORITY[b.state] ||
      a.dueAt.getTime() - b.dueAt.getTime() ||
      aId - bId,
  )
  upcoming.sort(([aId, a], [bId, b]) => a.dueAt.getTime() - b.dueAt.getTime() || aId - bId)
  return [...due, ...upcoming].map(([partId]) => partId)
}

/**
 * The wait a rating produces, for the button that offers it: "10 min", "1 d", "2.8 d".
 * A sub-day wait (which only an `again` produces) reads in minutes; longer waits read in
 * days, one decimal under ten days and whole days above.
 */
export function nextIntervalLabel(card: CardState, rating: Rating, now: Date): string {
  const dueAt = reviewCard(card, rating, now).dueAt
  const waitMs = Math.max(0, dueAt.getTime() - now.getTime())
  if (waitMs < DAY_MS) return `${Math.max(1, Math.round(waitMs / MINUTE_MS))} min`
  const days = waitMs / DAY_MS
  if (days < 10) return `${Math.round(days * 10) / 10} d`
  return `${Math.round(days)} d`
}
