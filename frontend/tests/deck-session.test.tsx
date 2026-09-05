import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { DeckSession } from '@/components/study/deck-session'
import { api, ApiError } from '@/lib/api'
import type { CardStateRead, DeckSession as DeckSessionRead, SessionCard } from '@/types'

vi.mock('@/router/hooks', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }),
  useParams: () => ({ id: '1', artifactId: '8' }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => '/classes/1/study/8',
}))

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  return { wrapper }
}

function newCardStateRead(): CardStateRead {
  return {
    due_at: '2026-08-06 12:00:00',
    stability: 0,
    difficulty: 5,
    reps: 0,
    lapses: 0,
    state: 'new',
    last_review_at: null,
    bucket: 'new',
  }
}

function sessionCard(partId: number, front: string, back: string): SessionCard {
  return {
    part_id: partId,
    label: null,
    card: { front, back, topic: 'Signals' },
    due: true,
    card_state: newCardStateRead(),
  }
}

const SESSION: DeckSessionRead = {
  cards: [
    sessionCard(11, 'What is the Fourier transform of a delta?', 'A flat spectrum'),
    sessionCard(12, 'What does linearity require?', 'Superposition'),
  ],
}

/** The server's answer to a rating, close enough to the real scheduler for the summary. */
function reviewedState(rating: string): CardStateRead {
  if (rating === 'again') {
    return {
      due_at: '2026-08-06 12:10:00',
      stability: 0.5,
      difficulty: 6,
      reps: 1,
      lapses: 1,
      state: 'learning',
      last_review_at: '2026-08-06 12:00:00',
      bucket: 'learning',
    }
  }
  return {
    due_at: '2026-08-08 12:00:00',
    stability: 2,
    difficulty: 4.9,
    reps: 1,
    lapses: 0,
    state: 'learning',
    last_review_at: '2026-08-06 12:00:00',
    bucket: 'learning',
  }
}

beforeEach(() => {
  vi.restoreAllMocks()
  sessionStorage.clear()
  vi.spyOn(api, 'getDeckSession').mockResolvedValue(SESSION)
  vi.spyOn(api, 'getDeck').mockResolvedValue({ cards: SESSION.cards } as never)
  vi.spyOn(api, 'reviewCard').mockImplementation((_partId, rating) =>
    Promise.resolve(reviewedState(rating)),
  )
})

