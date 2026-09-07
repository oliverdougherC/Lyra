import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ClassHub, readHubTab } from '@/components/classes/class-hub'
import { api } from '@/lib/api'
import type {
  ClassProfile,
  ClassRead,
  DocumentRead,
  FactRead,
  SessionRead,
  SolutionRead,
  SettingsRead,
} from '@/types'

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

/** A fact with only the fields the hub's count and the sheet care about. */
function fact(
  id: number,
  confidence: 'high' | 'low',
  confirmed: boolean,
  rejected = false,
): FactRead {
  return {
    id,
    class_id: 1,
    kind: 'topic',
    label: `Fact ${id}`,
    value: `Value ${id}`,
    confidence,
    confirmed,
    rejected,
    edited: false,
    source_document_id: null,
    source_filename: null,
    sources: [],
    source_writer_id: null,
    source_excerpt_id: null,
    source_title: null,
    source_url: null,
    created_at: '2026-01-05 09:00:00',
  } as FactRead
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(api, 'getSettings').mockResolvedValue({
    endpoint_url: 'http://localhost:8080/v1',
    model: 'tutor',
  } as SettingsRead)
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
  it('falls back to the front door for anything it does not recognize', () => {
    expect(readHubTab('files')).toBe('files')
    expect(readHubTab(null)).toBe('ask')
    // A hand-edited or stale URL should land somewhere real rather than on a blank panel.
    expect(readHubTab('nonsense')).toBe('ask')
  })

  it('lands a seven-tab-era bookmark on the task that owns the same view now', () => {
    expect(readHubTab('overview')).toBe('ask')
    expect(readHubTab('chats')).toBe('work')
    expect(readHubTab('solutions')).toBe('work')
    expect(readHubTab('study')).toBe('practice')
    expect(readHubTab('drafts')).toBe('work')
    expect(readHubTab('documents')).toBe('files')
    expect(readHubTab('profile')).toBe('ask')
  })
})

