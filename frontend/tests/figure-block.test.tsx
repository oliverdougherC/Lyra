import { act, fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { FigureBlock } from '@/components/solutions/figure-block'
import type { SolutionPart } from '@/types'

const { immediateAssetUrl, loadProtectedAssetSource } = vi.hoisted(() => ({
  immediateAssetUrl: vi.fn<(path: string) => string | null>(),
  loadProtectedAssetSource: vi.fn(),
}))

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api')
  return {
    ...actual,
    immediateAssetUrl,
    loadProtectedAssetSource,
  }
})

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
  beforeEach(() => {
    immediateAssetUrl.mockImplementation((path) => `http://127.0.0.1:8000${path}`)
    loadProtectedAssetSource.mockReset()
  })

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

  it('is never blown up past the size the crop was taken at', () => {
    // Found at 1280 in the browser, where the reading column is wider than a crop. The
    // figure is a flex column, so a stretched image takes the column's full width whatever
    // its own is: the acceptance homework's block diagrams are 771px and were rendering at
    // 1215, which is a blurred picture of a diagram drawn in hairlines. `self-start` is
    // what keeps the scaling one-directional.
    render(<FigureBlock figure={figurePart()} />)

    expect(screen.getByRole('img').className).toContain('self-start')
  })

  it('releases a protected blob that resolves after the figure unmounts', async () => {
    immediateAssetUrl.mockReturnValue(null)
    const release = vi.fn()
    let resolveSource: ((value: { url: string; release: () => void }) => void) | null = null
    loadProtectedAssetSource.mockReturnValue(
      new Promise<{ url: string; release: () => void }>((resolve) => {
        resolveSource = resolve
      }),
    )

    const view = render(<FigureBlock figure={figurePart()} />)
    view.unmount()

    await act(async () => {
      resolveSource?.({ url: 'blob:late-figure', release })
    })

    expect(release).toHaveBeenCalledTimes(1)
  })

  it('releases the previous blob before showing a replacement figure', async () => {
    immediateAssetUrl.mockReturnValue(null)
    const firstRelease = vi.fn()
    const secondRelease = vi.fn()
    loadProtectedAssetSource
      .mockResolvedValueOnce({ url: 'blob:first', release: firstRelease })
      .mockResolvedValueOnce({ url: 'blob:second', release: secondRelease })

    const view = render(<FigureBlock figure={figurePart({ content: '7', label: 'Figure 7' })} />)

    await screen.findByRole('img', { name: 'Figure 7' })
    view.rerender(<FigureBlock figure={figurePart({ content: '8', label: 'Figure 8' })} />)
    await screen.findByRole('img', { name: 'Figure 8' })

    expect(firstRelease).toHaveBeenCalledTimes(1)
    expect(secondRelease).not.toHaveBeenCalled()
  })
})
