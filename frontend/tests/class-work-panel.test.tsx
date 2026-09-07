import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ClassWorkPanel } from '@/components/classes/class-work-panel'
import { api } from '@/lib/api'
import { RouterProvider } from '@/router/hooks'
import type { DeckSummary, DraftRead, SessionRead, SolutionRead, StudyArtifactRead } from '@/types'

function resetLocation(url: string) {
  window.history.replaceState({}, '', url)
}

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <RouterProvider>{children}</RouterProvider>
    </QueryClientProvider>
  )
  return { wrapper }
}

const SESSION = {
  id: 4,
  class_id: 1,
  title: 'Fourier week',
  created_at: '2026-08-04 09:00:00',
} as SessionRead

const SOLUTION = {
  id: 8,
  class_id: 1,
  title: 'Homework 2',
  state: 'ready',
  updated_at: '2026-08-05 08:00:00',
  sources: [{ document_id: 3, filename: 'homework_2.pdf', role: 'problem_set' }],
} as SolutionRead

const DRAFT = {
  id: 12,
  class_id: 1,
  title: 'Signal notes draft',
  state: 'ready',
  updated_at: '2026-08-03 17:00:00',
} as DraftRead

const DECK = {
  id: 21,
  class_id: 1,
  kind: 'flashcard_deck',
  title: 'Laplace transforms',
  state: 'ready',
  stage_detail: null,
  problems_total: null,
  problems_done: 0,
  error_message: null,
  created_at: '2026-08-01 10:00:00',
  updated_at: '2026-08-06 09:00:00',
  cards_total: 20,
  due_count: 3,
  buckets: { new: 17, learning: 2, mastered: 1 },
} as DeckSummary

const QUIZ = {
  id: 22,
  class_id: 1,
  kind: 'quiz',
  title: 'Week 5 quiz',
  state: 'ready',
  stage_detail: null,
  problems_total: 5,
  problems_done: 2,
  error_message: null,
  created_at: '2026-08-01 11:00:00',
  updated_at: '2026-08-02 11:00:00',
} as StudyArtifactRead

beforeEach(() => {
  vi.restoreAllMocks()
  sessionStorage.clear()
  vi.spyOn(api, 'listSessions').mockResolvedValue([SESSION])
  vi.spyOn(api, 'listSolutions').mockResolvedValue([SOLUTION])
  vi.spyOn(api, 'listDrafts').mockResolvedValue([DRAFT])
  vi.spyOn(api, 'listStudy').mockResolvedValue({ decks: [DECK], quizzes: [QUIZ] })
})