describe('ClassHub', () => {
  it('names the class and every task it offers', async () => {
    const { wrapper } = createWrapper()

    render(<ClassHub classId={1} tab="ask" />, { wrapper })

    expect(await screen.findByRole('heading', { name: 'Continuous-Time Signals' })).toBeVisible()
    for (const name of ['Ask', 'Practice', 'Work', 'Files']) {
      expect(screen.getByRole('tab', { name: new RegExp(`^${name}`) })).toBeInTheDocument()
    }
  })

  it('keeps the front door free of the confirmation nudge while nothing needs confirming', async () => {
    const { wrapper } = createWrapper()

    render(<ClassHub classId={1} tab="ask" />, { wrapper })

    await screen.findByRole('heading', { name: 'Continuous-Time Signals' })
    expect(
      screen.queryByRole('button', { name: /class facts? need(s)? confirmation/ }),
    ).not.toBeInTheDocument()
  })

  it('nudges for unconfirmed low-confidence facts from the class itself, into the details sheet', async () => {
    vi.spyOn(api, 'getClassProfile').mockResolvedValue({
      facts: [
        // Low and unchecked: the two that need the student.
        fact(1, 'low', false),
        fact(2, 'low', false),
        // High and unchecked: Lyra is sure, nobody has to act.
        fact(3, 'high', false),
        // Checked or rejected: decided, out of the loop.
        fact(4, 'low', true),
        fact(5, 'low', false, true),
      ],
      extraction_skipped_reason: null,
    } as ClassProfile)
    const { wrapper } = createWrapper()
    const user = userEvent.setup()

    render(<ClassHub classId={1} tab="ask" />, { wrapper })

    const chip = await screen.findByRole('button', {
      name: '2 class facts need confirmation',
    })
    await user.click(chip)
    expect(await screen.findByRole('heading', { name: 'Class details' })).toBeVisible()
  })

  it('says the singular form when one fact needs confirming', async () => {
    vi.spyOn(api, 'getClassProfile').mockResolvedValue({
      facts: [fact(1, 'low', false), fact(2, 'high', false)],
      extraction_skipped_reason: null,
    } as ClassProfile)
    const { wrapper } = createWrapper()

    render(<ClassHub classId={1} tab="ask" />, { wrapper })

    expect(
      await screen.findByRole('button', { name: '1 class fact needs confirmation' }),
    ).toBeInTheDocument()
  })

  it('keeps task tab names stable without routine inventory counts', async () => {
    const { wrapper } = createWrapper()
    render(<ClassHub classId={1} tab="ask" />, { wrapper })
    for (const name of ['Files', 'Work', 'Practice', 'Ask']) {
      expect(await screen.findByRole('tab', { name })).toBeInTheDocument()
    }
  })

  it('opens on a continuation surface: ask, resume, and the ways to start', async () => {
    const { wrapper } = createWrapper()

    render(<ClassHub classId={1} tab="ask" />, { wrapper })

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
      '/#/classes/1?tab=files',
    )
    expect(screen.getByText('Needs attention')).toBeInTheDocument()

    // Practice is the dominant way in: one balanced session, no choosing first. The rest of
    // the verbs stay one click away, and each goes somewhere real.
    expect(screen.getByRole('button', { name: 'New quiz' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Solve a problem set' })).toHaveAttribute(
      'href',
      '/#/classes/1/solutions/new',
    )
    expect(screen.getByRole('button', { name: 'Start writing' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Add documents' })).toHaveAttribute(
      'href',
      '/#/classes/1?tab=files',
    )
  })

  it('distinguishes an empty class from one whose documents are working or broken', async () => {
    const { wrapper } = createWrapper()

    // Failed-only: the class is not "empty", it is broken, and must say so.
    vi.spyOn(api, 'listDocuments').mockResolvedValue([
      { id: 5, class_id: 1, filename: 'syllabus.pdf', state: 'failed' },
      { id: 6, class_id: 1, filename: 'notes.pdf', state: 'failed' },
    ] as DocumentRead[])
    const failedView = render(<ClassHub classId={1} tab="ask" />, { wrapper })
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
    const unsupportedView = render(<ClassHub classId={1} tab="ask" />, {
      wrapper: createWrapper().wrapper,
    })
    expect(
      await screen.findByRole('link', { name: /One document could not be used/ }),
    ).toHaveAttribute('href', '/#/classes/1?tab=files')
    expect(screen.getByText('Needs attention')).toBeInTheDocument()
    expect(screen.queryByText(/Nothing uploaded yet/)).not.toBeInTheDocument()
    unsupportedView.unmount()

    // Mixed: one failed plus one unsupported is one aggregate row counting both, not
    // two rows or a count that quietly drops the unsupported file.
    vi.spyOn(api, 'listDocuments').mockResolvedValue([
      { id: 5, class_id: 1, filename: 'syllabus.pdf', state: 'failed' },
      { id: 8, class_id: 1, filename: 'lecture.key', state: 'unsupported' },
    ] as DocumentRead[])
    const mixedView = render(<ClassHub classId={1} tab="ask" />, {
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
    const workingView = render(<ClassHub classId={1} tab="ask" />, {
      wrapper: createWrapper().wrapper,
    })
    expect(await screen.findByRole('link', { name: /One document being read/ })).toBeInTheDocument()
    expect(screen.queryByText(/Nothing uploaded yet/)).not.toBeInTheDocument()
    workingView.unmount()

    // Truly empty: only now does the empty copy appear.
    vi.spyOn(api, 'listDocuments').mockResolvedValue([])
    render(<ClassHub classId={1} tab="ask" />, { wrapper: createWrapper().wrapper })
    expect(await screen.findByRole('link', { name: 'Add course materials' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'New quiz' })).not.toBeInTheDocument()
    expect(screen.queryByText('Summarize the material I uploaded')).not.toBeInTheDocument()
  })

  it('holds Practice until the document list has loaded', async () => {
    // A pending query means the ready count is unknown, not zero: the button must not be
    // clickable into a wrong "nothing ready" answer.
    vi.spyOn(api, 'listDocuments').mockReturnValue(new Promise(() => {}))
    const { wrapper } = createWrapper()

    render(<ClassHub classId={1} tab="ask" />, { wrapper })

    await screen.findByRole('textbox', { name: 'Ask about Continuous-Time Signals' })
    expect(screen.queryByRole('button', { name: 'New quiz' })).not.toBeInTheDocument()
  })

  it('says when the document list itself failed, and retries it on request', async () => {
    // A failed query is not a loading one: Practice stays held either way, but held
    // forever with no reason on screen would read as a broken button.
    const listDocuments = vi.spyOn(api, 'listDocuments').mockRejectedValue(new Error('offline'))
    const { wrapper } = createWrapper()

    render(<ClassHub classId={1} tab="ask" />, { wrapper })

    expect(await screen.findByText(/The document list did not load/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'New quiz' })).not.toBeInTheDocument()
    // Not knowing what is uploaded is not the same as knowing nothing is.
    expect(screen.queryByText(/Nothing uploaded yet/)).not.toBeInTheDocument()

    // Retry asks again; a recovered backend re-enables Practice and retires the notice.
    listDocuments.mockResolvedValue([
      { id: 3, class_id: 1, filename: 'homework_2.pdf', state: 'ready' },
    ] as DocumentRead[])
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'New quiz' })).toBeEnabled())
    expect(screen.queryByText(/The document list did not load/)).not.toBeInTheDocument()
  })

  it('sends a typed question into a new conversation, words and all', async () => {
    const { wrapper } = createWrapper()

    render(<ClassHub classId={1} tab="ask" />, { wrapper })

    const user = userEvent.setup()
    const box = await screen.findByRole('textbox', { name: 'Ask about Continuous-Time Signals' })
    await user.type(box, 'Why does convolution flip the signal?')
    await user.click(screen.getByRole('button', { name: 'Ask' }))

    expect(push).toHaveBeenCalledWith(
      '/classes/1/chat?session=new&ask=Why+does+convolution+flip+the+signal%3F&send=1',
    )
  })
})

it('leads an unconfigured empty class to tutor setup and upload, while retaining manual writing', async () => {
  vi.mocked(api.getSettings).mockResolvedValue({ endpoint_url: null, model: null } as SettingsRead)
  vi.mocked(api.listDocuments).mockResolvedValue([])
  render(<ClassHub classId={1} tab="ask" />, { wrapper: createWrapper().wrapper })
  expect(await screen.findByRole('link', { name: 'Set up your tutor' })).toHaveAttribute(
    'href',
    '/#/settings',
  )
  expect(screen.getByRole('link', { name: 'Add course materials' })).toHaveAttribute(
    'href',
    '/#/classes/1?tab=files',
  )
  expect(screen.getByRole('button', { name: 'Start writing' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'New quiz' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Ask' })).not.toBeInTheDocument()
})
