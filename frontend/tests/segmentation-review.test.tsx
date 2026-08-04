import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { SegmentationReview } from '@/components/solutions/segmentation-review'
import type { SolutionDetail, SolutionPart } from '@/types'

/**
 * Contracts from docs/solver-phase-2.md: the gate exists so a missed or merged problem
 * costs one edit rather than a full re-run, and it has to say so. Merge and split are the
 * corrections that matter most here, and neither is expressible as a per-row edit, which
 * is why the whole list goes back on save.
 */

function part(overrides: Partial<SolutionPart> & { id: number }): SolutionPart {
  return {
    artifact_id: 1,
    parent_part_id: null,
    kind: 'problem',
    ordinal: 0,
    label: null,
    content: '',
    content_type: 'markdown',
    status: 'pending',
    origin: 'generated',
    verdict: 'unchecked',
    error_message: null,
    provenance: [],
    ...overrides,
  }
}

function solution(parts: SolutionPart[]): SolutionDetail {
  return {
    id: 1,
    class_id: 6,
    kind: 'solution_set',
    title: 'homework_4',
    state: 'awaiting_review',
    stage_detail: null,
    problems_total: parts.filter((one) => one.parent_part_id === null).length,
    problems_done: 0,
    error_message: null,
    created_at: '2026-08-03 12:00:00',
    updated_at: '2026-08-03 12:00:00',
    sources: [{ document_id: 8, role: 'problem_set', ordinal: 0, filename: 'homework_4.pdf' }],
    parts,
  }
}

const TWO_PROBLEMS = [
  part({
    id: 10,
    ordinal: 0,
    label: 'Problem 1',
    content: 'Find the transform.',
    provenance: [
      {
        chunk_id: 1,
        document_id: 8,
        page_number: 1,
        label: null,
        filename: 'homework_4.pdf',
      },
    ],
  }),
  part({ id: 11, ordinal: 1, label: 'Problem 2', content: 'Compute the convolution.' }),
]

function renderReview(parts: SolutionPart[] = TWO_PROBLEMS) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <SegmentationReview solution={solution(parts)} onResegment={vi.fn()} resegmenting={false} />
    </QueryClientProvider>,
  )
}

describe('SegmentationReview', () => {
  it('states the count and why checking it now is worth the interruption', () => {
    renderReview()

    expect(screen.getByText('Lyra found 2 problems')).toBeInTheDocument()
    expect(
      screen.getByText(/Fixing a problem now is much faster than re-solving one later/),
    ).toBeInTheDocument()
  })

  it('keeps Save disabled until something actually changes', async () => {
    renderReview()
    const save = screen.getByRole('button', { name: 'Save changes' })
    expect(save).toBeDisabled()

    await userEvent.click(screen.getByRole('button', { name: /Actions for Problem 1/ }))
    await userEvent.click(screen.getByRole('menuitem', { name: 'Merge with next' }))

    expect(screen.getByRole('button', { name: 'Save changes' })).toBeEnabled()
  })

  it('merges a problem into the next one and marks the result edited', async () => {
    renderReview()

    await userEvent.click(screen.getByRole('button', { name: /Actions for Problem 1/ }))
    await userEvent.click(screen.getByRole('menuitem', { name: 'Merge with next' }))

    expect(screen.getByText('Lyra found 1 problem')).toBeInTheDocument()
    expect(screen.getByText('Find the transform. Compute the convolution.')).toBeInTheDocument()
    // `Edited` means the statement is no longer verbatim from the sheet, which is exactly
    // what a merge makes true.
    expect(screen.getByText('Edited')).toBeInTheDocument()
  })

  it('cannot merge the last problem, because there is nothing after it', async () => {
    renderReview()

    await userEvent.click(screen.getByRole('button', { name: /Actions for Problem 2/ }))

    expect(screen.getByRole('menuitem', { name: 'Merge with next' })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
  })

  it('removes a problem the student says is not one', async () => {
    renderReview()

    await userEvent.click(screen.getByRole('button', { name: /Actions for Problem 2/ }))
    await userEvent.click(screen.getByRole('menuitem', { name: 'Remove' }))

    expect(screen.getByText('Lyra found 1 problem')).toBeInTheDocument()
    expect(screen.queryByText('Compute the convolution.')).not.toBeInTheDocument()
  })

  it('shows a source line only when it differs from the row above', () => {
    renderReview()

    // Eight problems from one file must not print eight identical citations, so only the
    // first row carries it.
    expect(screen.getAllByText(/homework_4\.pdf, page 1/)).toHaveLength(1)
  })

  it('offers a way forward when nothing could be segmented', () => {
    renderReview([])

    expect(screen.getByText('Lyra could not find separate problems')).toBeInTheDocument()
    // Not a dead end: some documents are prose, and that is a real outcome.
    expect(screen.getByRole('button', { name: 'Add a problem' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Read it again' })).toBeInTheDocument()
  })

  it('lists a problem sub-part under its problem and lets it be removed', async () => {
    renderReview([
      TWO_PROBLEMS[0],
      part({ id: 12, parent_part_id: 10, ordinal: 0, label: '(a)', content: 'Sketch it.' }),
    ])
    const card = screen.getByText('Sketch it.').closest('li')!

    await userEvent.click(
      within(card).getByRole('button', { name: 'Remove part (a) of Problem 1' }),
    )

    expect(screen.queryByText('Sketch it.')).not.toBeInTheDocument()
  })
})
