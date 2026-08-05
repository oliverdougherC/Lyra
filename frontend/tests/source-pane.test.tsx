import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SourcePane, type ProblemRegion } from '@/components/solutions/source-pane'
import { TooltipProvider } from '@/components/ui/tooltip'
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

/**
 * Pages decode off-screen now, so nothing renders until a load resolves. jsdom never
 * fetches, so the tests own the timing: `decodePages` is the moment the browser would have
 * finished decoding, which is exactly the seam a page turn has to survive without blanking.
 */
let pendingDecodes: Array<() => void> = []

class FakeImage {
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  naturalWidth = 850
  naturalHeight = 1100
  complete = false
  #src = ''

  get src(): string {
    return this.#src
  }

  set src(value: string) {
    this.#src = value
    pendingDecodes.push(() => {
      this.complete = true
      this.onload?.()
    })
  }
}

async function decodePages(): Promise<void> {
  const queued = pendingDecodes
  pendingDecodes = []
  await act(async () => {
    queued.forEach((resolve) => resolve())
  })
}

beforeEach(() => {
  pendingDecodes = []
  vi.stubGlobal('Image', FakeImage)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function renderPane(
  anchor: { documentId: number; pageNumber: number | null } | null,
  extra: Partial<React.ComponentProps<typeof SourcePane>> = {},
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // Mirrors the providers the app mounts around every route: the magnifier's control is a
  // tooltip trigger, and Radix requires the provider to be somewhere above it.
  const wrap = (node: React.ReactNode) => (
    <QueryClientProvider client={client}>
      <TooltipProvider>{node}</TooltipProvider>
    </QueryClientProvider>
  )
  const view = render(
    wrap(<SourcePane sources={SOURCES} documents={[DOCUMENT]} anchor={anchor} {...extra} />),
  )
  return {
    ...view,
    reanchor: (next: { documentId: number; pageNumber: number | null } | null) =>
      view.rerender(
        wrap(<SourcePane sources={SOURCES} documents={[DOCUMENT]} anchor={next} {...extra} />),
      ),
  }
}

describe('SourcePane', () => {
  it('keeps the reader on the page they turned to when nothing was reselected', async () => {
    // The workspace builds the anchor object fresh on every render and the solution detail
    // query polls while a solve runs, so an identity comparison here made the document
    // unpageable for as long as Lyra was working.
    const { reanchor } = renderPane({ documentId: 7, pageNumber: 1 })
    await decodePages()

    await userEvent.click(screen.getByRole('button', { name: 'Next page' }))
    expect(screen.getByText('page 2 of 3')).toBeInTheDocument()

    reanchor({ documentId: 7, pageNumber: 1 })

    expect(screen.getByText('page 2 of 3')).toBeInTheDocument()
  })

  it('holds the current page on screen until the next one has decoded', async () => {
    // A turn must never blank the pane. The previous page stays put for the whole of the
    // next one's load and is replaced in a single swap, so there is no frame showing
    // neither page.
    renderPane({ documentId: 7, pageNumber: 1 })
    await decodePages()
    expect(screen.getByAltText('Page 1')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Next page' }))

    // Mid-turn: page 2 is requested but not yet decoded, and page 1 is still standing.
    expect(screen.getByAltText('Page 1')).toBeInTheDocument()
    expect(screen.queryByAltText('Page 2')).not.toBeInTheDocument()

    await decodePages()

    expect(screen.getByAltText('Page 2')).toBeInTheDocument()
    expect(screen.queryByAltText('Page 1')).not.toBeInTheDocument()
  })

  it('reports the width its page needs so the column can be fitted to it', async () => {
    const onFitWidth = vi.fn()
    renderPane({ documentId: 7, pageNumber: 1 }, { onFitWidth })
    await decodePages()

    // jsdom lays nothing out, so the viewport measures zero and there is no honest width
    // to report. The contract under test is that a zero measurement is declined rather
    // than turned into a nonsense column width.
    expect(onFitWidth).not.toHaveBeenCalled()
  })

  it('runs each problem band from its own marker to the next one', async () => {
    // Where a problem ends is never decided from geometry: it ends where the next one
    // starts, so the page can never disagree with the segmentation the student confirmed.
    const onSelectProblem = vi.fn()
    renderPane(
      { documentId: 7, pageNumber: 1 },
      { regions: REGIONS, activeProblemId: 2, onSelectProblem },
    )
    await decodePages()

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

  it('withholds the bands while a page turn is still in flight', async () => {
    // The bands belong to the page being requested, and the page on screen is still the
    // previous one. Drawing them early would put problem 3's band over page 1.
    renderPane({ documentId: 7, pageNumber: 1 }, { regions: REGIONS, onSelectProblem: vi.fn() })
    await decodePages()
    expect(screen.getByRole('button', { name: 'Go to the solution for Problem 1' })).toBeVisible()

    await userEvent.click(screen.getByRole('button', { name: 'Next page' }))

    expect(screen.queryByRole('button', { name: /Go to the solution/ })).not.toBeInTheDocument()
  })

  it('draws nothing over a page whose problems were never located', async () => {
    renderPane({ documentId: 7, pageNumber: 1 })
    await decodePages()

    expect(screen.queryByRole('button', { name: /Go to the solution/ })).not.toBeInTheDocument()
  })

  describe('magnified', () => {
    // jsdom lays nothing out and never delivers resize notifications, so the pane's
    // measurements are supplied here. The numbers are a real column: 800x700 of viewport
    // around a 20px gutter, holding a letter-shaped page.
    const PANE_WIDTH = 800
    const PANE_HEIGHT = 700
    const PAGE_ASPECT = 850 / 1100

    beforeEach(() => {
      vi.stubGlobal(
        'ResizeObserver',
        class {
          constructor(private readonly callback: () => void) {}
          observe() {
            this.callback()
          }
          disconnect() {}
        },
      )
      Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
        configurable: true,
        get: () => PANE_WIDTH,
      })
      Object.defineProperty(HTMLElement.prototype, 'clientHeight', {
        configurable: true,
        get: () => PANE_HEIGHT,
      })
    })

    afterEach(() => {
      // @ts-expect-error -- restoring jsdom's own zero-size getters.
      delete HTMLElement.prototype.clientWidth
      // @ts-expect-error -- restoring jsdom's own zero-size getters.
      delete HTMLElement.prototype.clientHeight
    })

    /**
     * The page's own shape. A window wider than this is one that has been cropped.
     *
     * Parsed rather than read as a number: the style is normalised to `"<w> / <h>"` form.
     */
    function windowAspect(): number {
      const image = screen.getByAltText(/^Page/)
      const [width, height = '1'] = (image.parentElement as HTMLElement).style.aspectRatio
        .split('/')
        .map((part) => part.trim())
      return Number(width) / Number(height)
    }

    it('crops to a window centred on the problem in view', async () => {
      renderPane(
        { documentId: 7, pageNumber: 1 },
        { regions: REGIONS, activeProblemId: 1, magnified: true, onMagnifiedChange: vi.fn() },
      )
      await decodePages()

      // Cropped: the window is wider relative to its height than the page it looks onto.
      expect(windowAspect()).toBeGreaterThan(PAGE_ASPECT)
      // The page is pulled up behind the window so the band sits inside it, never so far
      // that blank space appears above the sheet.
      const top = Number.parseFloat(screen.getByAltText(/^Page/).style.top)
      expect(top).toBeLessThanOrEqual(0)
      expect(top).toBeGreaterThan(-100)
    })

    it('asks for a wider column than the whole page would need', async () => {
      const whole = vi.fn()
      renderPane({ documentId: 7, pageNumber: 1 }, { regions: REGIONS, onFitWidth: whole })
      await decodePages()

      const magnifiedFit = vi.fn()
      renderPane(
        { documentId: 7, pageNumber: 1 },
        { regions: REGIONS, activeProblemId: 1, magnified: true, onFitWidth: magnifiedFit },
      )
      await decodePages()

      const wholeWidth = whole.mock.calls.at(-1)?.[0] as number
      const zoomedWidth = magnifiedFit.mock.calls.at(-1)?.[0] as number
      expect(zoomedWidth).toBeGreaterThan(wholeWidth)
    })

    it('leaves the page whole when the problem in view was never located', async () => {
      // Problem 3 lives on page 2, so page 1 has no band to train the magnifier on. The
      // pane falls back to the plain page rather than inventing a crop.
      renderPane(
        { documentId: 7, pageNumber: 1 },
        { regions: REGIONS, activeProblemId: 3, magnified: true, onMagnifiedChange: vi.fn() },
      )
      await decodePages()

      expect(windowAspect()).toBeCloseTo(PAGE_ASPECT, 3)
    })
  })

  it('follows a newly selected problem back to its own page', async () => {
    const { reanchor } = renderPane({ documentId: 7, pageNumber: 1 })
    await decodePages()

    await userEvent.click(screen.getByRole('button', { name: 'Next page' }))
    reanchor({ documentId: 7, pageNumber: 3 })

    expect(screen.getByText('page 3 of 3')).toBeInTheDocument()
  })
})
