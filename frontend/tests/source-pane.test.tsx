import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SourcePane, type ProblemRegion } from '@/components/solutions/source-pane'
import { api, ApiError } from '@/lib/api'
import { TooltipProvider } from '@/components/ui/tooltip'
import type { DocumentRead, SolutionSource } from '@/types'

const { immediateAssetUrl, loadProtectedAssetSource, fetchProtectedAsset, saveOriginalDocument } =
  vi.hoisted(() => ({
    immediateAssetUrl: vi.fn<(path: string) => string | null>(),
    loadProtectedAssetSource: vi.fn(),
    fetchProtectedAsset: vi.fn(),
    saveOriginalDocument: vi.fn(),
  }))

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api')
  return {
    ...actual,
    immediateAssetUrl,
    loadProtectedAssetSource,
    fetchProtectedAsset,
  }
})

vi.mock('@/lib/runtime', async () => ({
  ...(await vi.importActual<typeof import('@/lib/runtime')>('@/lib/runtime')),
  saveOriginalDocument,
}))

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
  pages_failed: 0,
  recognize: false,
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
  immediateAssetUrl.mockImplementation((path) => `http://127.0.0.1:8000${path}`)
  loadProtectedAssetSource.mockReset()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function renderPane(
  anchor: { documentId: number; pageNumber: number | null } | null,
  extra: Partial<React.ComponentProps<typeof SourcePane>> = {},
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // Mirrors the providers the app mounts around every route. Radix requires the tooltip
  // provider somewhere above any trigger the pane's header renders.
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
  it('renders the controls its caller puts in the header', async () => {
    // The pane owns the page, not the layout around it: sizing the column to the page is
    // the workspace's to decide, so it hands the control in and this only finds it a place
    // to sit.
    renderPane(
      { documentId: 7, pageNumber: 1 },
      {
        fitToggle: <button type="button">Fit</button>,
      },
    )
    await decodePages()

    expect(screen.getByRole('button', { name: 'Fit' })).toBeInTheDocument()
  })

  it('releases a protected page blob that resolves after the reader moved on', async () => {
    immediateAssetUrl.mockReturnValue(null)
    const firstRelease = vi.fn()
    const secondRelease = vi.fn()
    let resolveFirst: ((value: { url: string; release: () => void }) => void) | null = null
    loadProtectedAssetSource
      .mockImplementationOnce(
        () =>
          new Promise<{ url: string; release: () => void }>((resolve) => {
            resolveFirst = resolve
          }),
      )
      .mockResolvedValueOnce({ url: 'blob:page-2', release: secondRelease })

    const { reanchor } = renderPane({ documentId: 7, pageNumber: 1 })
    reanchor({ documentId: 7, pageNumber: 2 })

    await act(async () => {
      resolveFirst?.({ url: 'blob:page-1', release: firstRelease })
    })

    expect(firstRelease).toHaveBeenCalledTimes(1)
    expect(secondRelease).not.toHaveBeenCalled()
  })
})

describe('source reading recovery', () => {
  it('retries the failed page without changing the selected page', async () => {
    immediateAssetUrl.mockReturnValue(null)
    loadProtectedAssetSource
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({ url: 'blob:retried' })
    renderPane({ documentId: 7, pageNumber: 2 })
    await userEvent.click(await screen.findByRole('button', { name: 'Retry page' }))
    await decodePages()
    expect(screen.getByAltText('Page 2')).toBeInTheDocument()
    expect(screen.getByText('page 2 of 3')).toBeInTheDocument()
    expect(loadProtectedAssetSource).toHaveBeenCalledTimes(2)
  })

  it('offers document text even when the page cannot render', async () => {
    immediateAssetUrl.mockReturnValue(null)
    loadProtectedAssetSource.mockRejectedValue(new Error('render failed'))
    vi.spyOn(api, 'getDocumentText').mockResolvedValue({
      filename: DOCUMENT.filename,
      text: 'Readable source equation',
      truncated: false,
    })
    renderPane({ documentId: 7, pageNumber: 2 })
    await userEvent.click(await screen.findByRole('button', { name: 'Read extracted text' }))
    expect(await screen.findByText('Readable source equation')).toBeVisible()
    expect(screen.getByText(/Extracted document text/)).toBeVisible()
    await userEvent.click(screen.getByRole('button', { name: 'View page' }))
    expect(screen.getByText('page 2 of 3')).toBeVisible()
  })

  it('zooms the source independently and resets without changing the page', async () => {
    renderPane({ documentId: 7, pageNumber: 2 })
    await decodePages()
    await userEvent.click(screen.getByRole('button', { name: 'Zoom in source page' }))
    expect(screen.getByText('125%')).toBeVisible()
    expect(screen.getByAltText('Page 2').parentElement?.parentElement).toHaveStyle({
      width: '125%',
    })
    await userEvent.click(screen.getByRole('button', { name: 'Reset zoom' }))
    expect(screen.getByText('100%')).toBeVisible()
    expect(screen.getByAltText('Page 2')).toBeVisible()
  })
})

