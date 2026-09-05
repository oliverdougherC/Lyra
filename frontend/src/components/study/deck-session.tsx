'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { Layers, RotateCcw } from 'lucide-react'
import { toast } from 'sonner'

import { MathText } from '@/components/solutions/math-text'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { Kbd } from '@/components/ui/kbd'
import { Skeleton } from '@/components/ui/skeleton'
import { ApiError } from '@/lib/api'
import { formatCount } from '@/lib/format'
import { useDeckSession, useReviewCard } from '@/lib/hooks/use-study'
import {
  RATINGS,
  bucket,
  cardStateFromRead,
  nextIntervalLabel,
  type CardState,
} from '@/lib/scheduler'
import {
  readSessionRecovery,
  saveSessionRecovery,
  type ReviewOperation,
} from '@/lib/study-session-recovery'
import { cn } from '@/lib/utils'
import type { Rating, SessionCard } from '@/types'

const RATING_LABELS: Record<Rating, string> = {
  again: 'Again',
  hard: 'Hard',
  good: 'Good',
  easy: 'Easy',
}

/** The key under each rating button: 1 is the worst recall, 4 the best. */
const RATING_KEYS: Record<string, Rating> = { '1': 'again', '2': 'hard', '3': 'good', '4': 'easy' }

/**
 * One run through a deck's session queue.
 *
 * The queue is owned here for the session's duration rather than re-derived from the
 * server: a rated card stays out of the queue even when its new state would schedule it
 * inside this session, because re-serving a card the student just rated teaches nothing.
 * What the server says about each card is still kept, and the end screen's buckets are
 * recomputed from those answers, so the summary is the scheduler's own math rather than a
 * second count kept in parallel.
 */
export function DeckSession({ deckId }: { deckId: number }) {
  return <DeckSessionRun key={deckId} deckId={deckId} />
}

