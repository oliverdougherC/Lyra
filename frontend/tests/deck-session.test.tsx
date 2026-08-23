import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { DeckSession } from '@/components/study/deck-session'
import { api } from '@/lib/api'
import type { CardStateRead, DeckSession as DeckSessionRead, SessionCard } from '@/types'

vi.mock('next/navigation', () => ({
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
  vi.spyOn(api, 'getDeckSession').mockResolvedValue(SESSION)
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

    await userEvent.click(screen.getByRole('button', { name: /Press Space to flip/ }))

    // Each rating says when the card comes back, from the scheduler's own math.
    const good = screen.getByRole('button', { name: /Good/ })
    expect(good).toHaveTextContent('2 d')
    expect(screen.getByRole('button', { name: /Again/ })).toHaveTextContent('10 min')
  })

  it('rates with the buttons and keeps the rated card out of the queue', async () => {
    const { wrapper } = createWrapper()
    render(<DeckSession deckId={8} />, { wrapper })

    await screen.findByText('What is the Fourier transform of a delta?')
    await userEvent.click(screen.getByRole('button', { name: /Press Space to flip/ }))
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
    await userEvent.click(screen.getByRole('button', { name: /Press Space to flip/ }))
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
})
