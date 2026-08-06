import { describe, expect, it } from 'vitest'

import {
  MASTERED_STABILITY_DAYS,
  bucket,
  newCardState,
  nextIntervalLabel,
  reviewCard,
  studyOrder,
  type CardState,
} from '@/lib/scheduler'
import type { Rating } from '@/types'

/**
 * Contract tests for the spaced-repetition scheduler, ported case for case from
 * backend/tests/test_scheduler.py; the two suites pin the same math so the TypeScript
 * mirror cannot drift from the Python it reflects. Every case pins `now`, because the
 * functions take their clock from the caller; that is what makes a schedule assertable
 * down to the minute.
 */
const NOW = new Date('2026-08-06T12:00:00Z')

const MINUTE_MS = 60 * 1000
const HOUR_MS = 60 * MINUTE_MS
const DAY_MS = 24 * HOUR_MS

/** A state with every field set, for the cases that build one by hand. */
function cardState(overrides: Partial<CardState> & Pick<CardState, 'dueAt'>): CardState {
  return {
    stability: 0,
    difficulty: 5,
    reps: 0,
    lapses: 0,
    state: 'new',
    lastReviewAt: null,
    ...overrides,
  }
}

/** Date arithmetic with float intervals, compared to the millisecond. */
function expectDueAfter(card: CardState, ms: number) {
  expect(Math.abs(card.dueAt.getTime() - (NOW.getTime() + ms))).toBeLessThan(1)
}

it('a new card is due immediately', () => {
  const card = newCardState(NOW)

  expect(card.dueAt.getTime()).toBe(NOW.getTime())
  expect(card.stability).toBe(0)
  expect(card.difficulty).toBe(5)
  expect(card.reps).toBe(0)
  expect(card.lapses).toBe(0)
  expect(card.state).toBe('new')
  expect(card.lastReviewAt).toBeNull()
})

it('easy on a new card fast-tracks to review', () => {
  const card = reviewCard(newCardState(NOW), 'easy', NOW)

  expect(card.state).toBe('review')
  // The seed floor times the easy factor: 1.0 * 2.8 days out, never a zero interval.
  expect(card.stability).toBeCloseTo(2.8)
  expectDueAfter(card, 2.8 * DAY_MS)
})

it('good twice grows the interval monotonically', () => {
  const first = reviewCard(newCardState(NOW), 'good', NOW)
  const second = reviewCard(first, 'good', first.dueAt)

  expect(first.state).toBe('learning')
  expect(second.state).toBe('review')
  expect(first.stability).toBeCloseTo(2.0)
  expect(second.stability).toBeCloseTo(4.0)
  expect(second.dueAt.getTime()).toBeGreaterThan(first.dueAt.getTime())
})

it('again on a review card relearns with a decayed positive stability', () => {
  const graduated = reviewCard(reviewCard(newCardState(NOW), 'good', NOW), 'good', NOW)
  expect(graduated.state).toBe('review')

  const lapsed = reviewCard(graduated, 'again', NOW)

  expect(lapsed.state).toBe('relearning')
  expectDueAfter(lapsed, 10 * MINUTE_MS)
  expect(lapsed.stability).toBeCloseTo(Math.max(graduated.stability * 0.2, 0.5))
  expect(lapsed.stability).toBeGreaterThan(0)
  expect(lapsed.lapses).toBe(1)
})

it('again on a card that never graduated stays learning', () => {
  // A card that never reached review has nothing to lapse out of.
  const learning = reviewCard(newCardState(NOW), 'good', NOW)
  const lapsed = reviewCard(learning, 'again', NOW)

  expect(lapsed.state).toBe('learning')
  expect(lapsed.lapses).toBe(1)
})

it('a lapse then a success returns to review', () => {
  const graduated = reviewCard(reviewCard(newCardState(NOW), 'good', NOW), 'good', NOW)
  const lapsed = reviewCard(graduated, 'again', NOW)
  const recovered = reviewCard(lapsed, 'good', NOW)

  expect(recovered.state).toBe('review')
  // The seed floor applies before scaling: the lapsed 0.8 is seeded to 1.0, then doubled.
  expect(recovered.stability).toBeCloseTo(2.0)
})