describe('DeckSession', () => {
  it('shows the front first and offers the ratings only once flipped', async () => {
    const { wrapper } = createWrapper()
    render(<DeckSession deckId={8} />, { wrapper })

    expect(await screen.findByText('What is the Fourier transform of a delta?')).toBeInTheDocument()
    expect(screen.getByText('Card 1 of 2')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Good/ })).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Show answer' }))

    // Each rating says when the card comes back, from the scheduler's own math.
    const good = screen.getByRole('button', { name: /Good/ })
    expect(good).toHaveTextContent('2 d')
    expect(screen.getByRole('button', { name: /Again/ })).toHaveTextContent('10 min')
  })

  it('rates with the buttons and keeps the rated card out of the queue', async () => {
    const { wrapper } = createWrapper()
    render(<DeckSession deckId={8} />, { wrapper })

    await screen.findByText('What is the Fourier transform of a delta?')
    await userEvent.click(screen.getByRole('button', { name: 'Show answer' }))
    await userEvent.click(screen.getByRole('button', { name: /Good/ }))

    await waitFor(() => expect(api.reviewCard).toHaveBeenCalledWith(11, 'good', expect.any(String)))
    expect(await screen.findByText('Card 2 of 2')).toBeInTheDocument()
    expect(screen.getByText('What does linearity require?')).toBeInTheDocument()
    // The next card starts on its front again.
    expect(screen.queryByRole('button', { name: /Good/ })).not.toBeInTheDocument()
  })

  it('rates with the number keys only while the back is showing', async () => {
    const { wrapper } = createWrapper()
    render(<DeckSession deckId={8} />, { wrapper })

    await screen.findByText('What is the Fourier transform of a delta?')

    // A rating key on an unflipped card is not a rating.
    await userEvent.keyboard('1')
    expect(api.reviewCard).not.toHaveBeenCalled()

    await userEvent.keyboard(' ')
    expect(screen.getByRole('button', { name: /Again/ })).toBeInTheDocument()

    await userEvent.keyboard('1')
    await waitFor(() =>
      expect(api.reviewCard).toHaveBeenCalledWith(11, 'again', expect.any(String)),
    )
  })

  it('reuses one operation id when a failed review is retried', async () => {
    // A lost or failed response keeps the card in place; retrying it must reuse the same
    // idempotency key so the server records the review once (PLA-296).
    const review = vi
      .spyOn(api, 'reviewCard')
      .mockRejectedValueOnce(new Error('network'))
      .mockImplementation((_partId, rating) => Promise.resolve(reviewedState(rating)))
    const { wrapper } = createWrapper()
    render(<DeckSession deckId={8} />, { wrapper })

    await screen.findByText('What is the Fourier transform of a delta?')
    await userEvent.click(screen.getByRole('button', { name: 'Show answer' }))
    await userEvent.click(screen.getByRole('button', { name: /Good/ }))
    // The first call failed; the card is still here to retry.
    await waitFor(() => expect(review).toHaveBeenCalledTimes(1))
    await userEvent.click(screen.getByRole('button', { name: /Good/ }))
    await waitFor(() => expect(review).toHaveBeenCalledTimes(2))

    const firstOperationId = review.mock.calls[0][2]
    const secondOperationId = review.mock.calls[1][2]
    expect(firstOperationId).toBe(secondOperationId)
  })

  it('ends the session with the rating counts and the buckets after', async () => {
    const { wrapper } = createWrapper()
    render(<DeckSession deckId={8} />, { wrapper })

    await screen.findByText('What is the Fourier transform of a delta?')
    await userEvent.keyboard(' ')
    await userEvent.keyboard('3')

    expect(await screen.findByText('What does linearity require?')).toBeInTheDocument()
    await userEvent.keyboard(' ')
    await userEvent.keyboard('1')

    expect(await screen.findByText('Session complete')).toBeInTheDocument()
    expect(screen.getByText(/1 good/)).toBeInTheDocument()
    expect(screen.getByText(/1 again/)).toBeInTheDocument()
    // Both cards lapsed into or stayed in learning, recomputed from the server's answers.
    expect(screen.getByText(/learning 2/)).toBeInTheDocument()
  })

  it('successful review retires the operation ID so Study Again mints a fresh one', async () => {
    const review = vi.spyOn(api, 'reviewCard')
    const { wrapper } = createWrapper()
    render(<DeckSession deckId={8} />, { wrapper })

    // Rate card 1 (part_id 11) successfully.
    await screen.findByText('What is the Fourier transform of a delta?')
    await userEvent.keyboard(' ')
    await userEvent.keyboard('3')

    // Rate card 2 (part_id 12) to complete the session.
    await screen.findByText('What does linearity require?')
    await userEvent.keyboard(' ')
    await userEvent.keyboard('3')

    await screen.findByText('Session complete')

    // Restart the session. This calls operationIds.current.clear() and refetches.
    await userEvent.click(screen.getByRole('button', { name: /Study again/ }))

    // Card 1 reappears. Rate it again.
    await screen.findByText('What is the Fourier transform of a delta?')
    await userEvent.keyboard(' ')
    await userEvent.keyboard('3')

    // The first rating of card 1 (call 0) and the post-restart rating (call 2) must
    // carry different operation IDs: the first was retired on success, and clear()
    // removed any residual, so the second round mints a fresh UUID.
    const firstId = review.mock.calls[0][2]
    const restartId = review.mock.calls[2][2]
    expect(firstId).toBeTypeOf('string')
    expect(restartId).toBeTypeOf('string')
    expect(restartId).not.toBe(firstId)
  })

  it('each card in a session gets its own distinct operation ID', async () => {
    const review = vi.spyOn(api, 'reviewCard')
    const { wrapper } = createWrapper()
    render(<DeckSession deckId={8} />, { wrapper })

    // Rate card 1.
    await screen.findByText('What is the Fourier transform of a delta?')
    await userEvent.keyboard(' ')
    await userEvent.keyboard('3')

    // Rate card 2.
    await screen.findByText('What does linearity require?')
    await userEvent.keyboard(' ')
    await userEvent.keyboard('3')

    await waitFor(() => expect(review).toHaveBeenCalledTimes(2))

    const idCard1 = review.mock.calls[0][2]
    const idCard2 = review.mock.calls[1][2]
    expect(idCard1).toBeTypeOf('string')
    expect(idCard2).toBeTypeOf('string')
    expect(idCard1).not.toBe(idCard2)
  })

  it('after a failed-then-successful retry, Study Again still mints a fresh ID', async () => {
    // Fail card 1 once, succeed on retry (same ID), finish the session, restart,
    // and rate card 1 again. The post-restart ID must be new.
    const review = vi
      .spyOn(api, 'reviewCard')
      .mockRejectedValueOnce(new Error('network'))
      .mockImplementation((_partId, rating) => Promise.resolve(reviewedState(rating)))
    const { wrapper } = createWrapper()
    render(<DeckSession deckId={8} />, { wrapper })

    // First attempt at card 1 fails.
    await screen.findByText('What is the Fourier transform of a delta?')
    await userEvent.keyboard(' ')
    await userEvent.keyboard('3')
    await waitFor(() => expect(review).toHaveBeenCalledTimes(1))

    // Retry succeeds, reusing the same operation ID.
    await userEvent.keyboard('3')
    await waitFor(() => expect(review).toHaveBeenCalledTimes(2))
    expect(review.mock.calls[0][2]).toBe(review.mock.calls[1][2])

    // Rate card 2 to complete the session.
    await screen.findByText('What does linearity require?')
    await userEvent.keyboard(' ')
    await userEvent.keyboard('3')
    await waitFor(() => expect(review).toHaveBeenCalledTimes(3))

    await screen.findByText('Session complete')

    // Restart and rate card 1 again.
    await userEvent.click(screen.getByRole('button', { name: /Study again/ }))
    await screen.findByText('What is the Fourier transform of a delta?')
    await userEvent.keyboard(' ')
    await userEvent.keyboard('3')
    await waitFor(() => expect(review).toHaveBeenCalledTimes(4))

    // The retry pair (calls 0 and 1) shared one ID; the post-restart rating (call 3)
    // must carry a completely new ID.
    const retryId = review.mock.calls[0][2]
    const freshId = review.mock.calls[3][2]
    expect(freshId).toBeTypeOf('string')
    expect(freshId).not.toBe(retryId)
  })
})

