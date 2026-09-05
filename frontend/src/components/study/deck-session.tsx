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
import { api, ApiError } from '@/lib/api'
import { formatCount } from '@/lib/format'
import { useDeck, useDeckSession, useReviewCard } from '@/lib/hooks/use-study'
import {
  RATINGS,
  bucket,
  cardStateFromRead,
  nextIntervalLabel,
  newCardState,
  type CardState,
} from '@/lib/scheduler'
import {
  readSessionRecoveryRecord,
  ownSessionRecovery,
  type SessionRecovery,
  type ReviewOperation,
} from '@/lib/study-session-recovery'
import type { DeckDetail, Rating, SessionCard } from '@/types'

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
  return <DeckSessionRecoveryControls key={deckId} deckId={deckId} />
}

function DeckSessionRecoveryControls({ deckId }: { deckId: number }) {
  const [attempt, setAttempt] = useState(0)
  const [readOnly, setReadOnly] = useState(false)
  function retryStorage() {
    setReadOnly(false)
    setAttempt((value) => value + 1)
  }
  return (
    <>
      {readOnly && (
        <Button variant="outline" onClick={retryStorage}>
          Retry recording reviews
        </Button>
      )}
      <DeckSessionRun
        key={attempt}
        deckId={deckId}
        readOnly={readOnly}
        studyReadOnly={() => {
          setReadOnly(true)
          setAttempt((value) => value + 1)
        }}
        retryStorage={retryStorage}
      />
    </>
  )
}

