import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import StudySessionPage from '@/app/classes/[id]/study/[artifactId]/page'
import { api } from '@/lib/api'
import type { DeckSummary, StudyArtifactRead, StudyStatus } from '@/types'

// The artifact id used for deck tests vs quiz tests.
let artifactId = '8'

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }),
  useParams: () => ({ id: '1', artifactId }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => `/classes/1/study/${artifactId}`,
}))

vi.mock('@/components/study/deck-session', () => ({
  DeckSession: ({ deckId }: { deckId: number }) => (
    <div data-testid="deck-session">DeckSession for {deckId}</div>
  ),
}))
vi.mock('@/components/study/quiz-runner', () => ({
  QuizRunner: ({ classId, quizId }: { classId: number; quizId: number }) => (
    <div data-testid="quiz-runner">
      QuizRunner for {classId}/{quizId}
    </div>
  ),
}))
vi.mock('@/components/chat/lyra-mark', () => ({
  LyraMark: () => <div data-testid="lyra-mark" />,
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

function deck(overrides: Partial<DeckSummary>): DeckSummary {
  return {
    id: 8,
    class_id: 1,
    kind: 'flashcard_deck',
    title: 'Signals flashcards',
    state: 'ready',
    stage_detail: null,
    problems_total: 12,
    problems_done: 12,
    error_message: null,
    created_at: '2026-08-05 09:00:00',
    updated_at: '2026-08-05 09:00:00',
    cards_total: 12,
    due_count: 0,
    buckets: { new: 3, learning: 2, mastered: 1 },
    ...overrides,
  }
}

function quiz(overrides: Partial<StudyArtifactRead>): StudyArtifactRead {
  return {
    id: 9,
    class_id: 1,
    kind: 'quiz',
    title: 'Week 4 quiz',
    state: 'ready',
    stage_detail: null,
    problems_total: 10,
    problems_done: 10,
    error_message: null,
    created_at: '2026-08-05 09:00:00',
    updated_at: '2026-08-05 09:00:00',
    ...overrides,
  }
}

function status(overrides: Partial<StudyStatus>): StudyStatus {
  return {
    state: 'ready',
    stage_detail: null,
    problems_total: 12,
    problems_done: 12,
    error_message: null,
    ...overrides,
  }
}

beforeEach(() => {
  vi.restoreAllMocks()
  artifactId = '8'
})

describe('StudySessionPage', () => {
  it('pending deck shows generating UI, no DeckSession', async () => {
    vi.spyOn(api, 'listStudy').mockResolvedValue({
      decks: [deck({ state: 'pending', problems_total: null, problems_done: 0 })],
      quizzes: [],
    })
    vi.spyOn(api, 'getDeckStatus').mockResolvedValue(
      status({ state: 'pending', problems_total: null, problems_done: 0 }),
    )
    const { wrapper } = createWrapper()

    render(<StudySessionPage />, { wrapper })

    expect(await screen.findByText('Queued')).toBeInTheDocument()
    expect(screen.getByText('Signals flashcards')).toBeInTheDocument()
    expect(screen.queryByTestId('deck-session')).not.toBeInTheDocument()
  })

  it('generating deck shows progress bar with stage detail', async () => {
    vi.spyOn(api, 'listStudy').mockResolvedValue({
      decks: [
        deck({
          state: 'generating',
          stage_detail: 'Writing cards for Linear systems',
          problems_total: 5,
          problems_done: 2,
        }),
      ],
      quizzes: [],
    })
    vi.spyOn(api, 'getDeckStatus').mockResolvedValue(
      status({
        state: 'generating',
        stage_detail: 'Writing cards for Linear systems',
        problems_total: 5,
        problems_done: 2,
      }),
    )
    const { wrapper } = createWrapper()

    render(<StudySessionPage />, { wrapper })

    expect(await screen.findByText('Writing')).toBeInTheDocument()
    expect(screen.getByText('Writing cards for Linear systems')).toBeInTheDocument()
    expect(screen.getByRole('progressbar', { name: '2 of 5 written' })).toBeInTheDocument()
  })

  it('failed deck shows error message and back link', async () => {
    vi.spyOn(api, 'listStudy').mockResolvedValue({
      decks: [deck({ state: 'failed', error_message: 'No topics found' })],
      quizzes: [],
    })
    vi.spyOn(api, 'getDeckStatus').mockResolvedValue(
      status({ state: 'failed', error_message: 'No topics found' }),
    )
    const { wrapper } = createWrapper()

    render(<StudySessionPage />, { wrapper })

    expect(await screen.findByText('No topics found')).toBeInTheDocument()
    const backLink = screen.getByRole('link', { name: /Back to study tools/ })
    expect(backLink).toHaveAttribute('href', '/classes/1?tab=study')
    expect(screen.queryByTestId('deck-session')).not.toBeInTheDocument()
  })

  it('cancelled deck shows cancellation message and back link', async () => {
    vi.spyOn(api, 'listStudy').mockResolvedValue({
      decks: [deck({ state: 'cancelled' })],
      quizzes: [],
    })
    vi.spyOn(api, 'getDeckStatus').mockResolvedValue(status({ state: 'cancelled' }))
    const { wrapper } = createWrapper()

    render(<StudySessionPage />, { wrapper })

    expect(await screen.findByText('This was cancelled')).toBeInTheDocument()
    const backLink = screen.getByRole('link', { name: /Back to study tools/ })
    expect(backLink).toHaveAttribute('href', '/classes/1?tab=study')
    expect(screen.queryByTestId('deck-session')).not.toBeInTheDocument()
  })

  it('ready deck renders DeckSession', async () => {
    vi.spyOn(api, 'listStudy').mockResolvedValue({
      decks: [deck({ state: 'ready' })],
      quizzes: [],
    })
    vi.spyOn(api, 'getDeckStatus').mockResolvedValue(status({ state: 'ready' }))
    vi.spyOn(api, 'getDeckSession').mockResolvedValue({ cards: [] })
    const { wrapper } = createWrapper()

    render(<StudySessionPage />, { wrapper })

    expect(await screen.findByTestId('deck-session')).toBeInTheDocument()
    expect(screen.getByTestId('deck-session')).toHaveTextContent('DeckSession for 8')
  })

  it('ready quiz renders QuizRunner', async () => {
    artifactId = '9'
    vi.spyOn(api, 'listStudy').mockResolvedValue({
      decks: [],
      quizzes: [quiz({ state: 'ready' })],
    })
    vi.spyOn(api, 'getQuizStatus').mockResolvedValue(status({ state: 'ready' }))
    vi.spyOn(api, 'getQuiz').mockResolvedValue({
      id: 9,
      class_id: 1,
      kind: 'quiz',
      title: 'Week 4 quiz',
      state: 'ready',
      stage_detail: null,
      problems_total: 10,
      problems_done: 10,
      error_message: null,
      created_at: '2026-08-05 09:00:00',
      updated_at: '2026-08-05 09:00:00',
      questions: [],
    })
    vi.spyOn(api, 'startAttempt').mockResolvedValue({
      attempt_id: 1,
      question_part_ids: [],
      question_count: 10,
      answers: [],
      finished: false,
    })
    const { wrapper } = createWrapper()

    render(<StudySessionPage />, { wrapper })

    expect(await screen.findByTestId('quiz-runner')).toBeInTheDocument()
    expect(screen.getByTestId('quiz-runner')).toHaveTextContent('QuizRunner for 1/9')
  })

  it('polling stops on terminal state (failed)', async () => {
    const getDeckStatus = vi
      .spyOn(api, 'getDeckStatus')
      .mockResolvedValueOnce(
        status({
          state: 'generating',
          stage_detail: 'Working',
          problems_total: 5,
          problems_done: 1,
        }),
      )
      .mockResolvedValue(status({ state: 'failed', error_message: 'Out of context' }))
    vi.spyOn(api, 'listStudy').mockResolvedValue({
      decks: [
        deck({
          state: 'generating',
          stage_detail: 'Working',
          problems_total: 5,
          problems_done: 1,
        }),
      ],
      quizzes: [],
    })
    const { wrapper } = createWrapper()

    render(<StudySessionPage />, { wrapper })

    // Initially shows the generating UI.
    expect(await screen.findByText('Writing')).toBeInTheDocument()

    // The poll transitions to failed; the component renders the failure alert.
    expect(await screen.findByText('Out of context')).toBeInTheDocument()
    expect(screen.getByText('Lyra could not finish writing this')).toBeInTheDocument()

    // The status was called at least twice (initial + one poll that returned failed).
    expect(getDeckStatus.mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it('deleted artifact shows not-found message', async () => {
    vi.spyOn(api, 'listStudy').mockResolvedValue({ decks: [], quizzes: [] })
    // Status endpoint should not be called with a real id for an artifact that does not exist.
    // The NaN guard in useDeckStatus/useQuizStatus keeps this disabled.
    const { wrapper } = createWrapper()

    render(<StudySessionPage />, { wrapper })

    expect(await screen.findByText('That study tool is not in this class')).toBeInTheDocument()
    const backLink = screen.getByRole('link', { name: /Back to study tools/ })
    expect(backLink).toHaveAttribute('href', '/classes/1?tab=study')
  })
})
