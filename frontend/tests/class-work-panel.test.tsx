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
    await user.click(screen.getByRole('tab', { name: 'Solutions' }))
    await waitFor(() => expect(window.location.hash).toBe('#/classes/1?tab=work&work=solutions'))
    expect(await screen.findByRole('link', { name: /Homework 2/ })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Fourier week/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Signal notes draft/ })).not.toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: 'Drafts' }))
    await waitFor(() => expect(window.location.hash).toBe('#/classes/1?tab=work&work=drafts'))
    expect(await screen.findByRole('link', { name: /Signal notes draft/ })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Homework 2/ })).not.toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: 'Chats' }))
    await waitFor(() => expect(window.location.hash).toBe('#/classes/1?tab=work&work=chats'))
    expect(await screen.findByRole('link', { name: /Fourier week/ })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Homework 2/ })).not.toBeInTheDocument()

    // "All" drops the param: the route says what the list shows.
    await user.click(screen.getByRole('tab', { name: 'All' }))
    await waitFor(() => expect(window.location.hash).toBe('#/classes/1?tab=work'))
    expect(await screen.findByRole('link', { name: /Laplace transforms/ })).toBeInTheDocument()
  })

  it('reads a filtered URL on arrival, so a reload of the filtered view lands filtered', async () => {
    resetLocation('/#/classes/1?tab=work&work=chats')
    const { wrapper } = createWrapper()

    render(<ClassWorkPanel classId={1} />, { wrapper })

    await screen.findByRole('link', { name: /Fourier week/ })
    expect(screen.getByRole('tab', { name: 'Chats' })).toHaveAttribute('aria-selected', 'true')
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
