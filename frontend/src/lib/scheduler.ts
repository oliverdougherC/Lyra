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

// Keep legacy inflated states and new schedules within a useful, finite horizon.
export const MAX_STABILITY_DAYS = 365
export const MIN_SPACED_ELAPSED_MS = 24 * 60 * 60 * 1000
const MAX_SCHEDULE_MS = new Date('9999-12-31T23:59:59.999Z').getTime()
// Seed new cards and successful relearning at one day before any earned growth.
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

function bounded(value: number, low: number, high: number, fallback: number): number {
  return Number.isFinite(value) ? Math.min(high, Math.max(low, value)) : fallback
}

function dueAfter(now: Date, intervalMs: number): Date {
  return new Date(Math.min(MAX_SCHEDULE_MS, now.getTime() + intervalMs))
}

/**
 * Apply one rating and return the next state. The input is not mutated.
 *
 * Throws on an unknown rating, as the Python does: a rating the scheduler does not know
 * is a programming error, not a scheduling decision.
 */
export function reviewCard(card: CardState, rating: Rating, now: Date): CardState {
  if (!RATINGS.includes(rating)) throw new Error(`Unknown rating: ${rating}`)

  const strength = bounded(card.stability, 0, MAX_STABILITY_DAYS, STABILITY_SEED_FLOOR)
  const difficulty = bounded(
    bounded(card.difficulty, MIN_DIFFICULTY, MAX_DIFFICULTY, INITIAL_DIFFICULTY) +
      DIFFICULTY_DELTAS[rating],
    MIN_DIFFICULTY,
    MAX_DIFFICULTY,
    INITIAL_DIFFICULTY,
  )

  if (rating === 'again') {
    // A card that never graduated has nothing to lapse out of, so it stays in learning;
    // only a card that reached review (or was already relearning) enters relearning.
    const state = card.state === REVIEW || card.state === RELEARNING ? RELEARNING : LEARNING
    return {
      dueAt: dueAfter(now, RELEARN_INTERVAL_MS),
      stability: Math.max(strength * LAPSE_DECAY, LAPSE_FLOOR_DAYS),
      difficulty,
      reps: card.reps + 1,
      lapses: card.lapses + 1,
      state,
      lastReviewAt: now,
    }
  }

  // Early successes preserve the deadline and strength. Growth requires a due card
  // and 24h since its last rating; due relearning can graduate without multiplying.
  const fresh = card.state === NEW && card.reps === 0
  const due = card.dueAt.getTime() <= now.getTime()
  const spaced =
    due &&
    card.lastReviewAt !== null &&
    now.getTime() - card.lastReviewAt.getTime() >= MIN_SPACED_ELAPSED_MS
  let stability: number
  let state: CardSchedulingState
  let dueAt: Date
  if (fresh || spaced) {
    stability = Math.min(
      MAX_STABILITY_DAYS,
      Math.max(strength, STABILITY_SEED_FLOOR) * STABILITY_FACTORS[rating],
    )
    state = rating === 'easy' || card.state !== NEW ? REVIEW : LEARNING
    dueAt = dueAfter(now, stability * DAY_MS)
  } else if (due) {
    stability = Math.max(strength, STABILITY_SEED_FLOOR)
    state = REVIEW
    dueAt = dueAfter(now, stability * DAY_MS)
  } else {
    stability = strength
    state = card.state
    const deadline = Number.isFinite(card.dueAt.getTime()) ? card.dueAt.getTime() : now.getTime()
    dueAt = new Date(
      Math.max(
        now.getTime(),
        Math.min(deadline, dueAfter(now, MAX_STABILITY_DAYS * DAY_MS).getTime()),
      ),
    )
  }
  return {
    dueAt,
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
 * Sub-day waits (including preserved early-practice deadlines) read in minutes; longer waits read in
 * days, one decimal under ten days and whole days above.
 */
export function nextIntervalLabel(card: CardState, rating: Rating, now: Date): string {
  const dueAt = reviewCard(card, rating, now).dueAt
  const waitMs = Math.max(0, dueAt.getTime() - now.getTime())
  if (waitMs === 0) return 'Now'
  if (waitMs < DAY_MS) return `${Math.max(1, Math.round(waitMs / MINUTE_MS))} min`
  const days = waitMs / DAY_MS
  if (days < 10) return `${Math.round(days * 10) / 10} d`
  return `${Math.round(days)} d`
}
