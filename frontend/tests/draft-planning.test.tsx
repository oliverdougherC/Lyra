import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { PlanPanel } from '@/components/drafts/plan-panel'
import { SourceLedger } from '@/components/drafts/source-ledger'
import { api } from '@/lib/api'
import { draftKeys } from '@/lib/hooks/use-drafts'
import { RouterProvider } from '@/router/hooks'
import type { DraftPlan } from '@/types'

function createWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return function TestWrapper({ children }: { children: ReactNode }) {
    return (
      <RouterProvider>
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      </RouterProvider>
    )
  }
}

const plan: DraftPlan = {
  id: 3,
  artifact_id: 8,
  version: 2,
  status: 'ready',
  brief_analysis: 'Compare the two accounts.',
  thesis: 'The second account better explains the evidence.',
  argument_map: [{ claim: 'The evidence is more complete.' }],
  sections: [
    {
      id: 11,
      section_ref: 'analysis',
      ordinal: 2,
      title: 'Analysis',
      job: 'Compare the explanations.',
      claim: 'The second explanation fits more observations.',
      evidence: ['Observation A'],
      sources: [4],
      word_budget: 500,
      research_notes: 'Use the lab report.',
    },
  ],
  created_at: '2026-08-07T10:00:00Z',
  updated_at: '2026-08-07T10:00:00Z',
}

beforeEach(() => vi.restoreAllMocks())

describe('PlanPanel', () => {
  it('shows the persistent plan and saves student edits', async () => {
    vi.spyOn(api, 'getDraftPlan').mockResolvedValue(plan)
    const update = vi.spyOn(api, 'updateDraftPlan').mockResolvedValue({ ...plan, version: 3 })
    render(<PlanPanel draftId={8} />, { wrapper: createWrapper() })

    expect(await screen.findByText('Plan v2')).toBeInTheDocument()
    const thesis = screen.getByLabelText('Thesis')
    await userEvent.clear(thesis)
    await userEvent.type(thesis, 'A narrower, stronger thesis.')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(update).toHaveBeenCalledWith(
      8,
      expect.objectContaining({ thesis: 'A narrower, stronger thesis.' }),
    )
  })
  it('keeps newlines while evidence is being typed and normalizes only on save', async () => {
    vi.spyOn(api, 'getDraftPlan').mockResolvedValue(plan)
    const update = vi.spyOn(api, 'updateDraftPlan').mockResolvedValue({ ...plan, version: 3 })
    render(<PlanPanel draftId={8} />, { wrapper: createWrapper() })
    const evidence = await screen.findByLabelText('Evidence, one item per line')
    fireEvent.change(evidence, { target: { value: 'First\n' } })
    expect(evidence).toHaveValue('First\n')
    await userEvent.type(evidence, 'Second')
    expect(evidence).toHaveValue('First\nSecond')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(update).toHaveBeenCalledWith(
      8,
      expect.objectContaining({
        sections: [expect.objectContaining({ evidence: ['First', 'Second'] })],
      }),
    )
  })

  it('preserves local thesis and evidence when a newer plan arrives', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    client.setQueryData(draftKeys.plan(8), plan)
    vi.spyOn(api, 'getDraftPlan').mockResolvedValue(plan)
    render(
      <QueryClientProvider client={client}>
        <PlanPanel draftId={8} />
      </QueryClientProvider>,
    )
    fireEvent.change(await screen.findByLabelText('Thesis'), {
      target: { value: 'My unsaved thesis' },
    })
    fireEvent.change(screen.getByLabelText('Evidence, one item per line'), {
      target: { value: 'My evidence\n' },
    })
    act(() => {
      client.setQueryData(draftKeys.plan(8), {
        ...plan,
        version: 3,
        thesis: 'Updated server thesis',
      })
    })
    expect(screen.getByLabelText('Thesis')).toHaveValue('My unsaved thesis')
    expect(screen.getByLabelText('Evidence, one item per line')).toHaveValue('My evidence\n')
    expect(await screen.findByText(/A newer plan is available/)).toBeInTheDocument()
    expect(screen.getByText('Unsaved edits')).toBeInTheDocument()
  })
  it('keeps the newer plan and local edits when an older save response arrives late', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    client.setQueryData(draftKeys.plan(8), plan)
    vi.spyOn(api, 'getDraftPlan').mockResolvedValue(plan)
    let resolveSave!: (plan: DraftPlan) => void
    vi.spyOn(api, 'updateDraftPlan').mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSave = resolve
        }),
    )
    render(
      <QueryClientProvider client={client}>
        <PlanPanel draftId={8} />
      </QueryClientProvider>,
    )
    fireEvent.change(await screen.findByLabelText('Thesis'), {
      target: { value: 'My saved thesis' },
    })
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    act(() => {
      client.setQueryData(draftKeys.plan(8), { ...plan, version: 4, thesis: 'Newer worker thesis' })
    })
    await screen.findByText(/A newer plan is available/)
    await act(async () => resolveSave({ ...plan, version: 3, thesis: 'My saved thesis' }))
    expect(client.getQueryData<DraftPlan>(draftKeys.plan(8))?.version).toBe(4)
    expect(screen.getByLabelText('Thesis')).toHaveValue('My saved thesis')
    expect(screen.getByText(/A newer plan is available/)).toBeInTheDocument()
    expect(screen.getByText('Unsaved edits')).toBeInTheDocument()
  })
})

describe('SourceLedger', () => {
  it('shows course and web evidence with the relied-on excerpts', async () => {
    vi.spyOn(api, 'listDraftSources').mockResolvedValue([
      {
        id: 4,
        class_id: 2,
        source_type: 'course',
        document_id: 7,
        url: null,
        title: 'Lab report',
        accessed_at: null,
        excerpts: [{ id: 1, section_ref: 'analysis', excerpt: 'The observed value doubled.' }],
      },
      {
        id: 5,
        class_id: 2,
        source_type: 'web',
        document_id: null,
        url: 'https://example.test/source',
        title: 'External study',
        accessed_at: '2026-08-07T10:00:00Z',
        excerpts: [],
      },
    ])
    render(<SourceLedger classId={2} />, { wrapper: createWrapper() })

    expect(await screen.findByText('Lab report')).toBeInTheDocument()
    expect(screen.getByText('The observed value doubled.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open External study' })).toHaveAttribute(
      'href',
      'https://example.test/source',
    )
  })
})
