import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ClassStudyPanel } from '@/components/classes/class-study-panel'
import { api } from '@/lib/api'
import { quickStudyTitle } from '@/lib/handoff'
import type { DeckSummary, StudyArtifactRead, StudyListRead } from '@/types'

const push = vi.fn()

vi.mock('@/router/hooks', () => ({
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
  sessionStorage.clear()
  // The create dialog and the quick-practice guard both read the documents.
  vi.spyOn(api, 'listDocuments').mockResolvedValue([])
})

afterEach(() => {
  // The same-minute test fakes Date; nothing may leak a frozen clock into the next test.
  vi.useRealTimers()
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
    expect(screen.getByRole('button', { name: 'New quiz' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'New deck' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Choose quiz sources' })).toBeInTheDocument()
  })

  it('holds New quiz until the document list has answered', async () => {
    vi.spyOn(api, 'listStudy').mockResolvedValue({ decks: [], quizzes: [] })
    // Never resolves: the ready count is unknown, which is not the same as zero.
    vi.spyOn(api, 'listDocuments').mockReturnValue(new Promise(() => {}))
    const createQuiz = vi.spyOn(api, 'createQuiz')
    const { wrapper } = createWrapper()

    render(<ClassStudyPanel classId={1} />, { wrapper })

    const practice = await screen.findByRole('button', { name: 'New quiz' })
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
    await user.click(await screen.findByRole('button', { name: 'New quiz' }))

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
    await user.click(await screen.findByRole('button', { name: 'New quiz' }))

    expect(createQuiz).not.toHaveBeenCalled()
  })

  it('explains a failed document list instead of leaving Practice silently dead', async () => {
    // The saved decks and quizzes load from their own query and stay fully usable; only
    // Practice depends on knowing which documents are ready.
    vi.spyOn(api, 'listStudy').mockResolvedValue({ decks: [deck({})], quizzes: [] })
    const listDocuments = vi.spyOn(api, 'listDocuments').mockRejectedValue(new Error('offline'))
    const { wrapper } = createWrapper()

    render(<ClassStudyPanel classId={1} />, { wrapper })

    expect(await screen.findByRole('link', { name: /Signals flashcards/ })).toBeInTheDocument()
    expect(await screen.findByText(/The document list did not load/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'New quiz' })).toBeDisabled()

    // Retry asks again; recovery re-enables Practice and retires the notice.
    listDocuments.mockResolvedValue([
      { id: 3, class_id: 1, filename: 'notes.pdf', state: 'ready' },
    ] as never)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'New quiz' })).toBeEnabled())
    expect(screen.queryByText(/The document list did not load/)).not.toBeInTheDocument()
  })

  it('numbers a second quick practice started inside the same minute', async () => {
    // Only Date is faked, so timers, polling, and userEvent still run for real.
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date(2026, 7, 14, 16, 18))
    const taken = quickStudyTitle('quiz')
    vi.spyOn(api, 'listStudy').mockResolvedValue({ decks: [], quizzes: [quiz({ title: taken })] })
    vi.spyOn(api, 'listDocuments').mockResolvedValue([
      { id: 3, class_id: 1, filename: 'notes.pdf', state: 'ready' },
    ] as never)
    const createQuiz = vi
      .spyOn(api, 'createQuiz')
      .mockResolvedValue(quiz({ id: 13, state: 'pending' }))
    const { wrapper } = createWrapper()

    render(<ClassStudyPanel classId={1} />, { wrapper })

    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'New quiz' }))

    await waitFor(() => expect(createQuiz).toHaveBeenCalledWith(1, { title: `${taken} · 2` }))
  })

  it('collapses Options again when the create dialog is reopened', async () => {
    vi.spyOn(api, 'listStudy').mockResolvedValue({ decks: [], quizzes: [] })
    vi.spyOn(api, 'listDocuments').mockResolvedValue([
      { id: 3, class_id: 1, filename: 'notes.pdf', state: 'ready' },
    ] as never)
    const { wrapper } = createWrapper()

    render(<ClassStudyPanel classId={1} />, { wrapper })

    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Choose quiz sources' }))
    await user.click(screen.getByRole('button', { name: /Options/ }))
    expect(screen.getByLabelText('Questions')).toBeVisible()

    // Close and reopen: the disclosure starts closed again, like the rest of the form.
    // Options left standing open would make the second opening a different dialog than
    // the first, which is the opposite of what a default is for.
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await user.click(screen.getByRole('button', { name: 'Choose quiz sources' }))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(screen.queryByLabelText('Questions')).not.toBeInTheDocument()
  })

  it('lists decks with their bucket counts and a due badge, and quizzes with their size', async () => {
    vi.spyOn(api, 'listStudy').mockResolvedValue({
      decks: [deck({ due_count: 4 })],
      quizzes: [quiz({})],
    })
    const { wrapper } = createWrapper()

    render(<ClassStudyPanel classId={1} />, { wrapper })

    const deckLink = await screen.findByRole('link', { name: /Signals flashcards/ })
    expect(deckLink).toHaveAttribute('href', '/#/classes/1/study/8?review=due')
    expect(deckLink).toHaveTextContent('12 cards')
    expect(deckLink).not.toHaveTextContent('mastered')
    expect(screen.getByText('Review due · 4')).toBeInTheDocument()

    const quizLink = screen.getByRole('link', { name: /Week 4 quiz/ })
    expect(quizLink).toHaveAttribute('href', '/#/classes/1/study/9')
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

it('retains raw quiz counts and rejects invalid whole-number counts', async () => {
  vi.spyOn(api, 'listStudy').mockResolvedValue({ decks: [], quizzes: [] })
  vi.spyOn(api, 'listDocuments').mockResolvedValue([
    { id: 3, class_id: 1, filename: 'notes.pdf', state: 'ready' },
  ] as never)
  const create = vi.spyOn(api, 'createQuiz').mockResolvedValue(quiz({}))
  render(<ClassStudyPanel classId={1} />, { wrapper: createWrapper().wrapper })
  await userEvent.click(await screen.findByRole('button', { name: 'Choose quiz sources' }))
  await userEvent.click(screen.getByRole('button', { name: /Options/ }))
  const count = screen.getByLabelText('Questions')
  for (const value of ['', '0', '31', '3.5']) {
    await userEvent.clear(count)
    if (value) await userEvent.type(count, value)
    expect(screen.getByText('Enter a whole number from 3 to 30.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create quiz' })).toBeDisabled()
  }
  await userEvent.clear(count)
  await userEvent.type(count, '12')
  await userEvent.click(screen.getByRole('button', { name: 'Create quiz' }))
  await waitFor(() =>
    expect(create).toHaveBeenCalledWith(1, expect.objectContaining({ count: 12 })),
  )
})

it('retries source loading in the open creation dialog without losing options', async () => {
  vi.spyOn(api, 'listStudy').mockResolvedValue({ decks: [], quizzes: [] })
  const documents = vi.spyOn(api, 'listDocuments').mockRejectedValue(new Error('offline'))
  render(<ClassStudyPanel classId={1} />, { wrapper: createWrapper().wrapper })
  await userEvent.click(await screen.findByRole('button', { name: 'Choose quiz sources' }))
  await userEvent.click(screen.getByRole('button', { name: /Options/ }))
  await userEvent.clear(screen.getByLabelText('Questions'))
  await userEvent.type(screen.getByLabelText('Questions'), '14')
  await userEvent.clear(screen.getByLabelText('Name'))
  await userEvent.type(screen.getByLabelText('Name'), 'Retained quiz')
  documents.mockResolvedValue([
    { id: 3, class_id: 1, filename: 'notes.pdf', state: 'ready' },
  ] as never)
  await userEvent.click(screen.getByRole('button', { name: 'Retry documents' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Create quiz' })).toBeEnabled())
  expect(screen.getByLabelText('Questions')).toHaveValue(14)
  expect(screen.getByLabelText('Name')).toHaveValue('Retained quiz')
})

it('finds old practice by title with bounded history and retains the search on return', async () => {
  vi.spyOn(api, 'listStudy').mockResolvedValue({
    decks: [],
    quizzes: Array.from({ length: 105 }, (_, index) =>
      quiz({ id: index + 100, title: `Similar long semester practice title ${index}` }),
    ),
  })
  const first = render(<ClassStudyPanel classId={1} />, { wrapper: createWrapper().wrapper })
  const search = await screen.findByRole('searchbox', { name: 'Search practice by title' })
  expect(screen.getAllByRole('link')).toHaveLength(20)
  await userEvent.type(search, '104')
  expect(screen.getAllByRole('link')).toHaveLength(1)
  expect(screen.getByRole('link', { name: /title 104/ })).toBeVisible()
  first.unmount()
  render(<ClassStudyPanel classId={1} />, { wrapper: createWrapper().wrapper })
  expect(await screen.findByRole('searchbox')).toHaveValue('104')
  await userEvent.clear(screen.getByRole('searchbox'))
  await userEvent.type(screen.getByRole('searchbox'), 'unmatched')
  expect(screen.getByRole('status')).toHaveTextContent('No practice matches this search.')
  expect(screen.queryByText('No decks or quizzes yet')).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Clear search' }))
  expect(screen.getAllByRole('link')).toHaveLength(20)
  await userEvent.click(screen.getByRole('button', { name: /Show more practice/ }))
  expect(screen.getAllByRole('link')).toHaveLength(40)
})

it('gates a new quiz before a missing-material attempt while preserving custom source selection', async () => {
  vi.spyOn(api, 'listStudy').mockResolvedValue({ decks: [], quizzes: [] })
  render(<ClassStudyPanel classId={1} />, { wrapper: createWrapper().wrapper })
  expect(await screen.findByRole('button', { name: 'New quiz' })).toBeDisabled()
  expect(screen.getByText(/Add a document in Files/)).toBeVisible()
  expect(screen.getByRole('button', { name: 'Choose quiz sources' })).toBeEnabled()
})

it('continues only a real active attempt and never treats generation as answered work', async () => {
  vi.spyOn(api, 'listStudy').mockResolvedValue({
    decks: [],
    quizzes: [quiz({ active_attempt_id: 31, answered_count: 3 })],
  })
  render(<ClassStudyPanel classId={1} />, { wrapper: createWrapper().wrapper })
  const continued = await screen.findByRole('button', { name: 'Continue quiz' })
  expect(screen.getByRole('link', { name: /Week 4 quiz/ })).toHaveTextContent('3 answered')
  await userEvent.click(continued)
  expect(push).toHaveBeenCalledWith('/classes/1/study/9')
})

it('keeps cached practice visible during failed refresh and pending Retry', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['study', 1], { decks: [deck({})], quizzes: [] })
  const request = vi.spyOn(api, 'listStudy').mockRejectedValue(new Error('offline'))
  render(
    <QueryClientProvider client={client}>
      <ClassStudyPanel classId={1} />
    </QueryClientProvider>,
  )
  await screen.findByText('Could not refresh your study tools')
  expect(screen.getByRole('link', { name: /Signals flashcards/ })).toBeVisible()
  request.mockReturnValue(new Promise(() => {}))
  await userEvent.click(screen.getByRole('button', { name: 'Retry study tools' }))
  expect(await screen.findByRole('button', { name: 'Retrying…' })).toBeDisabled()
  expect(screen.getByRole('link', { name: /Signals flashcards/ })).toBeVisible()
})
