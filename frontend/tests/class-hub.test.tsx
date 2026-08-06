import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ClassHub, readHubTab } from '@/components/classes/class-hub'
import { api } from '@/lib/api'
import type { ClassProfile, ClassRead, DocumentRead, SessionRead, SolutionRead } from '@/types'

const replace = vi.fn()

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace, push: vi.fn(), prefetch: vi.fn() }),
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
    // Zero is left unsaid rather than shown as a nought beside the label.
    expect(screen.getByRole('tab', { name: 'Profile' })).toBeInTheDocument()
  })

  it('digests each section on the overview and links into the work itself', async () => {
    const { wrapper } = createWrapper()

    render(<ClassHub classId={1} tab="overview" />, { wrapper })

    expect(await screen.findByRole('link', { name: /Fourier week/ })).toHaveAttribute(
      'href',
      '/classes/1/chat?session=4',
    )
    expect(screen.getByRole('link', { name: /Homework 2/ })).toHaveAttribute(
      'href',
      '/classes/1/solutions/8',
    )
    expect(screen.getByText('syllabus.pdf')).toBeInTheDocument()
  })

  it('opens a new conversation at the chat route rather than at the class itself', async () => {
    const { wrapper } = createWrapper()

    render(<ClassHub classId={1} tab="overview" />, { wrapper })

    const links = await screen.findAllByRole('link', { name: /New chat/ })
    for (const link of links) expect(link).toHaveAttribute('href', '/classes/1/chat?session=new')
  })
})