function DeckSessionRun({
  deckId,
  retryStorage,
  readOnly,
  studyReadOnly,
}: {
  deckId: number
  retryStorage: () => void
  readOnly: boolean
  studyReadOnly: () => void
}) {
  const [authoritativeDeck, setAuthoritativeDeck] = useState<DeckDetail | null>(null)
  const [authorityError, setAuthorityError] = useState(false)
  const session = useDeckSession(deckId)
  const { mutateAsync: submitReview, isPending: reviewing } = useReviewCard(deckId)

  const [{ recovery, recoveryError, revision }] = useState(() => {
    try {
      return {
        ...(readOnly ? { recovery: null, revision: null } : readSessionRecoveryRecord(deckId)),
        recoveryError: false,
      }
    } catch {
      return { recovery: null, recoveryError: true, revision: null }
    }
  })
  const [queue, setQueue] = useState<SessionCard[] | null>(recovery?.queue ?? null)
  const [total, setTotal] = useState(recovery?.total ?? 0)
  const [flipped, setFlipped] = useState(Boolean(recovery?.operation))
  const [retryRating, setRetryRating] = useState<Rating | null>(recovery?.operation?.rating ?? null)
  const reviewingRef = useRef(false)
  const [actionsOpen, setActionsOpen] = useState(false)
  const faceRef = useRef<HTMLElement>(null)
  const summaryRef = useRef<HTMLHeadingElement>(null)
  const owner = useRef<ReturnType<typeof ownSessionRecovery> | null>(null)
  const [storageError, setStorageError] = useState(false)
  const [missing, setMissing] = useState(false)
  const [notice, setNotice] = useState('')
  const unresolved = useRef(recovery?.unresolved ?? [])
  const [reconciled, setReconciled] = useState(!recovery)
  useEffect(() => {
    if (!recovery) return
    let active = true
    void api
      .getDeck(deckId)
      .then((fresh) => {
        if (active) setAuthoritativeDeck(fresh)
      })
      .catch(() => {
        if (active) setAuthorityError(true)
      })
    return () => {
      active = false
    }
  }, [deckId, recovery])
  useEffect(() => {
    if (readOnly || recoveryError) return
    try {
      owner.current = ownSessionRecovery(deckId, revision)
    } catch {
      setStorageError(true)
    }
    return () => owner.current?.release()
  }, [deckId, readOnly, recoveryError, revision])
  const persist = useCallback((value: SessionRecovery) => {
    if (!owner.current) throw new Error('Browser storage is unavailable.')
    try {
      owner.current.save({ ...value, unresolved: unresolved.current })
      setStorageError(false)
    } catch (error) {
      setStorageError(true)
      throw error
    }
  }, [])
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

  // Only the complete deck endpoint establishes membership; the due session is capped.
  useEffect(() => {
    if (reconciled || !queue || !authoritativeDeck || recoveryError) return
    const authoritative = new Map(authoritativeDeck.cards.map((card) => [card.part_id, card]))
    const nextStates = new Map(states)
    for (const id of nextStates.keys()) {
      const fresh = authoritative.get(id)
      if (!fresh) nextStates.delete(id)
      else
        nextStates.set(
          id,
          fresh.card_state ? cardStateFromRead(fresh.card_state) : newCardState(new Date()),
        )
    }
    const next = queue.flatMap((card, index) => {
      const fresh = authoritative.get(card.part_id)
      if (!fresh) {
        if (index === 0 && operation.current) {
          setMissing(true)
          return [card]
        }
        nextStates.delete(card.part_id)
        return []
      }
      const cardState = fresh.card_state ?? {
        due_at: new Date().toISOString(),
        stability: 0,
        difficulty: 5,
        reps: 0,
        lapses: 0,
        state: 'new' as const,
        last_review_at: null,
        bucket: 'new' as const,
      }
      const state = cardStateFromRead(cardState)
      nextStates.set(card.part_id, state)
      return [
        {
          ...card,
          label: fresh.label,
          card: fresh.card,
          card_state: cardState,
          due: state.dueAt.getTime() <= Date.now(),
        },
      ]
    })
    const nextTotal = total - (queue.length - next.length)
    try {
      persist({
        queue: next,
        total: nextTotal,
        ratings,
        states: [...nextStates],
        operation: operation.current,
      })
      setQueue(next)
      setTotal(nextTotal)
      setStates(nextStates)
      setReconciled(true)
      if (next.length !== queue.length)
        setNotice('Removed cards were left out of this session. They were not counted as reviews.')
    } catch {
      setStorageError(true)
    }
  }, [authoritativeDeck, queue, reconciled, recoveryError, states, total, ratings, persist])

  const flip = useCallback(() => setFlipped((current) => !current), [])

  const rate = useCallback(
    async (rating: Rating) => {
      const current = queue?.[0]
      if (
        readOnly ||
        !queue ||
        !current ||
        reviewingRef.current ||
        recoveryError ||
        !reconciled ||
        actionsOpen ||
        missing
      )
        return
      if (operation.current && operation.current.rating !== rating) return
      reviewingRef.current = true
      const pending = operation.current ?? { id: crypto.randomUUID(), rating }
      operation.current = pending
      try {
        persist({
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
        if (!owner.current?.current()) return
        const nextRatings = { ...ratings, [pending.rating]: ratings[pending.rating] + 1 }
        const nextStates = new Map(states).set(current.part_id, cardStateFromRead(updated))
        const nextQueue = queue.slice(1)
        // Acknowledge durably before advancing. If storage fails, replay the same key.
        persist({
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
        if (!owner.current?.current()) return
        if (caught instanceof ApiError && caught.status === 404) setMissing(true)
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
    [
      queue,
      total,
      ratings,
      states,
      submitReview,
      recoveryError,
      reconciled,
      actionsOpen,
      missing,
      persist,
      readOnly,
    ],
  )

  // Space flips, 1-4 rate, except while typing: a field owns those keys. A focused button
  // owns Space too - activating it is the expected path, and the shortcut firing beside
  // it would rate or flip twice.
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (actionsOpen || reviewingRef.current || recoveryError || !reconciled || missing) return
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
  }, [flipped, flip, rate, actionsOpen, recoveryError, reconciled, missing])

  useEffect(() => {
    faceRef.current?.focus()
  }, [flipped])
  useEffect(() => {
    if (queue?.length === 0) summaryRef.current?.focus()
    else if (queue && total > queue.length && !actionsOpen) faceRef.current?.focus()
  }, [queue, total, actionsOpen])

  function removeCurrent(retainUncertain = false) {
    if (!queue?.length) return
    const current = queue[0]
    const next = queue.slice(1)
    const nextStates = new Map(states)
    nextStates.delete(current.part_id)
    const previousUnresolved = unresolved.current
    if (retainUncertain && operation.current)
      unresolved.current = [
        ...unresolved.current,
        { partId: current.part_id, operation: operation.current },
      ]
    try {
      persist({ queue: next, total: total - 1, ratings, states: [...nextStates], operation: null })
      operation.current = null
      setRetryRating(null)
      setMissing(false)
      setQueue(next)
      setTotal(total - 1)
      setStates(nextStates)
      setFlipped(false)
      setPresentedAt(new Date())
      if (retainUncertain)
        setNotice(
          'The removed card’s review outcome remains unknown. Its original operation is saved separately and is not counted as a confirmed review.',
        )
    } catch {
      unresolved.current = previousUnresolved
      setStorageError(true)
    }
  }

  async function restart() {
    if (readOnly) {
      setQueue(null)
      setTotal(0)
      setFlipped(false)
      return
    }
    const fresh = await session.refetch()
    if (fresh.isError) {
      toast.error('Could not load another session. Try again.')
      return
    }
    if (!owner.current?.current()) return
    const next = fresh.data?.cards.filter(
      (card) => !unresolved.current.some((item) => item.partId === card.part_id),
    )
    if (!next) return
    const nextStates = new Map(
      next.map((card) => [card.part_id, cardStateFromRead(card.card_state)]),
    )
    try {
      persist({
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
          Saved review recovery is unavailable or malformed. The original record has been preserved.
          Restore browser storage access or repair the saved record, then retry here.
          <Button onClick={retryStorage}>Retry storage access</Button>
          <Button onClick={studyReadOnly}>Study without recording reviews</Button>
        </AlertDescription>
      </Alert>
    )
  }

  if (storageError || (!reconciled && authorityError))
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not restore this study session</AlertTitle>
        <AlertDescription>
          Recovery could not be saved or authoritative cards could not be loaded. The original
          pending review is preserved.
          <Button onClick={retryStorage}>Retry storage access</Button>
          <Button onClick={studyReadOnly}>Study without recording reviews</Button>
        </AlertDescription>
      </Alert>
    )

  if (session.isPending || !reconciled) {
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
        {unresolved.current.length > 0 && (
          <p role="status">
            {unresolved.current.length} unresolved review outcome(s) remain saved and are not
            included in confirmed counts.
          </p>
        )}
        {readOnly && (
          <p role="status">
            Read-only study: no reviews were recorded. Saved recovery remains untouched.
          </p>
        )}
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
        {unresolved.current.length > 0 && (
          <p role="status">
            {unresolved.current.length} unresolved review outcome(s) remain saved and are not
            included in confirmed counts.
          </p>
        )}
        {readOnly && (
          <p role="status">
            Read-only study: no reviews were recorded. Saved recovery remains untouched.
          </p>
        )}
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
          disabled={readOnly || reviewing || Boolean(operation.current) || missing}
          onOpenChange={setActionsOpen}
          onUpdated={(content) => {
            const next = queue.map((card) =>
              card.part_id === current.part_id ? { ...card, card: content } : card,
            )
            try {
              persist({
                queue: next,
                total,
                ratings,
                states: [...states],
                operation: operation.current,
              })
              setQueue(next)
            } catch {
              setStorageError(true)
            }
          }}
          onRemoved={() => removeCurrent()}
        />
      </div>
      <p className="sr-only" role="status">
        {flipped ? 'Answer shown.' : 'Question shown.'}
      </p>

      {readOnly && (
        <p role="status">
          Read-only study: reviews are not recorded. Saved uncertain review evidence remains
          untouched.
        </p>
      )}
      {notice && <p role="status">{notice}</p>}
      {missing && (
        <Alert variant="destructive">
          <AlertTitle>This card is no longer available</AlertTitle>
          <AlertDescription>
            Its review outcome cannot be confirmed. Keep its original operation saved separately and
            continue without counting it as a review.
            <Button onClick={() => removeCurrent(true)}>Continue remaining cards</Button>
          </AlertDescription>
        </Alert>
      )}
      {retryRating && !missing ? (
        <p role="alert" className="text-danger-text text-sm">
          That {RATING_LABELS[retryRating]} review could not be confirmed. Choose{' '}
          {RATING_LABELS[retryRating]} again to confirm it before using another rating.
        </p>
      ) : null}
      {flipped && readOnly ? (
        <Button
          onClick={() => {
            setQueue(queue.slice(1))
            setFlipped(false)
          }}
        >
          Next card (not recorded)
        </Button>
      ) : flipped ? (
        <div
          className="grid grid-cols-2 gap-2 min-[360px]:grid-cols-4"
          role="group"
          aria-label="Rate this card"
        >
          {RATINGS.map((rating, index) => (
            <Button
              key={rating}
              variant="outline"
              disabled={reviewing || missing || (retryRating !== null && rating !== retryRating)}
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
          Press <Kbd>Space</Kbd> or choose Show answer.
        </p>
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