it('keeps card content outside button semantics and exposes only the current face', async () => {
  render(<DeckSession deckId={8} />, { wrapper: createWrapper().wrapper })
  const question = await screen.findByText('What is the Fourier transform of a delta?')
  expect(question.closest('[role="button"],button')).toBeNull()
  expect(screen.queryByText('A flat spectrum')).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Show answer' }))
  expect(screen.getByText('A flat spectrum')).toBeVisible()
  expect(screen.getByRole('region', { name: 'Card answer' })).toHaveFocus()
  expect(screen.queryByText('What is the Fourier transform of a delta?')).not.toBeInTheDocument()
})

it('preserves a failed card edit and saves corrected content without rating it', async () => {
  const update = vi
    .spyOn(api, 'updateCard')
    .mockRejectedValueOnce(new Error('offline'))
    .mockImplementation((partId, card) => Promise.resolve({ part_id: partId, card }))
  render(<DeckSession deckId={8} />, { wrapper: createWrapper().wrapper })
  await userEvent.click(await screen.findByRole('button', { name: 'Card actions' }))
  await userEvent.click(screen.getByRole('menuitem', { name: 'Edit card' }))
  await userEvent.clear(screen.getByLabelText('Question'))
  await userEvent.type(screen.getByLabelText('Question'), 'Corrected question')
  await userEvent.clear(screen.getByLabelText('Answer'))
  await userEvent.type(screen.getByLabelText('Answer'), 'Corrected answer')
  await userEvent.clear(screen.getByLabelText('Topic'))
  await userEvent.type(screen.getByLabelText('Topic'), 'Corrected topic')
  await userEvent.click(screen.getByRole('button', { name: 'Save card' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Your edits are still here')
  expect(screen.getByLabelText('Question')).toHaveValue('Corrected question')
  await userEvent.keyboard('3')
  expect(api.reviewCard).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: 'Save card' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  expect(screen.getByText('Corrected question')).toBeVisible()
  expect(update).toHaveBeenLastCalledWith(11, {
    front: 'Corrected question',
    back: 'Corrected answer',
    topic: 'Corrected topic',
  })
  await userEvent.click(screen.getByRole('button', { name: 'Show answer' }))
  expect(screen.getByText('Corrected answer')).toBeVisible()
})

it('confirms removal, retains a card on failure, and advances without a recall rating', async () => {
  const remove = vi
    .spyOn(api, 'deleteCard')
    .mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValue(undefined)
  render(<DeckSession deckId={8} />, { wrapper: createWrapper().wrapper })
  await userEvent.click(await screen.findByRole('button', { name: 'Card actions' }))
  await userEvent.click(screen.getByRole('menuitem', { name: 'Remove card' }))
  expect(remove).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: 'Remove card' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not remove this card')
  await userEvent.click(screen.getByRole('button', { name: 'Remove card' }))
  await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
  expect(screen.getByText('Card 1 of 1')).toBeVisible()
  expect(screen.getByText('What does linearity require?')).toBeVisible()
  expect(api.reviewCard).not.toHaveBeenCalled()
})

it('reports session counts separately from a larger deck and its remaining due cards', async () => {
  vi.spyOn(api, 'getDeck').mockResolvedValue({
    cards: Array.from({ length: 50 }, (_, i) => sessionCard(i + 1, 'Question', 'Answer')),
  } as never)
  render(<DeckSession deckId={8} />, { wrapper: createWrapper().wrapper })
  await screen.findByText('Card 1 of 2')
  await userEvent.keyboard(' ')
  await userEvent.keyboard('3')
  await screen.findByText('Card 2 of 2')
  await userEvent.keyboard(' ')
  await userEvent.keyboard('3')
  expect(await screen.findByText(/Cards in this session:/)).toHaveTextContent('learning 2')
  expect(await screen.findByText(/Deck total: 50 cards/)).toHaveTextContent('50 due now')
})

it('retries the original rating after a lost response instead of relabeling its saved schedule', async () => {
  const review = vi
    .spyOn(api, 'reviewCard')
    .mockRejectedValueOnce(new Error('response lost'))
    .mockResolvedValue(reviewedState('good'))
  render(<DeckSession deckId={8} />, { wrapper: createWrapper().wrapper })
  await userEvent.click(await screen.findByRole('button', { name: 'Show answer' }))
  await userEvent.click(screen.getByRole('button', { name: /Good/ }))
  await waitFor(() => expect(screen.getByRole('button', { name: /Easy/ })).toBeDisabled())
  expect(screen.getByRole('alert')).toHaveTextContent('Choose Good again to confirm')
  await userEvent.keyboard('4')
  expect(review).toHaveBeenCalledTimes(1)
  await userEvent.click(screen.getByRole('button', { name: /Good/ }))
  await screen.findByText('Card 2 of 2')
  expect(review.mock.calls[1]).toEqual([11, 'good', review.mock.calls[0][2]])
  await userEvent.keyboard(' ')
  await userEvent.keyboard('3')
  expect(await screen.findByText(/You reviewed 2 cards/)).toHaveTextContent('2 good · 0 easy')
})

it('finishes a confirmed removal when a lost delete response is followed by already-missing', async () => {
  vi.spyOn(api, 'deleteCard')
    .mockRejectedValueOnce(new Error('response lost'))
    .mockRejectedValueOnce(new ApiError(404, 'Card not found'))
  render(<DeckSession deckId={8} />, { wrapper: createWrapper().wrapper })
  await userEvent.click(await screen.findByRole('button', { name: 'Card actions' }))
  await userEvent.click(screen.getByRole('menuitem', { name: 'Remove card' }))
  await userEvent.click(screen.getByRole('button', { name: 'Remove card' }))
  await screen.findByRole('alert')
  await userEvent.click(screen.getByRole('button', { name: 'Remove card' }))
  expect(await screen.findByText('Card 1 of 1')).toBeVisible()
  expect(screen.getByText('What does linearity require?')).toBeVisible()
  expect(api.reviewCard).not.toHaveBeenCalled()
})

describe('durable rating recovery', () => {
  it.each([false, true])(
    'recovers after reload when first request committed=%s',
    async (committedBeforeLoss) => {
      vi.spyOn(api, 'getDeckSession').mockResolvedValue({ cards: [SESSION.cards[0]] })
      const log = new Map<string, { rating: string; state: CardStateRead }>()
      let fail = true
      const review = vi.spyOn(api, 'reviewCard').mockImplementation(async (_partId, rating, id) => {
        // The operation is durable before any network request, including the first.
        expect(JSON.parse(sessionStorage.getItem('lyra:study-session:v1:8')!).operation).toEqual({
          id,
          rating,
        })
        if (!log.has(id) && (!fail || committedBeforeLoss))
          log.set(id, { rating, state: reviewedState(rating) })
        if (fail) {
          fail = false
          throw new Error('lost response')
        }
        expect(log.get(id)?.rating).toBe(rating)
        return log.get(id)!.state
      })
      const first = render(<DeckSession deckId={8} />, { wrapper: createWrapper().wrapper })
      await screen.findByText('What is the Fourier transform of a delta?')
      await userEvent.keyboard(' ')
      await userEvent.keyboard('4')
      await screen.findByRole('alert')
      expect(screen.getByRole('button', { name: /Again/ })).toBeDisabled()
      await userEvent.keyboard('1')
      expect(review).toHaveBeenCalledTimes(1)
      first.unmount()

      // A committed card may no longer be returned in the server's due queue.
      vi.spyOn(api, 'getDeckSession').mockResolvedValue({ cards: [] })
      const second = render(<DeckSession deckId={8} />, { wrapper: createWrapper().wrapper })
      await screen.findByRole('alert')
      expect(screen.getByRole('button', { name: /Again/ })).toBeDisabled()
      await userEvent.click(screen.getByRole('button', { name: /Easy/ }))
      await screen.findByText('Session complete')
      expect(screen.getByText(/1 easy/)).toHaveTextContent('0 again')
      expect(log.size).toBe(1)
      expect(review.mock.calls[1]).toEqual(review.mock.calls[0])
      const acknowledged = JSON.parse(sessionStorage.getItem('lyra:study-session:v1:8')!)
      expect(acknowledged.operation).toBeNull()
      expect(acknowledged.states[0][1].dueAt).toBe(
        new Date(reviewedState('easy').due_at + 'Z').toISOString(),
      )
      second.unmount()
      render(<DeckSession deckId={8} />, { wrapper: createWrapper().wrapper })
      await screen.findByText('Session complete')
      expect(screen.getByText(/1 easy/)).toHaveTextContent('0 again')
      expect(review).toHaveBeenCalledTimes(2)
    },
  )

  it('does not send a review when persisting its key fails', async () => {
    render(<DeckSession deckId={8} />, { wrapper: createWrapper().wrapper })
    await screen.findByText('What is the Fourier transform of a delta?')
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota')
    })
    await userEvent.keyboard(' ')
    await userEvent.keyboard('4')
    await screen.findByRole('alert')
    expect(api.reviewCard).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Retry storage access' })).toBeInTheDocument()
  })
})

it.each(['{', '{}'])('blocks reviews if stored recovery is invalid: %s', async (value) => {
  sessionStorage.setItem('lyra:study-session:v1:8', value)
  render(<DeckSession deckId={8} />, { wrapper: createWrapper().wrapper })
  await screen.findByText('Could not restore this study session')
  await userEvent.keyboard(' 4')
  expect(api.reviewCard).not.toHaveBeenCalled()
})

describe('recovery ownership and authoritative cards', () => {
  const saved = () => JSON.parse(sessionStorage.getItem('lyra:study-session:v1:8')!)
  function seed(operation: { id: string; rating: string } | null = null) {
    sessionStorage.setItem(
      'lyra:study-session:v1:8',
      JSON.stringify({
        queue: SESSION.cards,
        total: 2,
        ratings: { again: 0, hard: 0, good: 0, easy: 0 },
        states: [],
        operation,
      }),
    )
  }
  it('old A continuation cannot replace B pending after navigate back and reload', async () => {
    let releaseA!: (state: CardStateRead) => void
    const log = new Map<string, CardStateRead>()
    const review = vi.spyOn(api, 'reviewCard').mockImplementation((_part, rating, id) => {
      if (!log.has(id)) log.set(id, reviewedState(rating))
      if (review.mock.calls.length === 1)
        return new Promise((resolve) => {
          releaseA = resolve
        })
      if (review.mock.calls.length === 3) return new Promise(() => {})
      return Promise.resolve(log.get(id)!)
    })
    const first = render(<DeckSession deckId={8} />, { wrapper: createWrapper().wrapper })
    await screen.findByText(SESSION.cards[0].card.front)
    await userEvent.keyboard(' 3')
    await waitFor(() => expect(review).toHaveBeenCalledTimes(1))
    const a = saved().operation
    first.unmount()
    const second = render(<DeckSession deckId={8} />, { wrapper: createWrapper().wrapper })
    await screen.findByRole('button', { name: /Good/ })
    await userEvent.click(screen.getByRole('button', { name: /Good/ }))
    await screen.findByText(SESSION.cards[1].card.front)
    expect(review.mock.calls[1][2]).toBe(a.id)
    await userEvent.keyboard(' 2')
    await waitFor(() => expect(review).toHaveBeenCalledTimes(3))
    const b = saved()
    releaseA(reviewedState('good'))
    await new Promise((resolve) => setTimeout(resolve, 10))
    expect(saved()).toEqual(b)
    second.unmount()
    render(<DeckSession deckId={8} />, { wrapper: createWrapper().wrapper })
    await screen.findByRole('button', { name: /Hard/ })
    await userEvent.click(screen.getByRole('button', { name: /Hard/ }))
    await screen.findByText('Session complete')
    expect(review.mock.calls[3][2]).toBe(b.operation.id)
    expect(log.size).toBe(2)
    expect(saved().ratings).toEqual({ again: 0, hard: 1, good: 1, easy: 0 })
  })
  it('drops a deleted restored card instead of offering it forever', async () => {
    seed()
    vi.spyOn(api, 'getDeck').mockResolvedValue({ cards: [SESSION.cards[1]] } as never)
    vi.spyOn(api, 'getDeckSession').mockResolvedValue({ cards: [SESSION.cards[1]] })
    render(<DeckSession deckId={8} />, { wrapper: createWrapper().wrapper })
    await screen.findByText(SESSION.cards[1].card.front)
    expect(screen.queryByText(SESSION.cards[0].card.front)).not.toBeInTheDocument()
    expect(saved().queue.map((card: SessionCard) => card.part_id)).toEqual([12])
    expect(saved().total).toBe(1)
    expect(saved().ratings.good).toBe(0)
  })
  it('refreshes corrected card content and state outside limited session', async () => {
    seed({ id: 'original-key', rating: 'good' })
    vi.spyOn(api, 'getDeckSession').mockResolvedValue({ cards: [SESSION.cards[1]] })
    vi.spyOn(api, 'getDeck').mockResolvedValue({
      cards: [
        {
          ...SESSION.cards[0],
          card: { ...SESSION.cards[0].card, back: 'Corrected answer' },
          card_state: reviewedState('good'),
        },
        SESSION.cards[1],
      ],
    } as never)
    render(<DeckSession deckId={8} />, { wrapper: createWrapper().wrapper })
    await screen.findByText('Corrected answer')
    await userEvent.click(screen.getByRole('button', { name: /Good/ }))
    await screen.findByText(SESSION.cards[1].card.front)
    expect(api.reviewCard).toHaveBeenCalledWith(11, 'good', 'original-key')
    expect(saved().ratings.good).toBe(1)
  })
  it('preserves removed unresolved operation and continues valid cards after 404', async () => {
    seed({ id: 'uncertain-key', rating: 'easy' })
    vi.spyOn(api, 'reviewCard').mockRejectedValueOnce(new ApiError(404, 'Card not found'))
    render(<DeckSession deckId={8} />, { wrapper: createWrapper().wrapper })
    await screen.findByRole('button', { name: /Easy/ })
    await userEvent.click(screen.getByRole('button', { name: /Easy/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'Continue remaining cards' }))
    await screen.findByText(SESSION.cards[1].card.front)
    expect(saved().unresolved).toEqual([
      { partId: 11, operation: { id: 'uncertain-key', rating: 'easy' } },
    ])
    expect(saved().ratings.easy).toBe(0)
    await userEvent.keyboard(' 3')
    await screen.findByText('Session complete')
    expect(saved().ratings.good).toBe(1)
    expect(saved().ratings.easy).toBe(0)
  })
  it('offers read-only study for malformed storage without touching uncertain evidence', async () => {
    sessionStorage.setItem('lyra:study-session:v1:8', '{broken')
    render(<DeckSession deckId={8} />, { wrapper: createWrapper().wrapper })
    await userEvent.click(
      await screen.findByRole('button', { name: 'Study without recording reviews' }),
    )
    await screen.findByText(SESSION.cards[0].card.front)
    await userEvent.keyboard(' 3')
    expect(api.reviewCard).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Next card (not recorded)' }))
    await screen.findByText(SESSION.cards[1].card.front)
    expect(sessionStorage.getItem('lyra:study-session:v1:8')).toBe('{broken')
  })
})

it.each(['edit', 'remove'])(
  'reconciles committed %s after snapshot write failure through in-place retry',
  async (action) => {
    sessionStorage.setItem(
      'lyra:study-session:v1:8',
      JSON.stringify({
        queue: SESSION.cards,
        total: 2,
        ratings: { again: 0, hard: 0, good: 0, easy: 0 },
        states: [],
        operation: null,
      }),
    )
    render(<DeckSession deckId={8} />, { wrapper: createWrapper().wrapper })
    await userEvent.click(await screen.findByRole('button', { name: 'Card actions' }))
    if (action === 'edit') {
      await userEvent.click(screen.getByRole('menuitem', { name: 'Edit card' }))
      await userEvent.clear(screen.getByLabelText('Question'))
      await userEvent.type(screen.getByLabelText('Question'), 'Persisted correction')
      vi.spyOn(api, 'updateCard').mockImplementation(async (partId, card) => {
        vi.mocked(api.getDeck).mockResolvedValue({
          cards: [{ ...SESSION.cards[0], card }, SESSION.cards[1]],
        } as never)
        return { part_id: partId, card }
      })
    } else {
      await userEvent.click(screen.getByRole('menuitem', { name: 'Remove card' }))
      vi.spyOn(api, 'deleteCard').mockImplementation(async () => {
        vi.mocked(api.getDeck).mockResolvedValue({ cards: [SESSION.cards[1]] } as never)
      })
    }
    vi.spyOn(Storage.prototype, 'setItem').mockImplementationOnce(() => {
      throw new Error('quota')
    })
    await userEvent.click(
      screen.getByRole('button', { name: action === 'edit' ? 'Save card' : 'Remove card' }),
    )
    await userEvent.click(await screen.findByRole('button', { name: 'Retry storage access' }))
    await screen.findByText(
      action === 'edit' ? 'Persisted correction' : SESSION.cards[1].card.front,
    )
    expect(screen.queryByText(SESSION.cards[0].card.front)).not.toBeInTheDocument()
    expect(api.reviewCard).not.toHaveBeenCalled()
  },
)

it('requires a fresh restore if recovery changes between render and ownership claim', async () => {
  const key = 'lyra:study-session:v1:8'
  const initial = {
    queue: SESSION.cards,
    total: 2,
    ratings: { again: 0, hard: 0, good: 0, easy: 0 },
    states: [],
    operation: { id: 'A', rating: 'good' },
  }
  const newer = JSON.stringify({
    ...initial,
    queue: [SESSION.cards[1]],
    ratings: { ...initial.ratings, good: 1 },
    operation: { id: 'B', rating: 'hard' },
  })
  sessionStorage.setItem(key, JSON.stringify(initial))
  const get = Storage.prototype.getItem
  vi.spyOn(Storage.prototype, 'getItem').mockImplementationOnce(function (this: Storage, name) {
    const rendered = get.call(this, name)
    this.setItem(key, newer)
    return rendered
  })
  render(<DeckSession deckId={8} />, { wrapper: createWrapper().wrapper })
  await screen.findByRole('button', { name: 'Retry storage access' })
  expect(sessionStorage.getItem(key)).toBe(newer)
  expect(api.reviewCard).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: 'Retry storage access' }))
  await userEvent.click(await screen.findByRole('button', { name: /Hard/ }))
  await screen.findByText('Session complete')
  expect(api.reviewCard).toHaveBeenCalledWith(12, 'hard', 'B')
})

