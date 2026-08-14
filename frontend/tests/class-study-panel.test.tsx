import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ClassStudyPanel } from '@/components/classes/class-study-panel'
import { api } from '@/lib/api'
import type { DeckSummary, StudyArtifactRead, StudyListRead } from '@/types'

const push = vi.fn()

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn(), push, prefetch: vi.fn() }),
  useParams: () => ({ id: '1' }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => '/classes/1',
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

beforeEach(() => {
  vi.restoreAllMocks()
  push.mockClear()
  // The create dialog and the quick-practice guard both read the documents.
  vi.spyOn(api, 'listDocuments').mockResolvedValue([])
})

describe('ClassStudyPanel', () => {
  it('shows a loading skeleton that matches the row layout', () => {
    // Never settled: the panel must hold its skeleton for as long as the list does.
    const { promise } = Promise.withResolvers<StudyListRead>()
    vi.spyOn(api, 'listStudy').mockReturnValue(promise)
    const { wrapper } = createWrapper()

    render(<ClassStudyPanel classId={1} />, { wrapper })

    expect(screen.getByLabelText('Loading study tools')).toBeInTheDocument()
  })

  it('leads with practice and keeps both custom creates when there is nothing yet', async () => {
    vi.spyOn(api, 'listStudy').mockResolvedValue({ decks: [], quizzes: [] })
    // A class can hold plenty of ready material and still have zero saved study sets, so
    // the empty state names what is actually empty: the artifacts, never the material.
    vi.spyOn(api, 'listDocuments').mockResolvedValue([
      { id: 3, class_id: 1, filename: 'notes.pdf', state: 'ready' },
    ] as never)
    const { wrapper } = createWrapper()

    render(<ClassStudyPanel classId={1} />, { wrapper })

    expect(await screen.findByText('No decks or quizzes yet')).toBeInTheDocument()
    expect(screen.queryByText(/Nothing to study from/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Practice now' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'New deck' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'New quiz' })).toBeInTheDocument()
  })

  it('holds Practice now until the document list has answered', async () => {
    vi.spyOn(api, 'listStudy').mockResolvedValue({ decks: [], quizzes: [] })
    // Never resolves: the ready count is unknown, which is not the same as zero.
    vi.spyOn(api, 'listDocuments').mockReturnValue(new Promise(() => {}))
    const createQuiz = vi.spyOn(api, 'createQuiz')
    const { wrapper } = createWrapper()

    render(<ClassStudyPanel classId={1} />, { wrapper })

    const practice = await screen.findByRole('button', { name: 'Practice now' })
    expect(practice).toBeDisabled()
    expect(createQuiz).not.toHaveBeenCalled()
  })

  it('starts a practice quiz in one click, at the defaults, named after the day', async () => {
    vi.spyOn(api, 'listStudy').mockResolvedValue({ decks: [], quizzes: [] })
    vi.spyOn(api, 'listDocuments').mockResolvedValue([
      { id: 3, class_id: 1, filename: 'notes.pdf', state: 'ready' },
    ] as never)
    const createQuiz = vi
      .spyOn(api, 'createQuiz')
      .mockResolvedValue(quiz({ id: 12, state: 'pending' }))
    const { wrapper } = createWrapper()

    render(<ClassStudyPanel classId={1} />, { wrapper })

    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Practice now' }))

    // No name asked for, no sources picked, no counts chosen: the backend's own defaults
    // carry the run, and the artifact page it lands on shows the progress.
    await waitFor(() =>
      expect(createQuiz).toHaveBeenCalledWith(1, { title: expect.stringMatching(/^Practice · /) }),
    )
    await waitFor(() => expect(push).toHaveBeenCalledWith('/classes/1/study/12'))
  })

  it('refuses quick practice plainly when nothing has finished processing', async () => {
    vi.spyOn(api, 'listStudy').mockResolvedValue({ decks: [], quizzes: [] })
    vi.spyOn(api, 'listDocuments').mockResolvedValue([])
    const createQuiz = vi.spyOn(api, 'createQuiz')
    const { wrapper } = createWrapper()

    render(<ClassStudyPanel classId={1} />, { wrapper })

    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Practice now' }))

    expect(createQuiz).not.toHaveBeenCalled()
  })

  it('lists decks with their bucket counts and a due badge, and quizzes with their size', async () => {
    vi.spyOn(api, 'listStudy').mockResolvedValue({
      decks: [deck({ due_count: 4 })],
      quizzes: [quiz({})],
    })
    const { wrapper } = createWrapper()

    render(<ClassStudyPanel classId={1} />, { wrapper })

    const deckLink = await screen.findByRole('link', { name: /Signals flashcards/ })
    expect(deckLink).toHaveAttribute('href', '/classes/1/study/8')
    expect(deckLink).toHaveTextContent('new 3 · learning 2 · mastered 1')
    expect(screen.getByText('4 due')).toBeInTheDocument()

    const quizLink = screen.getByRole('link', { name: /Week 4 quiz/ })
    expect(quizLink).toHaveAttribute('href', '/classes/1/study/9')
    expect(quizLink).toHaveTextContent('10 questions')
  })

  it('shows the stage line instead of counts while a deck is still generating', async () => {
    vi.spyOn(api, 'listStudy').mockResolvedValue({
      decks: [
        deck({
          state: 'generating',
          stage_detail: 'Writing cards for Linear systems',
          problems_total: null,
          problems_done: 0,
        }),
      ],
      quizzes: [],
    })
    const { wrapper } = createWrapper()

    render(<ClassStudyPanel classId={1} />, { wrapper })

    const deckLink = await screen.findByRole('link', { name: /Signals flashcards/ })
    expect(deckLink).toHaveTextContent('Writing cards for Linear systems')
    expect(deckLink).toHaveTextContent('Generating')
    expect(screen.queryByText(/due/)).not.toBeInTheDocument()
  })
})
