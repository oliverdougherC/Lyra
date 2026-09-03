import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import ClassHubPage from '@/app/classes/[id]/page'
import { api } from '@/lib/api'
import { RouterProvider } from '@/router/hooks'
import type { ClassProfile, ClassRead } from '@/types'

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

const KLASS = {
  id: 1,
  name: 'Continuous-Time Signals',
  code: 'ECE 203',
  semester: 'Spring 2026',
  archived: false,
  document_count: 1,
  created_at: '2026-01-05 09:00:00',
  last_active_at: '2026-08-05 09:00:00',
} as ClassRead

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(api, 'getClass').mockResolvedValue(KLASS)
  vi.spyOn(api, 'listSessions').mockResolvedValue([])
  vi.spyOn(api, 'listSolutions').mockResolvedValue([])
  vi.spyOn(api, 'listDocuments').mockResolvedValue([])
  vi.spyOn(api, 'listStudy').mockResolvedValue({ decks: [], quizzes: [] })
  vi.spyOn(api, 'listDrafts').mockResolvedValue([])
  vi.spyOn(api, 'getClassProfile').mockResolvedValue({
    facts: [],
    extraction_skipped_reason: null,
  } as ClassProfile)
})

describe('ClassHubPage legacy tab links', () => {
  it('moves a seven-tab-era ?tab=study onto practice', async () => {
    resetLocation('/#/classes/1?tab=study')
    const { wrapper } = createWrapper()

    render(<ClassHubPage />, { wrapper })

    await waitFor(() => expect(window.location.hash).toBe('#/classes/1?tab=practice'))
    expect(screen.getByRole('tab', { name: /Practice/ })).toHaveAttribute('aria-selected', 'true')
  })

  it('moves a seven-tab-era ?tab=documents onto files', async () => {
    resetLocation('/#/classes/1?tab=documents')
    const { wrapper } = createWrapper()

    render(<ClassHubPage />, { wrapper })

    await waitFor(() => expect(window.location.hash).toBe('#/classes/1?tab=files'))
    expect(screen.getByRole('tab', { name: /Files/ })).toHaveAttribute('aria-selected', 'true')
  })

  it('moves a seven-tab-era ?tab=chats onto the chats filter of work', async () => {
    resetLocation('/#/classes/1?tab=chats')
    const { wrapper } = createWrapper()

    render(<ClassHubPage />, { wrapper })

    await waitFor(() => expect(window.location.hash).toBe('#/classes/1?tab=work&work=chats'))
    expect(screen.getByRole('tab', { name: /^Work/ })).toHaveAttribute('aria-selected', 'true')
  })

  it('moves ?tab=overview and ?tab=profile onto the front door', async () => {
    resetLocation('/#/classes/1?tab=profile')
    const { wrapper } = createWrapper()

    render(<ClassHubPage />, { wrapper })

    await waitFor(() => expect(window.location.hash).toBe('#/classes/1'))
    expect(screen.getByRole('tab', { name: /^Ask/ })).toHaveAttribute('aria-selected', 'true')
  })

  it('leaves a canonical tab value alone', async () => {
    resetLocation('/#/classes/1?tab=work')
    const { wrapper } = createWrapper()

    render(<ClassHubPage />, { wrapper })

    await waitFor(() => expect(window.location.hash).toBe('#/classes/1?tab=work'))
    expect(screen.getByRole('tab', { name: /^Work/ })).toHaveAttribute('aria-selected', 'true')
  })
})
