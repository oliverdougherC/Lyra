import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { PlanPanel } from '@/components/drafts/plan-panel'
import { SourceLedger } from '@/components/drafts/source-ledger'
import { api } from '@/lib/api'
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