describe('original document recovery', () => {
  async function doubleFailure(empty = false) {
    immediateAssetUrl.mockReturnValue(null)
    loadProtectedAssetSource.mockRejectedValue(new Error('render failed'))
    const text = vi.spyOn(api, 'getDocumentText')
    if (empty) text.mockResolvedValue({ filename: DOCUMENT.filename, text: '', truncated: false })
    else text.mockRejectedValue(new Error('text failed'))
    const onSelectProblem = vi.fn()
    renderPane(
      { documentId: 7, pageNumber: 2 },
      { regions: REGIONS, activeProblemId: 3, onSelectProblem },
    )
    await userEvent.click(await screen.findByRole('button', { name: 'Read extracted text' }))
    return onSelectProblem
  }

  it.each([false, true])(
    'saves the original after page and text failure (empty text: %s), retaining context',
    async (empty) => {
      const blob = new Blob(['original PDF bytes'])
      fetchProtectedAsset.mockResolvedValue(blob)
      saveOriginalDocument.mockResolvedValue('saved')
      const onSelectProblem = await doubleFailure(empty)
      await userEvent.click(await screen.findByRole('button', { name: 'Save original document' }))
      expect(await screen.findByRole('status')).toHaveTextContent('Original document saved.')
      expect(fetchProtectedAsset).toHaveBeenLastCalledWith(
        '/api/documents/7/original',
        expect.any(AbortSignal),
      )
      expect(saveOriginalDocument).toHaveBeenLastCalledWith(blob, DOCUMENT.filename)
      expect(screen.getByTitle(DOCUMENT.filename)).toBeVisible()
      expect(onSelectProblem).not.toHaveBeenCalled()
      await userEvent.click(screen.getByRole('button', { name: 'View page' }))
      expect(screen.getByText('page 2 of 3')).toBeVisible()
    },
  )

  it('reports an unavailable original honestly without losing the selected page', async () => {
    fetchProtectedAsset.mockRejectedValue(new ApiError(404, 'missing'))
    saveOriginalDocument.mockClear()
    await doubleFailure()
    await userEvent.click(await screen.findByRole('button', { name: 'Save original document' }))
    expect(
      await screen.findByText('The original document is missing or inaccessible.'),
    ).toBeVisible()
    expect(saveOriginalDocument).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'View page' }))
    expect(screen.getByText('page 2 of 3')).toBeVisible()
  })

  it('aborts a delayed original fetch when the reader leaves text recovery', async () => {
    let resolve: (blob: Blob) => void = () => undefined
    fetchProtectedAsset.mockImplementation(
      () =>
        new Promise<Blob>((done) => {
          resolve = done
        }),
    )
    saveOriginalDocument.mockClear()
    await doubleFailure()
    await userEvent.click(await screen.findByRole('button', { name: 'Save original document' }))
    const signal = fetchProtectedAsset.mock.lastCall?.[1] as AbortSignal
    await userEvent.click(screen.getByRole('button', { name: 'View page' }))
    expect(signal.aborted).toBe(true)
    await act(async () => resolve(new Blob(['late original'])))
    expect(saveOriginalDocument).not.toHaveBeenCalled()
    expect(screen.getByText('page 2 of 3')).toBeVisible()
  })

  it.each([
    'That destination already exists. Choose another filename to save the original document.',
    'The original document was saved, but final cleanup or durability could not be confirmed. Check the saved file before retrying.',
  ])('preserves the native publication outcome: %s', async (message) => {
    fetchProtectedAsset.mockResolvedValue(new Blob(['original']))
    // Tauri rejects Result::Err(String) with a string, not an Error object.
    saveOriginalDocument.mockRejectedValue(message)
    await doubleFailure()
    await userEvent.click(await screen.findByRole('button', { name: 'Save original document' }))
    expect(await screen.findByText(message, { exact: true })).toHaveAttribute('role', 'alert')
    expect(screen.getByRole('button', { name: 'Save original document' })).toBeEnabled()
  })

  it('does not expose arbitrary native error details', async () => {
    fetchProtectedAsset.mockResolvedValue(new Blob(['original']))
    saveOriginalDocument.mockRejectedValue('cannot open /private/user/destination.pdf')
    await doubleFailure()
    await userEvent.click(await screen.findByRole('button', { name: 'Save original document' }))
    expect(
      await screen.findByText(
        'The original document could not be saved. Try again or choose another destination.',
        { exact: true },
      ),
    ).toHaveAttribute('role', 'alert')
    expect(screen.queryByText(/private\/user/)).not.toBeInTheDocument()
  })

  it('does not claim success when the native save is cancelled', async () => {
    fetchProtectedAsset.mockResolvedValue(new Blob(['original']))
    saveOriginalDocument.mockResolvedValue('cancelled')
    await doubleFailure()
    await userEvent.click(await screen.findByRole('button', { name: 'Save original document' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Save cancelled.')
  })
})