it('returns from read-only study by restoring the original pending operation', async () => {
  const key = 'lyra:study-session:v1:8'
  const raw = JSON.stringify({
    queue: SESSION.cards,
    total: 2,
    ratings: { again: 0, hard: 0, good: 0, easy: 0 },
    states: [],
    operation: { id: 'original-uncertain', rating: 'easy' },
  })
  sessionStorage.setItem(key, raw)
  vi.spyOn(Storage.prototype, 'getItem').mockImplementationOnce(() => {
    throw new Error('storage unavailable')
  })
  render(<DeckSession deckId={8} />, { wrapper: createWrapper().wrapper })
  await userEvent.click(
    await screen.findByRole('button', { name: 'Study without recording reviews' }),
  )
  await screen.findByText(SESSION.cards[0].card.front)
  await userEvent.keyboard(' 3')
  expect(api.reviewCard).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: 'Next card (not recorded)' }))
  await screen.findByText(SESSION.cards[1].card.front)
  expect(sessionStorage.getItem(key)).toBe(raw)
  await userEvent.click(screen.getByRole('button', { name: 'Retry recording reviews' }))
  await userEvent.click(await screen.findByRole('button', { name: /Easy/ }))
  await screen.findByText(SESSION.cards[1].card.front)
  expect(api.reviewCard).toHaveBeenCalledWith(11, 'easy', 'original-uncertain')
  expect(api.reviewCard).toHaveBeenCalledTimes(1)
})

it('does not carry read-only mode into another deck', async () => {
  sessionStorage.setItem('lyra:study-session:v1:8', '{broken')
  const view = render(<DeckSession deckId={8} />, { wrapper: createWrapper().wrapper })
  await userEvent.click(
    await screen.findByRole('button', { name: 'Study without recording reviews' }),
  )
  await screen.findByText(SESSION.cards[0].card.front)
  view.rerender(<DeckSession deckId={9} />)
  await screen.findByText(SESSION.cards[0].card.front)
  expect(screen.queryByRole('button', { name: 'Retry recording reviews' })).not.toBeInTheDocument()
  await userEvent.keyboard(' 3')
  await waitFor(() => expect(api.reviewCard).toHaveBeenCalledTimes(1))
  expect(sessionStorage.getItem('lyra:study-session:v1:8')).toBe('{broken')
})
