'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { Layers, RotateCcw } from 'lucide-react'
import { toast } from 'sonner'

import { CardActions } from '@/components/study/card-actions'
import { MathText } from '@/components/solutions/math-text'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { Kbd } from '@/components/ui/kbd'
import { Skeleton } from '@/components/ui/skeleton'
import { ApiError } from '@/lib/api'
import { formatCount } from '@/lib/format'
import { useDeck, useDeckSession, useReviewCard } from '@/lib/hooks/use-study'
import {
  RATINGS,
  bucket,
  cardStateFromRead,
  nextIntervalLabel,
  type CardState,
} from '@/lib/scheduler'
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
  const session = useDeckSession(deckId)
  const { mutateAsync: submitReview, isPending: reviewing } = useReviewCard(deckId)

  const [queue, setQueue] = useState<SessionCard[] | null>(null)
  const [total, setTotal] = useState(0)
  const [actionsOpen, setActionsOpen] = useState(false)
  const reviewingRef = useRef(false)
  const faceRef = useRef<HTMLElement>(null)
  const summaryRef = useRef<HTMLHeadingElement>(null)
  const [flipped, setFlipped] = useState(false)
  const [retryRating, setRetryRating] = useState<Rating | null>(null)
  const [ratings, setRatings] = useState<Record<Rating, number>>({
    again: 0,
    hard: 0,
    good: 0,
    easy: 0,
  })
  /** The latest scheduling state the interface holds for each card in the session. */
  const [states, setStates] = useState<Map<number, CardState>>(new Map())
  /** When the current card came up; the interval labels are measured from it. */
  const [presentedAt, setPresentedAt] = useState(() => new Date())
  /**
   * One idempotency key per card review, keyed by part id. A retry after a failed or lost
   * response reuses the same key, so the server applies the review once whether or not the
   * first request actually committed (PLA-296). The key represents "the review of this
   * card"; it is generated on first submit and reused until that card leaves the queue.
   */
  const operationIds = useRef<Map<number, { id: string; rating: Rating }>>(new Map())

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
      if (!current || reviewingRef.current || actionsOpen) return
      // A lost response may already have committed. Retrying must confirm the same
      // rating as well as the same key, or the summary would contradict saved history.
      let operation = operationIds.current.get(current.part_id)
      if (operation && operation.rating !== rating) return
      reviewingRef.current = true
      if (!operation) {
        operation = { id: crypto.randomUUID(), rating }
        operationIds.current.set(current.part_id, operation)
      }
      try {
        const updated = await submitReview({
          partId: current.part_id,
          rating: operation.rating,
          operationId: operation.id,
        })
        operationIds.current.delete(current.part_id)
        setRetryRating(null)
        setRatings((previous) => ({ ...previous, [rating]: previous[rating] + 1 }))
        setStates((previous) => new Map(previous).set(current.part_id, cardStateFromRead(updated)))
        setQueue((previous) => (previous ?? []).slice(1))
        setFlipped(false)
        setPresentedAt(new Date())
      } catch (caught) {
        setRetryRating(rating)
        toast.error(caught instanceof ApiError ? caught.message : 'Could not record that review.')
      } finally {
        reviewingRef.current = false
      }
    },
    [queue, actionsOpen, submitReview],
  )

  // Space flips, 1-4 rate, except while typing: a field owns those keys. A focused button
  // owns Space too - activating it is the expected path, and the shortcut firing beside
  // it would rate or flip twice.
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (actionsOpen || reviewingRef.current) return
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
  }, [flipped, flip, rate, actionsOpen])

  useEffect(() => {
    faceRef.current?.focus()
  }, [flipped])

  useEffect(() => {
    if (queue?.length === 0) summaryRef.current?.focus()
    else if (queue && total > queue.length && !actionsOpen) faceRef.current?.focus()
  }, [queue, total, actionsOpen])

  async function restart() {
    operationIds.current.clear()
    setRetryRating(null)
    const fresh = await session.refetch()
    if (fresh.isError) {
      toast.error('Could not load another session. Try again.')
      return
    }
    const next = fresh.data?.cards
    if (!next) return
    setQueue(next)
    setTotal(next.length)
    setStates(new Map(next.map((card) => [card.part_id, cardStateFromRead(card.card_state)])))
    setRatings({ again: 0, hard: 0, good: 0, easy: 0 })
    setFlipped(false)
    setPresentedAt(new Date())
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
          <EmptyTitle>No cards in this session</EmptyTitle>
          <EmptyDescription>Start another session to check for more cards.</EmptyDescription>
        </EmptyHeader>
        <DeckProgress deckId={deckId} />
        <Button variant="outline" disabled={session.isFetching} onClick={() => void restart()}>
          Study again
        </Button>
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
        <h2
          ref={summaryRef}
          tabIndex={-1}
          className="font-heading text-text-primary text-2xl tracking-tight focus:outline-none"
        >
          Session complete
        </h2>
        <p className="text-text-secondary text-sm">
          You reviewed {formatCount(rated, 'card')}:{' '}
          {RATINGS.map((rating) => `${ratings[rating]} ${rating}`).join(' · ')}
        </p>
        <p className="text-text-tertiary text-sm">
          Cards in this session: new {bucketsAfter.new} · learning {bucketsAfter.learning} ·
          mastered {bucketsAfter.mastered}
        </p>
        <DeckProgress deckId={deckId} />
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

      <section
        ref={faceRef}
        tabIndex={0}
        aria-label={flipped ? 'Card answer' : 'Card question'}
        className="border-border bg-card flex min-h-32 max-h-[30dvh] flex-col gap-3 overflow-y-auto rounded-lg border p-4 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none sm:min-h-48 sm:max-h-[40dvh] sm:p-6"
      >
        <span className="text-text-tertiary text-xs tracking-[0.14em] uppercase">
          {flipped ? 'Answer' : 'Question'}
        </span>
        <MathText className="text-text-primary text-lg">
          {flipped ? current.card.back : current.card.front}
        </MathText>
      </section>
      <div className="flex items-center justify-between gap-2">
        <Button variant="outline" onClick={flip} disabled={reviewing}>
          {flipped ? 'Show question' : 'Show answer'}
          <Kbd aria-hidden className="hidden sm:inline-flex">
            Space
          </Kbd>
        </Button>
        <CardActions
          key={current.part_id}
          deckId={deckId}
          current={current}
          disabled={reviewing}
          onOpenChange={setActionsOpen}
          onUpdated={(content) =>
            setQueue((previous) =>
              (previous ?? []).map((card) =>
                card.part_id === current.part_id ? { ...card, card: content } : card,
              ),
            )
          }
          onRemoved={() => {
            operationIds.current.delete(current.part_id)
            setRetryRating(null)
            setStates((previous) => {
              const next = new Map(previous)
              next.delete(current.part_id)
              return next
            })
            setQueue((previous) =>
              (previous ?? []).filter((card) => card.part_id !== current.part_id),
            )
            setTotal((previous) => previous - 1)
            setFlipped(false)
            setPresentedAt(new Date())
          }}
        />
      </div>
      <p className="sr-only" role="status">
        {flipped ? 'Answer shown.' : 'Question shown.'}
      </p>

      {retryRating ? (
        <p role="alert" className="text-danger-text text-sm">
          That {RATING_LABELS[retryRating]} review could not be confirmed. Choose{' '}
          {RATING_LABELS[retryRating]} again to confirm it before using another rating.
        </p>
      ) : null}
      {flipped ? (
        <div
          className="grid grid-cols-2 gap-2 min-[360px]:grid-cols-4"
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
              className="flex h-auto flex-col gap-0.5 px-1 py-2"
            >
              <span className="flex items-center gap-1.5">
                {RATING_LABELS[rating]}
                <Kbd className="hidden sm:inline-flex">{index + 1}</Kbd>
              </span>
              <span className="text-text-tertiary text-xs font-normal">
                {nextIntervalLabel(currentState, rating, presentedAt)}
              </span>
            </Button>
          ))}
        </div>
      ) : (
        <p className="text-text-tertiary text-sm">Recall your answer, then choose Show answer.</p>
      )}
    </div>
  )
}

/** Loaded after the last mutation, so remaining due counts cover the entire deck. */
function DeckProgress({ deckId }: { deckId: number }) {
  const deck = useDeck(deckId)
  if (deck.isPending || deck.isFetching)
    return (
      <p role="status" className="text-text-tertiary text-sm">
        Updating deck progress…
      </p>
    )
  if (deck.isError)
    return (
      <div className="text-sm">
        <p>Could not refresh deck progress.</p>
        <Button variant="outline" size="sm" onClick={() => void deck.refetch()}>
          Retry deck progress
        </Button>
      </div>
    )
  const due = deck.data.cards.filter(
    (card) => !card.card_state || cardStateFromRead(card.card_state).dueAt.getTime() <= Date.now(),
  ).length
  return (
    <p className="text-text-secondary text-sm">
      Deck total: {formatCount(deck.data.cards.length, 'card')} · {due} due now
    </p>
  )
}