function DeckSessionRun({ deckId }: { deckId: number }) {
  const session = useDeckSession(deckId)
  const { mutateAsync: submitReview, isPending: reviewing } = useReviewCard(deckId)

  const [{ recovery, recoveryError }] = useState(() => {
    try {
      return { recovery: readSessionRecovery(deckId), recoveryError: false }
    } catch {
      return { recovery: null, recoveryError: true }
    }
  })
  const [queue, setQueue] = useState<SessionCard[] | null>(recovery?.queue ?? null)
  const [total, setTotal] = useState(recovery?.total ?? 0)
  const [flipped, setFlipped] = useState(Boolean(recovery?.operation))
  const [retryRating, setRetryRating] = useState<Rating | null>(recovery?.operation?.rating ?? null)
  const reviewingRef = useRef(false)
  const [ratings, setRatings] = useState<Record<Rating, number>>(
    recovery?.ratings ?? {
      again: 0,
      hard: 0,
      good: 0,
      easy: 0,
    },
  )
  /** The latest scheduling state the interface holds for each card in the session. */
  const [states, setStates] = useState<Map<number, CardState>>(new Map(recovery?.states))
  /** When the current card came up; the interval labels are measured from it. */
  const [presentedAt, setPresentedAt] = useState(() => new Date())
  // Persist the payload and key before sending; transport failure cannot change either.
  const operation = useRef<ReviewOperation | null>(recovery?.operation ?? null)

  // Seeded during render rather than in an effect, so the first card never flashes the
  // end screen for a frame. A finished session leaves the queue empty but not null, so
  // this never re-fires over a completed run.
  const cards = session.data?.cards
  if (cards && queue === null) {
    setQueue(cards)
    setTotal(cards.length)
    setStates(new Map(cards.map((card) => [card.part_id, cardStateFromRead(card.card_state)])))
  }

  const flip = useCallback(() => setFlipped((current) => !current), [])

  const rate = useCallback(
    async (rating: Rating) => {
      const current = queue?.[0]
      if (!queue || !current || reviewingRef.current || recoveryError) return
      if (operation.current && operation.current.rating !== rating) return
      reviewingRef.current = true
      const pending = operation.current ?? { id: crypto.randomUUID(), rating }
      operation.current = pending
      try {
        saveSessionRecovery(deckId, {
          queue,
          total,
          ratings,
          states: [...states],
          operation: pending,
        })
        const updated = await submitReview({
          partId: current.part_id,
          rating: pending.rating,
          operationId: pending.id,
        })
        const nextRatings = { ...ratings, [pending.rating]: ratings[pending.rating] + 1 }
        const nextStates = new Map(states).set(current.part_id, cardStateFromRead(updated))
        const nextQueue = queue.slice(1)
        // Acknowledge durably before advancing. If storage fails, replay the same key.
        saveSessionRecovery(deckId, {
          queue: nextQueue,
          total,
          ratings: nextRatings,
          states: [...nextStates],
          operation: null,
        })
        operation.current = null
        setRetryRating(null)
        setRatings(nextRatings)
        setStates(nextStates)
        setQueue(nextQueue)
        setFlipped(false)
        setPresentedAt(new Date())
      } catch (caught) {
        setRetryRating(pending.rating)
        toast.error(
          caught instanceof ApiError
            ? caught.message
            : 'Could not confirm that review. Retry the same rating.',
        )
      } finally {
        reviewingRef.current = false
      }
    },
    [queue, total, ratings, states, deckId, submitReview, recoveryError],
  )

  // Space flips, 1-4 rate, except while typing: a field owns those keys. A focused button
  // owns Space too - activating it is the expected path, and the shortcut firing beside
  // it would rate or flip twice.
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      const target = event.target
      if (target instanceof HTMLElement) {
        if (target.isContentEditable) return
        if (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) return
      }
      if (event.key === ' ') {
        if (target instanceof HTMLButtonElement) return
        event.preventDefault()
        flip()
        return
      }
      const rating = RATING_KEYS[event.key]
      if (!rating || !flipped) return
      event.preventDefault()
      void rate(rating)
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [flipped, flip, rate])

  async function restart() {
    const fresh = await session.refetch()
    if (fresh.isError) {
      toast.error('Could not load another session. Try again.')
      return
    }
    const next = fresh.data?.cards
    if (!next) return
    const nextStates = new Map(
      next.map((card) => [card.part_id, cardStateFromRead(card.card_state)]),
    )
    try {
      saveSessionRecovery(deckId, {
        queue: next,
        total: next.length,
        ratings: { again: 0, hard: 0, good: 0, easy: 0 },
        states: [...nextStates],
        operation: null,
      })
    } catch {
      toast.error('Could not save the new session. Try again.')
      return
    }
    operation.current = null
    setRetryRating(null)
    setQueue(next)
    setTotal(next.length)
    setStates(nextStates)
    setRatings({ again: 0, hard: 0, good: 0, easy: 0 })
    setFlipped(false)
    setPresentedAt(new Date())
  }

  if (recoveryError) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not restore this study session</AlertTitle>
        <AlertDescription>
          Saved review recovery is unavailable. Reload after restoring browser storage access before
          rating more cards.
        </AlertDescription>
      </Alert>
    )
  }

  if (session.isPending) {
    return (
      <div className="flex flex-col gap-4" aria-busy="true" aria-label="Loading study session">
        <Skeleton className="h-4 w-24" />
        <Skeleton className="h-64 w-full rounded-lg" />
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {[0, 1, 2, 3].map((button) => (
            <Skeleton key={button} className="h-14 w-full rounded-md" />
          ))}
        </div>
      </div>
    )
  }

  if (session.isError) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load this session</AlertTitle>
        <AlertDescription>
          <p>
            {session.error instanceof ApiError ? session.error.message : 'Something went wrong.'}
          </p>
          <Button
            variant="outline"
            size="sm"
            className="mt-3"
            onClick={() => void session.refetch()}
          >
            Retry
          </Button>
        </AlertDescription>
      </Alert>
    )
  }

  if (total === 0) {
    return (
      <Empty className="py-12">
        <EmptyHeader>
          <EmptyMedia variant="icon">
            <Layers className="text-text-tertiary size-8" />
          </EmptyMedia>
          <EmptyTitle>Nothing to study</EmptyTitle>
          <EmptyDescription>This deck has no cards yet.</EmptyDescription>
        </EmptyHeader>
      </Empty>
    )
  }

  if (queue !== null && queue.length === 0) {
    const rated = ratings.again + ratings.hard + ratings.good + ratings.easy
    const bucketsAfter = { new: 0, learning: 0, mastered: 0 }
    for (const state of states.values()) bucketsAfter[bucket(state)] += 1
    return (
      <section
        aria-label="Session summary"
        className="flex flex-col items-center gap-4 py-10 text-center"
      >
        <h2 className="font-heading text-text-primary text-2xl tracking-tight">Session complete</h2>
        <p className="text-text-secondary text-sm">
          You reviewed {formatCount(rated, 'card')}:{' '}
          {RATINGS.map((rating) => `${ratings[rating]} ${rating}`).join(' · ')}
        </p>
        <p className="text-text-tertiary text-sm">
          Your deck now: new {bucketsAfter.new} · learning {bucketsAfter.learning} · mastered{' '}
          {bucketsAfter.mastered}
        </p>
        <Button variant="outline" onClick={() => void restart()} disabled={session.isFetching}>
          <RotateCcw className="size-4" />
          Study again
        </Button>
      </section>
    )
  }

  const current = queue?.[0]
  if (!current) return null
  const currentState = states.get(current.part_id) ?? cardStateFromRead(current.card_state)
  const position = total - (queue?.length ?? 0) + 1

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-3">
        <p className="text-text-tertiary text-sm tabular-nums" aria-live="polite">
          Card {position} of {total}
        </p>
        <p className="text-text-tertiary text-sm">{current.card.topic}</p>
      </div>

      {/* The flip is a CSS transform on a preserve-3d pair; under prefers-reduced-motion
          the transition is dropped and the swap is instant. Both faces stay in the tree
          with the hidden one aria-hidden, so the animation has something to animate. */}
      <div className="[perspective:1200px]">
        <div
          role="button"
          tabIndex={0}
          aria-label={
            flipped
              ? 'Card back. Press Space to see the front.'
              : 'Card front. Press Space to flip.'
          }
          onClick={flip}
          onKeyDown={(event) => {
            // Enter only: Space reaches the window handler above, which flips for any
            // target that is not a control.
            if (event.key === 'Enter') {
              event.preventDefault()
              flip()
            }
          }}
          className={cn(
            'relative min-h-64 w-full cursor-pointer rounded-lg transition-transform duration-300',
            '[transform-style:preserve-3d] motion-reduce:transition-none',
            'focus-visible:ring-ring focus-visible:ring-2 focus-visible:outline-none',
          )}
          style={{ transform: flipped ? 'rotateY(180deg)' : 'rotateY(0deg)' }}
        >
          <div
            aria-hidden={flipped}
            className="border-border bg-card absolute inset-0 flex flex-col gap-3 overflow-y-auto rounded-lg border p-6 [backface-visibility:hidden]"
          >
            <span className="text-text-tertiary text-xs tracking-[0.14em] uppercase">Front</span>
            <MathText className="text-text-primary text-lg">{current.card.front}</MathText>
          </div>
          <div
            aria-hidden={!flipped}
            className="border-border bg-card absolute inset-0 flex flex-col gap-3 overflow-y-auto rounded-lg border p-6 [backface-visibility:hidden] [transform:rotateY(180deg)]"
          >
            <span className="text-text-tertiary text-xs tracking-[0.14em] uppercase">Back</span>
            <MathText className="text-text-primary text-lg">{current.card.back}</MathText>
          </div>
        </div>
      </div>

      {retryRating ? (
        <p role="alert" className="text-danger-text text-sm">
          That {RATING_LABELS[retryRating]} review could not be confirmed. Choose{' '}
          {RATING_LABELS[retryRating]} again to confirm it before using another rating.
        </p>
      ) : null}
      {flipped ? (
        <div
          className="grid grid-cols-2 gap-2 sm:grid-cols-4"
          role="group"
          aria-label="Rate this card"
        >
          {RATINGS.map((rating, index) => (
            <Button
              key={rating}
              variant="outline"
              disabled={reviewing || (retryRating !== null && rating !== retryRating)}
              onClick={(event) => {
                // Focus goes back to the page, so the next Space flips the next card
                // instead of pressing this button again.
                event.currentTarget.blur()
                void rate(rating)
              }}
              className="flex h-auto flex-col gap-0.5 py-2"
            >
              <span className="flex items-center gap-1.5">
                {RATING_LABELS[rating]}
                <Kbd>{index + 1}</Kbd>
              </span>
              <span className="text-text-tertiary text-xs font-normal">
                {retryRating
                  ? rating === retryRating
                    ? 'Confirm review'
                    : 'Awaiting confirmation'
                  : nextIntervalLabel(currentState, rating, presentedAt)}
              </span>
            </Button>
          ))}
        </div>
      ) : (
        <p className="text-text-tertiary flex items-center gap-1.5 text-sm">
          Press <Kbd>Space</Kbd> or click the card to flip it.
        </p>
      )}
    </div>
  )
}
