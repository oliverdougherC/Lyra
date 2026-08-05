import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { SourcePane, type ProblemRegion } from '@/components/solutions/source-pane'
import type { DocumentRead, SolutionSource } from '@/types'

/**
 * Contract from docs/ui-phase-2.md: scrolling away from the anchored page does not change
 * the selection. The reader is allowed to look around, including while Lyra is still
 * working, which is exactly when they are most likely to want to.
 */

const DOCUMENT: DocumentRead = {
  id: 7,
  class_id: 1,
  filename: 'homework_7.pdf',
  mime: 'application/pdf',
  byte_size: 1024,
  state: 'ready',
  stage_detail: null,
  pages_total: 3,
  pages_done: 3,
  pages_skipped: 0,
  error_message: null,
  created_at: '2026-08-04T00:00:00Z',
}

const SOURCES: SolutionSource[] = [
  { document_id: 7, filename: 'homework_7.pdf', role: 'problem_set', ordinal: 0 },
]

// Markers at a tenth and six tenths down the page. Each band starts a shade above its
// marker, so the two offsets cancel and the first band is exactly half the page.
const REGIONS: ProblemRegion[] = [
  { problemId: 1, documentId: 7, page: 1, top: 0.108, label: 'Problem 1' },
  { problemId: 2, documentId: 7, page: 1, top: 0.608, label: 'Problem 2' },
  { problemId: 3, documentId: 7, page: 2, top: 0.2, label: 'Problem 3' },
]

function renderPane(anchor: { documentId: number; pageNumber: number | null } | null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <QueryClientProvider client={client}>
      <SourcePane sources={SOURCES} documents={[DOCUMENT]} anchor={anchor} />
    </QueryClientProvider>,
  )
  return {
    ...view,
    reanchor: (next: { documentId: number; pageNumber: number | null } | null) =>
      view.rerender(
        <QueryClientProvider client={client}>
          <SourcePane sources={SOURCES} documents={[DOCUMENT]} anchor={next} />
        </QueryClientProvider>,
      ),
  }
}

describe('SourcePane', () => {
  it('keeps the reader on the page they turned to when nothing was reselected', async () => {
    // The workspace builds the anchor object fresh on every render and the solution detail
    // query polls while a solve runs, so an identity comparison here made the document
    // unpageable for as long as Lyra was working.
    const { reanchor } = renderPane({ documentId: 7, pageNumber: 1 })

    await userEvent.click(screen.getByRole('button', { name: 'Next page' }))
    expect(screen.getByText('page 2 of 3')).toBeInTheDocument()

    reanchor({ documentId: 7, pageNumber: 1 })

    expect(screen.getByText('page 2 of 3')).toBeInTheDocument()
  })

  it('runs each problem band from its own marker to the next one', async () => {
    // Where a problem ends is never decided from geometry: it ends where the next one
    // starts, so the page can never disagree with the segmentation the student confirmed.
    const onSelectProblem = vi.fn()
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <SourcePane
          sources={SOURCES}
          documents={[DOCUMENT]}
          anchor={{ documentId: 7, pageNumber: 1 }}
          regions={REGIONS}
          activeProblemId={2}
          onSelectProblem={onSelectProblem}
        />
      </QueryClientProvider>,
    )
    fireEvent.load(screen.getByAltText('Page 1'))

    const first = screen.getByRole('button', { name: 'Go to the solution for Problem 1' })
    const second = screen.getByRole('button', { name: 'Go to the solution for Problem 2' })
    // Problem 1 runs from its own marker down to problem 2's, and problem 2 runs to the
    // foot of the page because nothing follows it.
    expect(first.style.height).toBe('50%')
    expect(second.style.height).toBe('40%')
    // Only the problem being read is marked; the rest wait to be hovered.
    expect(second.className).toContain('border-accent-primary')
    expect(first.className).toContain('border-transparent')

    await userEvent.click(first)
    expect(onSelectProblem).toHaveBeenCalledWith(1)
  })

  it('draws nothing over a page whose problems were never located', () => {
    renderPane({ documentId: 7, pageNumber: 1 })
    fireEvent.load(screen.getByAltText('Page 1'))

    expect(screen.queryByRole('button', { name: /Go to the solution/ })).not.toBeInTheDocument()
  })

  it('follows a newly selected problem back to its own page', async () => {
    const { reanchor } = renderPane({ documentId: 7, pageNumber: 1 })

    await userEvent.click(screen.getByRole('button', { name: 'Next page' }))
    reanchor({ documentId: 7, pageNumber: 3 })

    expect(screen.getByText('page 3 of 3')).toBeInTheDocument()
  })
})
