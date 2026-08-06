import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { FigureBlock } from '@/components/solutions/figure-block'
import type { SolutionPart } from '@/types'

function figurePart(overrides: Partial<SolutionPart> = {}): SolutionPart {
  return {
    id: 40,
    artifact_id: 1,
    parent_part_id: 2,
    kind: 'figure',
    ordinal: 0,
    label: 'Page 1, figure 2',
    // The figure's id, not a copy of the image: the picture follows the document.
    content: '7',
    content_type: 'image',
    status: 'complete',
    origin: 'generated',
    verdict: 'unchecked',
    verdict_detail: null,
    solve_parts: 'together',
    provenance: [
      {
        chunk_id: null,
        document_id: 3,
        page_number: 1,
        label: 'Page 1, figure 2',
        filename: 'homework_3.pdf',
        section_path: null,
        bbox: [0.3, 0.3, 0.7, 0.37],
      },
    ],
    checks: [],
    ...overrides,
  } as SolutionPart
}

describe('FigureBlock', () => {
  it('draws the figure by its id and cites the page it came from', () => {
    render(<FigureBlock figure={figurePart()} />)

    const image = screen.getByRole('img', { name: 'Page 1, figure 2' })
    expect(image).toHaveAttribute('src', expect.stringContaining('/api/figures/7'))
    expect(screen.getByText('homework_3.pdf, page 1')).toBeInTheDocument()
  })

  it('costs a caption rather than the solution when the image is gone', () => {
    // Not a broken-image glyph and not an empty row. The solution is worth more than the
    // figure, and an export or a re-index that lost the crop must not read as a rendering
    // fault in the working itself.
    render(<FigureBlock figure={figurePart()} />)

    fireEvent.error(screen.getByRole('img'))

    expect(screen.getByText('Figure not available')).toBeInTheDocument()
    expect(screen.getByText('Page 1, figure 2')).toBeInTheDocument()
  })

  it('never exceeds the reading column', () => {
    // A wide figure scales down rather than scrolling. Math scrolls because cutting an
    // equation loses information; a figure twenty percent smaller loses none.
    render(<FigureBlock figure={figurePart()} />)

    expect(screen.getByRole('img').className).toContain('max-w-full')
  })
})
