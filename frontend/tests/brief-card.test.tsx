import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { BriefCard } from '@/components/drafts/brief-card'
import { api } from '@/lib/api'
import { draftKeys } from '@/lib/hooks/use-drafts'
import type { DraftBrief } from '@/types'

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  return { wrapper, queryClient }
}

function brief(overrides: Partial<DraftBrief>): DraftBrief {
  return {
    artifact_id: 8,
    assignment_type: 'lab report',
    summary: 'Pendulum period vs. length.',
    audience: 'the TA',
    length_target: '5 pages',
    source_document_id: null,
    status: 'proposed',
    created_at: '2026-08-06 09:00:00',
    updated_at: '2026-08-06 09:00:00',
    ...overrides,
  }
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('BriefCard', () => {
  it('invites setup when there is no brief yet', async () => {
    vi.spyOn(api, 'getBrief').mockResolvedValue(null)
    const { wrapper } = createWrapper()

    render(<BriefCard draftId={8} />, { wrapper })

    expect(await screen.findByText(/No brief yet/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Set up' })).toBeInTheDocument()
  })

  it('marks a proposed brief as a guess and offers Confirm', async () => {
    vi.spyOn(api, 'getBrief').mockResolvedValue(brief({}))
    const { wrapper } = createWrapper()

    render(<BriefCard draftId={8} />, { wrapper })

    expect(await screen.findByText("Lyra's guess")).toBeInTheDocument()
    expect(screen.getByText('Pendulum period vs. length.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Confirm/ })).toBeInTheDocument()
  })

  it('confirming calls the endpoint and settles the card', async () => {
    vi.spyOn(api, 'getBrief').mockResolvedValue(brief({}))
    const confirm = vi.spyOn(api, 'confirmBrief').mockResolvedValue(brief({ status: 'confirmed' }))
    const { wrapper } = createWrapper()
    render(<BriefCard draftId={8} />, { wrapper })

    await userEvent.click(await screen.findByRole('button', { name: /Confirm/ }))

    expect(confirm).toHaveBeenCalledWith(8)
    expect(await screen.findByText('Pendulum period vs. length.')).toBeInTheDocument()
    expect(screen.queryByText("Lyra's guess")).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Confirm/ })).not.toBeInTheDocument()
  })

  it('a confirmed brief reads as one settled line with an edit affordance', async () => {
    vi.spyOn(api, 'getBrief').mockResolvedValue(brief({ status: 'confirmed' }))
    const { wrapper } = createWrapper()

    render(<BriefCard draftId={8} />, { wrapper })

    expect(await screen.findByText('Pendulum period vs. length.')).toBeInTheDocument()
    expect(screen.getByText('lab report · 5 pages · the TA')).toBeInTheDocument()
    expect(screen.queryByText("Lyra's guess")).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Edit the brief' })).toBeInTheDocument()
  })

  it('saving the form sends the students words and closes it', async () => {
    vi.spyOn(api, 'getBrief').mockResolvedValue(null)
    const put = vi
      .spyOn(api, 'putBrief')
      .mockResolvedValue(brief({ summary: 'My essay on entropy.', status: 'confirmed' }))
    const { wrapper } = createWrapper()
    render(<BriefCard draftId={8} />, { wrapper })

    await userEvent.click(await screen.findByRole('button', { name: 'Set up' }))
    await userEvent.type(screen.getByLabelText('What is this?'), 'My essay on entropy.')
    await userEvent.type(screen.getByLabelText('Length'), '2000 words')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(put).toHaveBeenCalledWith(8, {
      summary: 'My essay on entropy.',
      assignment_type: '',
      audience: '',
      length_target: '2000 words',
      source_document_id: null,
    })
    // The form closes on the settled card, which now shows the saved summary.
    expect(await screen.findByText('My essay on entropy.')).toBeInTheDocument()
    expect(screen.queryByLabelText('Edit the brief', { selector: 'form' })).toBeNull()
  })
  it('shows a retryable load failure instead of claiming a brief is absent', async () => {
    vi.spyOn(api, 'getBrief')
      .mockRejectedValueOnce(new Error('Offline'))
      .mockResolvedValue(brief({}))
    const { wrapper } = createWrapper()
    render(<BriefCard draftId={8} />, { wrapper })
    expect(await screen.findByRole('alert')).toHaveTextContent('The brief could not be refreshed')
    expect(screen.queryByText(/No brief yet/)).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Retry brief' }))
    expect(await screen.findByText('Pendulum period vs. length.')).toBeInTheDocument()
  })
  it('retains the saved brief when a background refresh fails', async () => {
    vi.spyOn(api, 'getBrief').mockRejectedValue(new Error('Offline'))
    const { wrapper, queryClient } = createWrapper()
    queryClient.setQueryData(draftKeys.brief(8), brief({}))
    render(<BriefCard draftId={8} />, { wrapper })
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: draftKeys.brief(8) })
    })
    expect(await screen.findByRole('alert')).toHaveTextContent('Showing the saved brief')
    expect(screen.getByText('Pendulum period vs. length.')).toBeInTheDocument()
  })
})
