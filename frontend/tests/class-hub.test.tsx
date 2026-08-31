import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ClassHub, readHubTab } from '@/components/classes/class-hub'
import { api } from '@/lib/api'
import type { ClassProfile, ClassRead, DocumentRead, SessionRead, SolutionRead } from '@/types'

const replace = vi.fn()
const push = vi.fn()

vi.mock('@/router/hooks', () => ({
  useRouter: () => ({ replace, push, prefetch: vi.fn() }),
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

const KLASS = {
  id: 1,
  name: 'Continuous-Time Signals',
  code: 'ECE 203',
  semester: 'Spring 2026',
  archived: false,
  document_count: 2,
  created_at: '2026-01-05 09:00:00',
  last_active_at: '2026-08-05 09:00:00',
} as ClassRead

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(api, 'getClass').mockResolvedValue(KLASS)
  vi.spyOn(api, 'listSessions').mockResolvedValue([
    { id: 4, class_id: 1, title: 'Fourier week', created_at: '2026-08-04 09:00:00' },
  ] as SessionRead[])
  vi.spyOn(api, 'listSolutions').mockResolvedValue([
    {
      id: 8,
      class_id: 1,
      title: 'Homework 2',
      state: 'ready',
      problems_total: 4,
      problems_done: 4,
      sources: [{ document_id: 3, filename: 'homework_2.pdf', role: 'problem_set' }],
      updated_at: '2026-08-05 08:00:00',
    },
  ] as SolutionRead[])
  vi.spyOn(api, 'listDocuments').mockResolvedValue([
    { id: 3, class_id: 1, filename: 'homework_2.pdf', state: 'ready' },
    { id: 5, class_id: 1, filename: 'syllabus.pdf', state: 'failed' },
  ] as DocumentRead[])
  vi.spyOn(api, 'listStudy').mockResolvedValue({ decks: [], quizzes: [] })
  vi.spyOn(api, 'listDrafts').mockResolvedValue([])
  vi.spyOn(api, 'getClassProfile').mockResolvedValue({
    facts: [],
    extraction_skipped_reason: null,
  } as ClassProfile)
})

describe('readHubTab', () => {
  it('falls back to the overview for anything it does not recognize', () => {
    expect(readHubTab('documents')).toBe('documents')
    expect(readHubTab(null)).toBe('overview')
    // A hand-edited or stale URL should land somewhere real rather than on a blank panel.
    expect(readHubTab('nonsense')).toBe('overview')
  })
})

describe('ClassHub', () => {
  it('names the class and every section it holds', async () => {
    const { wrapper } = createWrapper()

    render(<ClassHub classId={1} tab="overview" />, { wrapper })

    expect(await screen.findByRole('heading', { name: 'Continuous-Time Signals' })).toBeVisible()
    for (const name of ['Overview', 'Chats', 'Solutions', 'Study', 'Documents', 'Profile']) {
      expect(screen.getByRole('tab', { name: new RegExp(`^${name}`) })).toBeInTheDocument()
    }
  })

  it('shows what is in the class on its tabs, so the counts are readable at a glance', async () => {
    const { wrapper } = createWrapper()

    render(<ClassHub classId={1} tab="overview" />, { wrapper })

    expect(await screen.findByRole('tab', { name: 'Documents 2' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Chats 1' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Solutions 1' })).toBeInTheDocument()
    // Every collection tab counts once its data has loaded, zero included, so the strip is
    // consistent rather than counting only the tabs that happen to be non-empty (ui-overhaul
    // 3.2). Overview is a synthesis, not a collection, so it carries no count.
    expect(screen.getByRole('tab', { name: 'Profile 0' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Overview' })).toBeInTheDocument()
  })

  it('opens on a continuation surface: ask, resume, and the ways to start', async () => {
    const { wrapper } = createWrapper()

    render(<ClassHub classId={1} tab="overview" />, { wrapper })

    // The ask box is the front door: a question typed here lands in a conversation.
    expect(
      await screen.findByRole('textbox', { name: 'Ask about Continuous-Time Signals' }),
    ).toBeInTheDocument()

    // The most recent work links straight back into itself, whatever kind it is.
    expect(await screen.findByRole('link', { name: /Fourier week/ })).toHaveAttribute(
      'href',
      '/#/classes/1/chat?session=4',
    )
    expect(screen.getByRole('link', { name: /Homework 2/ })).toHaveAttribute(
      'href',
      '/#/classes/1/solutions/8',
    )

    // The fixture's syllabus.pdf failed ingestion, and a failure is a continuation item:
    // it is the thing most worth the student's next click.
    expect(screen.getByRole('link', { name: /One document could not be used/ })).toHaveAttribute(
      'href',
      '/#/classes/1?tab=documents',
    )
    expect(screen.getByText('Needs attention')).toBeInTheDocument()

    // The common starts are verbs, not feature cards, and each goes somewhere real.
    expect(screen.getByRole('button', { name: 'Practice this material' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Solve a problem set' })).toHaveAttribute(
      'href',
      '/#/classes/1/solutions/new',
    )
    expect(screen.getByRole('button', { name: 'Start writing' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Add documents' })).toHaveAttribute(
      'href',
      '/#/classes/1?tab=documents',
    )
  })

  it('distinguishes an empty class from one whose documents are working or broken', async () => {
    const { wrapper } = createWrapper()

    // Failed-only: the class is not "empty", it is broken, and must say so.
    vi.spyOn(api, 'listDocuments').mockResolvedValue([
      { id: 5, class_id: 1, filename: 'syllabus.pdf', state: 'failed' },
      { id: 6, class_id: 1, filename: 'notes.pdf', state: 'failed' },
    ] as DocumentRead[])
    const failedView = render(<ClassHub classId={1} tab="overview" />, { wrapper })
    expect(
      await screen.findByRole('link', { name: /2 documents could not be used/ }),
    ).toBeInTheDocument()
    expect(screen.queryByText(/Nothing uploaded yet/)).not.toBeInTheDocument()
    failedView.unmount()

    // Unsupported-only: nothing crashed, nothing is coming, and nothing here is usable.
    // Terminal-but-unusable is attention, not emptiness, whichever way it got there.
    vi.spyOn(api, 'listDocuments').mockResolvedValue([
      { id: 8, class_id: 1, filename: 'lecture.key', state: 'unsupported' },
    ] as DocumentRead[])
    const unsupportedView = render(<ClassHub classId={1} tab="overview" />, {
      wrapper: createWrapper().wrapper,
    })
    expect(
      await screen.findByRole('link', { name: /One document could not be used/ }),
    ).toHaveAttribute('href', '/#/classes/1?tab=documents')
    expect(screen.getByText('Needs attention')).toBeInTheDocument()
    expect(screen.queryByText(/Nothing uploaded yet/)).not.toBeInTheDocument()
    unsupportedView.unmount()

    // Mixed: one failed plus one unsupported is one aggregate row counting both, not
    // two rows or a count that quietly drops the unsupported file.
    vi.spyOn(api, 'listDocuments').mockResolvedValue([
      { id: 5, class_id: 1, filename: 'syllabus.pdf', state: 'failed' },
      { id: 8, class_id: 1, filename: 'lecture.key', state: 'unsupported' },
    ] as DocumentRead[])
    const mixedView = render(<ClassHub classId={1} tab="overview" />, {
      wrapper: createWrapper().wrapper,
    })
    expect(
      await screen.findByRole('link', { name: /2 documents could not be used/ }),
    ).toBeInTheDocument()
    mixedView.unmount()

    // Still processing: working, not empty.
    vi.spyOn(api, 'listDocuments').mockResolvedValue([
      { id: 7, class_id: 1, filename: 'slides.pdf', state: 'embedding' },
    ] as DocumentRead[])
    const workingView = render(<ClassHub classId={1} tab="overview" />, {
      wrapper: createWrapper().wrapper,
    })
    expect(await screen.findByRole('link', { name: /One document being read/ })).toBeInTheDocument()
    expect(screen.queryByText(/Nothing uploaded yet/)).not.toBeInTheDocument()
    workingView.unmount()

    // Truly empty: only now does the empty copy appear.
    vi.spyOn(api, 'listDocuments').mockResolvedValue([])
    render(<ClassHub classId={1} tab="overview" />, { wrapper: createWrapper().wrapper })
    expect(await screen.findByText(/Nothing uploaded yet/)).toBeInTheDocument()
  })

  it('holds Practice until the document list has loaded', async () => {
    // A pending query means the ready count is unknown, not zero: the button must not be
    // clickable into a wrong "nothing ready" answer.
    vi.spyOn(api, 'listDocuments').mockReturnValue(new Promise(() => {}))
    const { wrapper } = createWrapper()

    render(<ClassHub classId={1} tab="overview" />, { wrapper })

    const practice = await screen.findByRole('button', { name: 'Practice this material' })
    expect(practice).toBeDisabled()
  })

  it('says when the document list itself failed, and retries it on request', async () => {
    // A failed query is not a loading one: Practice stays held either way, but held
    // forever with no reason on screen would read as a broken button.
    const listDocuments = vi.spyOn(api, 'listDocuments').mockRejectedValue(new Error('offline'))
    const { wrapper } = createWrapper()

    render(<ClassHub classId={1} tab="overview" />, { wrapper })

    expect(await screen.findByText(/The document list did not load/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Practice this material' })).toBeDisabled()
    // Not knowing what is uploaded is not the same as knowing nothing is.
    expect(screen.queryByText(/Nothing uploaded yet/)).not.toBeInTheDocument()

    // Retry asks again; a recovered backend re-enables Practice and retires the notice.
    listDocuments.mockResolvedValue([
      { id: 3, class_id: 1, filename: 'homework_2.pdf', state: 'ready' },
    ] as DocumentRead[])
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Practice this material' })).toBeEnabled(),
    )
    expect(screen.queryByText(/The document list did not load/)).not.toBeInTheDocument()
  })

  it('sends a typed question into a new conversation, words and all', async () => {
    const { wrapper } = createWrapper()

    render(<ClassHub classId={1} tab="overview" />, { wrapper })

    const user = userEvent.setup()
    const box = await screen.findByRole('textbox', { name: 'Ask about Continuous-Time Signals' })
    await user.type(box, 'Why does convolution flip the signal?')
    await user.click(screen.getByRole('button', { name: 'Ask' }))

    expect(push).toHaveBeenCalledWith(
      '/classes/1/chat?session=new&ask=Why+does+convolution+flip+the+signal%3F&send=1',
    )
  })

  it('opens a new conversation at the chat route rather than at the class itself', async () => {
    const { wrapper } = createWrapper()

    render(<ClassHub classId={1} tab="overview" />, { wrapper })

    const links = await screen.findAllByRole('link', { name: /New chat/ })
    for (const link of links) expect(link).toHaveAttribute('href', '/#/classes/1/chat?session=new')
  })
})