describe('ClassWorkPanel filters', () => {
  it('keeps each filter inside the hash route, updating URL and list together', async () => {
    resetLocation('/#/classes/1?tab=work')
    const { wrapper } = createWrapper()

    render(<ClassWorkPanel classId={1} />, { wrapper })
    const user = userEvent.setup()

    // The unfiltered list is the getting-back-to-it view: every kind, most recent first,
    // including the study artifacts the panel owns a row for.
    await screen.findByRole('link', { name: /Laplace transforms/ })
    expect(await screen.findByRole('link', { name: /Fourier week/ })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Homework 2/ })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Signal notes draft/ })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Week 5 quiz/ })).toBeInTheDocument()

    // A filter click moves the `work` param into the actual hash route - not into the
    // document's query string, which the router never reads - and the list hands over.
    await user.click(screen.getByRole('button', { name: 'Solutions' }))
    await waitFor(() => expect(window.location.hash).toBe('#/classes/1?tab=work&work=solutions'))
    expect(await screen.findByRole('link', { name: /Homework 2/ })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Fourier week/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Signal notes draft/ })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Drafts' }))
    await waitFor(() => expect(window.location.hash).toBe('#/classes/1?tab=work&work=drafts'))
    expect(await screen.findByRole('link', { name: /Signal notes draft/ })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Homework 2/ })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Chats' }))
    await waitFor(() => expect(window.location.hash).toBe('#/classes/1?tab=work&work=chats'))
    expect(await screen.findByRole('link', { name: /Fourier week/ })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Homework 2/ })).not.toBeInTheDocument()

    // "All" drops the param: the route says what the list shows.
    await user.click(screen.getByRole('button', { name: 'All' }))
    await waitFor(() => expect(window.location.hash).toBe('#/classes/1?tab=work'))
    expect(await screen.findByRole('link', { name: /Laplace transforms/ })).toBeInTheDocument()
  })

  it('lets keyboard users activate ordinary pressed filter buttons', async () => {
    resetLocation('/#/classes/1?tab=work')
    const { wrapper } = createWrapper()
    render(<ClassWorkPanel classId={1} />, { wrapper })
    await screen.findByRole('link', { name: /Fourier week/ })
    const user = userEvent.setup()
    await user.tab()
    expect(screen.getByRole('button', { name: 'All' })).toHaveFocus()
    await user.tab()
    await user.keyboard(' ')
    expect(screen.getByRole('button', { name: 'Chats' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument()
  })

  it('reads a filtered URL on arrival, so a reload of the filtered view lands filtered', async () => {
    resetLocation('/#/classes/1?tab=work&work=chats')
    const { wrapper } = createWrapper()

    render(<ClassWorkPanel classId={1} />, { wrapper })

    await screen.findByRole('link', { name: /Fourier week/ })
    expect(screen.getByRole('button', { name: 'Chats' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.queryByRole('link', { name: /Homework 2/ })).not.toBeInTheDocument()
  })

  it('defaults a legacy ?tab=chats view to the chats list before the page rewrites it', async () => {
    resetLocation('/#/classes/1?tab=chats')
    const { wrapper } = createWrapper()

    render(<ClassWorkPanel classId={1} />, { wrapper })

    await screen.findByRole('link', { name: /Fourier week/ })
    expect(screen.queryByRole('link', { name: /Laplace transforms/ })).not.toBeInTheDocument()
  })
})

describe('Work recovery and readable states', () => {
  it('keeps loaded categories visible and retries a failed category without claiming it is empty', async () => {
    resetLocation('/#/classes/1?tab=work')
    vi.mocked(api.listSolutions).mockRejectedValueOnce(new Error('offline'))
    const { wrapper } = createWrapper()
    render(<ClassWorkPanel classId={1} />, { wrapper })
    expect(await screen.findByRole('link', { name: /Fourier week/ })).toBeInTheDocument()
    expect(await screen.findByText('Some work could not be refreshed')).toBeInTheDocument()
    expect(screen.queryByText('Nothing here yet')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Retry all' }))
    expect(await screen.findByRole('link', { name: /Homework 2/ })).toBeInTheDocument()
    expect(screen.queryByText('Some work could not be refreshed')).not.toBeInTheDocument()
  })

  it('shows a recovery state when every successful category is empty', async () => {
    resetLocation('/#/classes/1?tab=work')
    vi.mocked(api.listSessions).mockResolvedValue([])
    vi.mocked(api.listDrafts).mockResolvedValue([])
    vi.mocked(api.listStudy).mockResolvedValue({ decks: [], quizzes: [] })
    vi.mocked(api.listSolutions).mockRejectedValue(new Error('offline'))
    const { wrapper } = createWrapper()
    render(<ClassWorkPanel classId={1} />, { wrapper })
    await screen.findByText('Some work could not be refreshed')
    expect(screen.queryByText('Nothing here yet')).not.toBeInTheDocument()
  })

  it('offers understandable actions for review and queued work', async () => {
    resetLocation('/#/classes/1?tab=work')
    vi.mocked(api.listSolutions).mockResolvedValue([
      { ...SOLUTION, state: 'awaiting_review', stage_detail: null },
    ])
    vi.mocked(api.listDrafts).mockResolvedValue([
      { ...DRAFT, state: 'pending', stage_detail: null },
    ])
    const { wrapper } = createWrapper()
    render(<ClassWorkPanel classId={1} />, { wrapper })
    expect(
      await screen.findByRole('link', { name: /Homework 2.*Review problems/ }),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Signal notes draft.*Queued/ })).toBeInTheDocument()
    expect(screen.queryByText('awaiting_review')).not.toBeInTheDocument()
  })
})

it('finds old work by title without expanding the whole inventory and restores its query', async () => {
  resetLocation('/#/classes/1?tab=work')
  vi.mocked(api.listSessions).mockResolvedValue(
    Array.from({ length: 105 }, (_, index) => ({
      ...SESSION,
      id: index + 100,
      title: `Near-identical lecture conversation ${index}`,
    })),
  )
  const first = render(<ClassWorkPanel classId={1} />, { wrapper: createWrapper().wrapper })
  const search = await screen.findByRole('searchbox', { name: 'Search work by title' })
  expect(screen.getAllByRole('link')).toHaveLength(20)
  await userEvent.type(search, '104')
  expect(screen.getAllByRole('link')).toHaveLength(1)
  expect(screen.getByRole('link', { name: /conversation 104/ })).toBeVisible()
  first.unmount()
  render(<ClassWorkPanel classId={1} />, { wrapper: createWrapper().wrapper })
  expect(await screen.findByRole('searchbox')).toHaveValue('104')
  await userEvent.type(screen.getByRole('searchbox'), 'missing')
  expect(screen.getByRole('status')).toHaveTextContent('No work matches this search.')
  expect(screen.queryByText('Nothing here yet')).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Clear search' }))
  expect(screen.getAllByRole('link')).toHaveLength(20)
  await userEvent.click(screen.getByRole('button', { name: /Show more work/ }))
  expect(screen.getAllByRole('link')).toHaveLength(40)
})

it('does not label generated quiz questions as student answers', async () => {
  resetLocation('/#/classes/1?tab=work')
  render(<ClassWorkPanel classId={1} />, { wrapper: createWrapper().wrapper })
  const quiz = await screen.findByRole('link', { name: /Week 5 quiz/ })
  expect(quiz).not.toHaveTextContent('answered')
})

it.each(['Chats', 'Solutions', 'Drafts'])(
  'keeps search, bounded history and management available in %s',
  async (filter) => {
    resetLocation('/#/classes/1?tab=work')
    vi.mocked(api.listSessions).mockResolvedValue(
      Array.from({ length: 105 }, (_, i) => ({
        ...SESSION,
        id: i + 100,
        title: `Similar old work title ${i}`,
      })),
    )
    vi.mocked(api.listSolutions).mockResolvedValue(
      Array.from({ length: 105 }, (_, i) => ({
        ...SOLUTION,
        id: i + 300,
        title: `Similar old work title ${i}`,
      })),
    )
    vi.mocked(api.listDrafts).mockResolvedValue(
      Array.from({ length: 105 }, (_, i) => ({
        ...DRAFT,
        id: i + 500,
        title: `Similar old work title ${i}`,
      })),
    )
    render(<ClassWorkPanel classId={1} />, { wrapper: createWrapper().wrapper })
    await screen.findByRole('searchbox')
    await userEvent.click(screen.getByRole('button', { name: filter }))
    expect(screen.getAllByRole('listitem')).toHaveLength(20)
    await userEvent.type(screen.getByRole('searchbox'), '104')
    expect(screen.getAllByRole('listitem')).toHaveLength(1)
    expect(screen.getByRole('link', { name: /Similar old work title 104/ })).toBeVisible()
    await userEvent.click(
      screen.getByRole('button', { name: /Actions for Similar old work title 104/ }),
    )
    expect(screen.getByRole('menuitem', { name: 'Rename' })).toBeVisible()
    expect(screen.getByRole('menuitem', { name: 'Delete' })).toBeVisible()
    await userEvent.keyboard('{Escape}')
    await userEvent.type(screen.getByRole('searchbox'), 'missing')
    expect(screen.getByRole('status')).toHaveTextContent('No work matches this search.')
    await userEvent.click(screen.getByRole('button', { name: 'Clear search' }))
    expect(screen.getAllByRole('listitem')).toHaveLength(20)
    await userEvent.click(screen.getByRole('button', { name: /Show more work/ }))
    expect(screen.getAllByRole('listitem')).toHaveLength(40)
  },
)

it('combines unavailable categories into one recovery notice and retries all visibly', async () => {
  resetLocation('/#/classes/1?tab=work')
  vi.mocked(api.listSolutions).mockRejectedValue(new Error('offline'))
  vi.mocked(api.listDrafts).mockRejectedValue(new Error('offline'))
  render(<ClassWorkPanel classId={1} />, { wrapper: createWrapper().wrapper })
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not load solutions, drafts.')
  expect(screen.getAllByRole('alert')).toHaveLength(1)
  vi.mocked(api.listSolutions).mockReturnValue(new Promise(() => {}))
  vi.mocked(api.listDrafts).mockReturnValue(new Promise(() => {}))
  await userEvent.click(screen.getByRole('button', { name: 'Retry all' }))
  expect(await screen.findByRole('button', { name: 'Retrying…' })).toBeDisabled()
  expect(screen.getByRole('link', { name: /Fourier week/ })).toBeVisible()
})
