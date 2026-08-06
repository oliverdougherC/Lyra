import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ClassDraftsPanel } from '@/components/classes/class-drafts-panel'
import { api } from '@/lib/api'
import type { DraftRead } from '@/types'

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }),
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

function draft(overrides: Partial<DraftRead>): DraftRead {
  return {
    id: 8,
    class_id: 1,
    kind: 'draft',
    title: 'Essay on feedback systems',
    state: 'ready',
    stage_detail: null,
    problems_total: null,
    problems_done: 0,
    error_message: null,
    created_at: '2026-08-05 09:00:00',
    updated_at: '2026-08-05 09:00:00',
    ...overrides,
  }
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('ClassDraftsPanel', () => {
  it('shows a loading skeleton that matches the row layout', () => {
    // Never settled: the panel must hold its skeleton for as long as the list does.
    const { promise } = Promise.withResolvers<DraftRead[]>()
    vi.spyOn(api, 'listDrafts').mockReturnValue(promise)
    const { wrapper } = createWrapper()

    render(<ClassDraftsPanel classId={1} />, { wrapper })

    expect(screen.getByLabelText('Loading drafts')).toBeInTheDocument()
  })

  it('says what is missing and offers a create when there is nothing yet', async () => {
    vi.spyOn(api, 'listDrafts').mockResolvedValue([])
    const { wrapper } = createWrapper()

    render(<ClassDraftsPanel classId={1} />, { wrapper })

    expect(await screen.findByText('No drafts yet')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'New draft' })).toBeInTheDocument()
  })

  it('lists drafts as rows that navigate to their workspace', async () => {
    vi.spyOn(api, 'listDrafts').mockResolvedValue([
      draft({}),
      draft({ id: 9, title: 'Lab report' }),
    ])
    const { wrapper } = createWrapper()

    render(<ClassDraftsPanel classId={1} />, { wrapper })

    expect(await screen.findByRole('link', { name: /Essay on feedback systems/ })).toHaveAttribute(
      'href',
      '/classes/1/drafts/8',
    )
    expect(screen.getByRole('link', { name: /Lab report/ })).toHaveAttribute(
      'href',
      '/classes/1/drafts/9',
    )
    expect(screen.getAllByText(/Edited /)).toHaveLength(2)
  })

  it('shows the stage line instead of the edit time while a suggestion is running', async () => {
    vi.spyOn(api, 'listDrafts').mockResolvedValue([
      draft({ state: 'generating', stage_detail: 'Reading the draft' }),
    ])
    const { wrapper } = createWrapper()

    render(<ClassDraftsPanel classId={1} />, { wrapper })

    const row = await screen.findByRole('link', { name: /Essay on feedback systems/ })
    expect(row).toHaveTextContent('Reading the draft')
    expect(row).toHaveTextContent('Suggesting')
    expect(row).not.toHaveTextContent('Edited')
  })
})