it.each([
  { ratings: Array<Rating>(6).fill('again'), expected: 10 },
  { ratings: Array<Rating>(8).fill('easy'), expected: 1 },
])('difficulty clamps at $expected after repeated ratings', ({ ratings, expected }) => {
  let card = newCardState(NOW)
  for (const rating of ratings) card = reviewCard(card, rating, NOW)

  expect(card.difficulty).toBe(expected)
})

it('bucket boundaries hold at reps 0 and at the mastered stability', () => {
  const fresh = newCardState(NOW)
  expect(bucket(fresh)).toBe('new')

  const seenOnce = reviewCard(fresh, 'good', NOW)
  expect(seenOnce.reps).toBe(1)
  expect(bucket(seenOnce)).toBe('learning')

  // Exactly at the boundary a review card is mastered; a day under it is not.
  const at = cardState({
    dueAt: NOW,
    stability: MASTERED_STABILITY_DAYS,
    reps: 3,
    state: 'review',
    lastReviewAt: NOW,
  })
  const under = cardState({ ...at, stability: MASTERED_STABILITY_DAYS - 1 })
  expect(bucket(at)).toBe('mastered')
  expect(bucket(under)).toBe('learning')
})

it('study order puts due first, new before learning before review', () => {
  const overdueReview = cardState({
    dueAt: new Date(NOW.getTime() - 2 * DAY_MS),
    stability: 30,
    reps: 5,
    state: 'review',
    lastReviewAt: NOW,
  })
  const overdueNew = newCardState(new Date(NOW.getTime() - DAY_MS))
  const overdueLearning = cardState({
    dueAt: new Date(NOW.getTime() - 3 * HOUR_MS),
    stability: 2,
    reps: 1,
    state: 'learning',
    lastReviewAt: NOW,
  })
  const notDue = cardState({
    dueAt: new Date(NOW.getTime() + DAY_MS),
    stability: 4,
    reps: 2,
    state: 'review',
    lastReviewAt: NOW,
  })
  const states = new Map([
    [1, overdueReview],
    [2, overdueNew],
    [3, overdueLearning],
    [4, notDue],
  ])

  expect(studyOrder(states, NOW)).toEqual([2, 3, 1, 4])
})

it('study order is deterministic for a fixed now', () => {
  const card = newCardState(new Date(NOW.getTime() - HOUR_MS))
  const states = new Map([
    [5, card],
    [3, card],
    [1, card],
  ])

  expect(studyOrder(states, NOW)).toEqual([1, 3, 5])
  expect(studyOrder(states, NOW)).toEqual(studyOrder(states, NOW))
})

it('an unknown rating is rejected', () => {
  expect(() => reviewCard(newCardState(NOW), 'perfect' as Rating, NOW)).toThrow('Unknown rating')
})

describe('nextIntervalLabel', () => {
  it('reads the ten-minute relearn in minutes', () => {
    expect(nextIntervalLabel(newCardState(NOW), 'again', NOW)).toBe('10 min')
  })

  it('reads short intervals in days with at most one decimal', () => {
    const fresh = newCardState(NOW)
    expect(nextIntervalLabel(fresh, 'good', NOW)).toBe('2 d')
    expect(nextIntervalLabel(fresh, 'hard', NOW)).toBe('1.2 d')
    expect(nextIntervalLabel(fresh, 'easy', NOW)).toBe('2.8 d')
  })

  it('rounds to whole days at ten days and above', () => {
    const strong = cardState({
      dueAt: NOW,
      stability: 10,
      reps: 4,
      state: 'review',
      lastReviewAt: NOW,
    })
    // 10 * 1.2 = 12 days: whole days, no decimal.
    expect(nextIntervalLabel(strong, 'hard', NOW)).toBe('12 d')
    // 10 * 2.8 = 28 days.
    expect(nextIntervalLabel(strong, 'easy', NOW)).toBe('28 d')
  })
})
