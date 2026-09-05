'use client'

import { ChevronLeft, ChevronRight, FileText, ZoomIn, ZoomOut } from 'lucide-react'
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { api, documentPagePath, immediateAssetUrl, loadProtectedAssetSource } from '@/lib/api'
import { truncateMiddle } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { DocumentRead, SolutionSource } from '@/types'

/** Where one problem starts in its source document, as a fraction of the page box. */
export type ProblemRegion = {
  problemId: number
  documentId: number
  page: number
  /** Top of the problem's own marker line. */
  top: number
  label: string
}

type SourcePaneProps = {
  sources: SolutionSource[]
  documents: DocumentRead[]
  /** The page the selected problem was found on, or null when that is not known. */
  anchor: { documentId: number; pageNumber: number | null } | null
  /** Where each problem sits on its page, for the bands drawn over the image. */
  regions?: ProblemRegion[]
  /** The problem being read, which is marked on the page. */
  activeProblemId?: number | null
  onSelectProblem?: (problemId: number) => void
  /**
   * The width, in pixels, this pane would need for its page to stand whole. Reported from
   * the load that first reveals a page, so the column can be sized in the same commit.
   */
  onFitWidth?: (width: number) => void
  /** The control that sizes the column to the page, rendered in this pane's header. */
  fitToggle?: React.ReactNode
}

/** The padding around the page inside the scrolling area, per side. Matches `p-5`. */
const PAGE_GUTTER_PX = 20

/**
 * Turn the problems on one page into the bands that cover it.
 *
 * A band runs from its own problem's marker to the next one's, so the answer to "where
 * does problem 3 end" is always "where problem 4 starts". Deriving the extent here rather
 * than storing it keeps geometry from ever disagreeing with the segmentation the student
 * confirmed at the review gate.
 */
function bandsOn(regions: ProblemRegion[], documentId: number, page: number) {
  const onPage = regions
    .filter((region) => region.documentId === documentId && region.page === page)
    .sort((left, right) => left.top - right.top)
  return onPage.map((region, index) => ({
    ...region,
    // A little above the marker, so the band reads as containing the heading rather than
    // starting halfway through its letters.
    from: Math.max(0, region.top - 0.008),
    to: onPage[index + 1] ? Math.max(0, onPage[index + 1].top - 0.008) : 1,
  }))
}

/**
 * The page a problem came from, beside its solution.
 *
 * PDFs render as page images rather than through an embedded viewer: that is what buys
 * exact anchoring, identical rendering in both themes and every browser, and no new
 * frontend dependency. TXT and MD have no pages, so they render as their extracted text,
 * which is the same anchor with a different surface.
 *
 * Scrolling away from the anchored page does not change the selection. The reader is
 * allowed to look around.
 */
export function SourcePane({
  sources,
  documents,
  anchor,
  regions = [],
  activeProblemId = null,
  onSelectProblem,
  onFitWidth,
  fitToggle = null,
}: SourcePaneProps) {
  const viewportRef = useRef<HTMLDivElement | null>(null)
  // The shape of the page on screen, learned when it decoded. Held here rather than inside
  // the image, because the width this column asks for is computed from it.
  const [pageAspect, setPageAspect] = useState<number | null>(null)
  const [zoom, setZoom] = useState(1)
  const [textView, setTextView] = useState(false)
  const problemSets = sources.filter((source) => source.role === 'problem_set')
  // Where the reader has navigated to by hand, which outranks the anchor until the
  // selected problem changes.
  const [browsing, setBrowsing] = useState<{ documentId: number; page: number } | null>(null)
  // Compared by value, not by identity. The caller builds this object fresh on every
  // render, and the detail query polls while a solve is running, so an identity check
  // threw away the reader's page turn every couple of seconds: the document simply could
  // not be paged through for as long as Lyra was working.
  const anchorKey = anchor ? `${anchor.documentId}:${anchor.pageNumber ?? ''}` : ''
  const [lastAnchorKey, setLastAnchorKey] = useState(anchorKey)

  // Adjusted during render rather than in an effect: a new anchor means a new problem is
  // selected, and the reader's manual page turn belonged to the previous one.
  if (anchorKey !== lastAnchorKey) {
    setLastAnchorKey(anchorKey)
    setBrowsing(null)
  }

  const documentId =
    browsing?.documentId ?? anchor?.documentId ?? problemSets[0]?.document_id ?? null
  const page = browsing?.page ?? anchor?.pageNumber ?? 1
  const setPage = (next: number) =>
    documentId === null ? undefined : setBrowsing({ documentId, page: next })

  const document = documents.find((entry) => entry.id === documentId)
  const isPdf = document?.mime === 'application/pdf'
  const pages = document?.pages_total ?? 1
  const filename = document?.filename ?? sources.find((s) => s.document_id === documentId)?.filename

  // Measured rather than assumed: the width a whole page needs depends on the height this
  // pane was actually given, and that is capped by the panel minimums.
  const [paneSize, setPaneSize] = useState<{ width: number; height: number } | null>(null)
  const measurePane = useCallback(() => {
    const viewport = viewportRef.current
    if (!viewport) return
    const width = viewport.clientWidth
    const height = viewport.clientHeight
    // Same numbers, same object: this runs after every commit, and a fresh object each
    // time would re-render forever.
    setPaneSize((current) =>
      current && current.width === width && current.height === height ? current : { width, height },
    )
  }, [])

  // Measured after every commit as well as on resize. A ResizeObserver alone is not enough:
  // like animation frames, its callbacks are delivered as part of rendering, so a document
  // that is never rendered would leave this unmeasured and its column never sized.
  useLayoutEffect(measurePane)

  useEffect(() => {
    const viewport = viewportRef.current
    if (!viewport) return
    // Dragging the split changes this pane's width without re-rendering it.
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measurePane)
    observer?.observe(viewport)
    // And the window changing height changes what a whole page has to fit into, which is
    // the measurement the column's width is computed from. Said outright rather than left
    // to the observer: a stale height here is a page grown past the bottom of the pane.
    window.addEventListener('resize', measurePane)
    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', measurePane)
    }
  }, [measurePane])

  const pageBands = documentId === null ? [] : bandsOn(regions, documentId, page)

  /** The height a page has to fit into, inside the gutter. Null before it is measured. */
  const available = paneSize ? paneSize.height - PAGE_GUTTER_PX * 2 : null

  /**
   * Ask for the width this column needs to stand a whole page in.
   *
   * The height available is a property of the window, not of this column, so the arithmetic
   * runs one way only: a page of a given shape needs a particular width to fill that height
   * exactly, and asking for it cannot feed back into the measurement.
   *
   * Measured rather than read once, so a window that changes height asks for the width that
   * new height wants. A shorter window needs a narrower column to hold a whole page, and a
   * column that stayed put would leave the reader scrolling a sheet that used to stand whole.
   *
   * The first fit lands in the same beat the page decodes, so the column is already the
   * right width before there is anything in it to see. Later ones are coalesced: a height
   * that changes over a run of frames -- the window dragged, or the header folding away in
   * immersive mode -- would otherwise ask for a new width on every one of them, and each
   * answer re-renders the workbench and re-lays the split, which is a whole animation of
   * choppiness for a column that only needed to settle once the height stopped moving. So
   * the settled height is the one that gets a fit, and the frames on the way to it do not.
   */
  const hasFitRef = useRef(false)
  useEffect(() => {
    if (!onFitWidth || !pageAspect || available === null || available <= 0) return
    const width = available * pageAspect + PAGE_GUTTER_PX * 2
    if (!hasFitRef.current) {
      hasFitRef.current = true
      onFitWidth(width)
      return
    }
    const settle = window.setTimeout(() => onFitWidth(width), 150)
    return () => window.clearTimeout(settle)
  }, [available, onFitWidth, pageAspect])

  if (documentId === null) {
    return (
      <div className="text-text-tertiary flex h-full items-center justify-center p-6 text-sm">
        This solution set has no source document left to show.
      </div>
    )
  }

  return (
    <section aria-label="Source document" className="flex h-full min-h-0 flex-col">
      {/* An explicit height, matching the solutions pane's and the Phase 1 workspace's.
          Both headers used to size themselves from their own controls, and the solutions
          side has a button where this side has a filename, so the two rules sat 16px
          apart and the split read as two panels rather than one. */}
      <header className="border-border flex h-9 shrink-0 items-center justify-between gap-2 border-b px-4 lg:h-10">
        <span className="flex min-w-0 items-center gap-2">
          <FileText className="text-text-tertiary size-4 shrink-0" aria-hidden />
          <span className="text-text-secondary truncate text-xs" title={filename}>
            {filename ? truncateMiddle(filename, 30) : 'Source'}
          </span>
        </span>
        <span className="flex shrink-0 items-center gap-1">
          {problemSets.length > 1 ? (
            <select
              className="border-border bg-card text-text-secondary rounded-md border px-2 py-1 text-xs"
              value={documentId}
              onChange={(event) => setBrowsing({ documentId: Number(event.target.value), page: 1 })}
              aria-label="Source document"
            >
              {problemSets.map((source) => (
                <option key={source.document_id} value={source.document_id}>
                  {source.filename}
                </option>
              ))}
            </select>
          ) : null}
          {fitToggle}
        </span>
      </header>

      {isPdf ? (
        <div className="border-border flex shrink-0 flex-wrap items-center gap-1 border-b px-3 py-1">
          <Button
            size="sm"
            variant="ghost"
            aria-pressed={textView}
            onClick={() => setTextView((value) => !value)}
          >
            {textView ? 'View page' : 'Read text'}
          </Button>
          {!textView ? (
            <>
              <Button
                size="icon"
                variant="ghost"
                className="size-8"
                aria-label="Zoom out source page"
                disabled={zoom <= 1}
                onClick={() => setZoom((value) => Math.max(1, value - 0.25))}
              >
                <ZoomOut className="size-4" />
              </Button>
              <span aria-live="polite" className="text-text-secondary text-xs tabular-nums">
                {Math.round(zoom * 100)}%
              </span>
              <Button
                size="icon"
                variant="ghost"
                className="size-8"
                aria-label="Zoom in source page"
                disabled={zoom >= 3}
                onClick={() => setZoom((value) => Math.min(3, value + 0.25))}
              >
                <ZoomIn className="size-4" />
              </Button>
              {zoom !== 1 ? (
                <Button size="sm" variant="ghost" onClick={() => setZoom(1)}>
                  Reset zoom
                </Button>
              ) : null}
            </>
          ) : null}
        </div>
      ) : null}

      {/* The desk under the sheet: a slightly sunken tone, so the rendered page reads as
          a physical page lying on the workspace rather than a white rectangle in a form. */}
      <div
        ref={viewportRef}
        role="region"
        aria-label="Source content"
        tabIndex={0}
        className="bg-muted/40 min-h-0 flex-1 overflow-auto focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <div className="p-5">
          {isPdf && !textView ? (
            // Keyed by document alone. Keying by page too remounted the element on every
            // turn, which is what made a page change blink.
            <PageImage
              key={documentId}
              documentId={documentId}
              page={page}
              bands={onSelectProblem ? pageBands : []}
              activeProblemId={activeProblemId}
              onSelect={onSelectProblem}
              onDecoded={setPageAspect}
              availableHeight={available}
              availableWidth={paneSize ? Math.max(0, paneSize.width - PAGE_GUTTER_PX * 2) : null}
              zoom={zoom}
              onReadText={() => setTextView(true)}
            />
          ) : (
            <SourceText documentId={documentId} isPdf={isPdf} />
          )}
        </div>
      </div>

      {isPdf && !textView && pages > 1 ? (
        <footer className="border-border flex items-center justify-center gap-3 border-t px-4 py-2">
          <Button
            variant="ghost"
            size="sm"
            className="size-8 p-0"
            onClick={() => setPage(Math.max(1, page - 1))}
            disabled={page <= 1}
            aria-label="Previous page"
          >
            <ChevronLeft className="size-4" />
          </Button>
          <span className="text-text-tertiary text-xs tabular-nums">
            page {page} of {pages}
          </span>
          <Button
            variant="ghost"
            size="sm"
            className="size-8 p-0"
            onClick={() => setPage(Math.min(pages, page + 1))}
            disabled={page >= pages}
            aria-label="Next page"
          >
            <ChevronRight className="size-4" />
          </Button>
        </footer>
      ) : null}
    </section>
  )
}

/**
 * One rendered page of the source document.
 *
 * The page on screen is held in state rather than driven straight from the `page` prop, so
 * that turning a page never blanks the pane. Pointing a live `<img>` at a new `src`, or
 * remounting it per page, clears the element the moment the request starts and leaves it
 * empty until the next render decodes — a flash of nothing on every turn, however fast the
 * backend answers. Here the next page is decoded off-screen first and only replaces the
 * current one once it is ready to paint, so a turn is a single clean swap.
 */
function PageImage({
  documentId,
  page,
  bands,
  activeProblemId,
  onSelect,
  onDecoded,
  availableHeight = null,
  availableWidth = null,
  zoom = 1,
  onReadText,
}: {
  documentId: number
  page: number
  bands: ReturnType<typeof bandsOn>
  activeProblemId: number | null
  onSelect?: (problemId: number) => void
  /** The decoded page's aspect ratio, width over height. */
  onDecoded?: (aspect: number) => void
  /** The height the page has to stand in, inside the gutter. Null before it is measured. */
  availableHeight?: number | null
  availableWidth?: number | null
  zoom?: number
  onReadText: () => void
}) {
  const src = documentPagePath(documentId, page)
  const [attempt, setAttempt] = useState(0)
  const [shown, setShown] = useState<{
    src: string
    page: number
    aspect: number
    revoke?: () => void
  } | null>(null)
  // Which page failed, rather than a flag that has to be cleared as each new load starts.
  // A flag would mean writing state from the effect body on every page turn, and the reset
  // is not information the effect owns: whether *this* page has failed is derivable.
  const [failedSrc, setFailedSrc] = useState<string | null>(null)
  const failed = failedSrc === src

  useEffect(() => {
    const controller = new AbortController()
    let releaseShown: (() => void) | undefined
    let adopted = false
    const image = new Image()
    const settle = () => {
      if (controller.signal.aborted || !image.src) return
      const aspect = image.naturalHeight > 0 ? image.naturalWidth / image.naturalHeight : 0
      // Both updates land in one commit, so the page appears at the width it was measured
      // for rather than arriving and then being resized under the reader.
      setShown((current) => {
        current?.revoke?.()
        return { src: image.src, page, aspect, revoke: releaseShown }
      })
      setFailedSrc(null)
      adopted = true
      if (aspect > 0) onDecoded?.(aspect)
    }

    image.onload = settle
    image.onerror = () => {
      if (!controller.signal.aborted) {
        releaseShown?.()
        setFailedSrc(src)
      }
    }
    const directUrl = immediateAssetUrl(src)
    if (directUrl) {
      image.src = directUrl
      if (image.complete && image.naturalWidth > 0) settle()
    } else {
      void loadProtectedAssetSource(src, controller.signal)
        .then(({ url, release }) => {
          if (controller.signal.aborted) {
            release?.()
            return
          }
          releaseShown = release
          image.src = url
          // A cached page is already complete before the handlers were attached.
          if (image.complete && image.naturalWidth > 0) settle()
        })
        .catch((error) => {
          if (
            controller.signal.aborted ||
            (error instanceof DOMException && error.name === 'AbortError')
          ) {
            return
          }
          setFailedSrc(src)
        })
    }

    return () => {
      controller.abort()
      image.onload = null
      image.onerror = null
      if (!adopted) releaseShown?.()
    }
  }, [src, page, onDecoded, attempt])

  useEffect(
    () => () => {
      shown?.revoke?.()
    },
    [shown?.src, shown?.revoke],
  )

  if (failed) {
    return (
      <div role="alert" className="text-text-tertiary space-y-3 py-8 text-center text-sm">
        <p>That page could not be rendered.</p>
        <div className="flex flex-wrap justify-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setFailedSrc(null)
              setAttempt((value) => value + 1)
            }}
          >
            Retry page
          </Button>
          <Button variant="outline" size="sm" onClick={onReadText}>
            Read extracted text
          </Button>
        </div>
      </div>
    )
  }

  if (shown === null) {
    return <Skeleton className="aspect-[8.5/11] w-full rounded-[3px]" />
  }

  return (
    <div
      // Never taller than the pane it stands in. The column is sized to the width this page
      // wants, but it has floors and ceilings of its own — a minimum width, the share the
      // solutions column will not give up — and the difference has to go somewhere. Spent
      // on desk to either side of a whole page, rather than on a page grown past the fold:
      // a sheet the reader has to scroll to see the bottom of is the one thing this pane
      // exists to avoid.
      className="border-border/60 bg-paper-sheet relative mx-auto w-full overflow-hidden rounded-[3px] border shadow-md"
      style={
        shown.aspect > 0
          ? {
              aspectRatio: String(shown.aspect),
              width:
                availableWidth && availableWidth > 0
                  ? `${Math.min(availableWidth, availableHeight && availableHeight > 0 ? availableHeight * shown.aspect : availableWidth) * zoom}px`
                  : `${zoom * 100}%`,
              maxWidth: 'none',
            }
          : undefined
      }
    >
      {/* The sheet, with the bands riding on it: one layer for both, so a band is
          positioned against the page it marks rather than against the box around it. */}
      <div className="absolute inset-x-0 top-0">
        {/* The backend serves these at an unknown intrinsic size, so the packaged runtime
            fetches them with its authenticated API session and paints the decoded blob. */}
        <img src={shown.src} alt={`Page ${shown.page}`} className="block w-full" />
        {/* Laid over the page rather than drawn into it: the image is a faithful render of
            the student's own sheet, and marking it up would make the two columns disagree
            about what the sheet says. Percentages, because the page is rendered at whatever
            width the pane happens to have. Withheld while a turn is still in flight, so a
            band is never drawn over the page it does not belong to. */}
        {shown.page === page && onSelect
          ? bands.map((band) => (
              <button
                key={band.problemId}
                type="button"
                onClick={() => onSelect(band.problemId)}
                aria-label={`Go to the solution for ${band.label}`}
                title={band.label}
                style={{ top: `${band.from * 100}%`, height: `${(band.to - band.from) * 100}%` }}
                className={cn(
                  'absolute inset-x-0 border-l-2 transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
                  band.problemId === activeProblemId
                    ? 'border-accent-primary bg-accent-primary/8'
                    : 'border-transparent hover:border-accent-primary/60 hover:bg-accent-primary/6',
                )}
              />
            ))
          : null}
      </div>
    </div>
  )
}

function SourceText({ documentId, isPdf }: { documentId: number; isPdf: boolean }) {
  const text = useQuery({
    queryKey: ['document-text', documentId],
    queryFn: ({ signal }) => api.getDocumentText(documentId, signal),
  })

  if (text.isPending) return <Skeleton className="h-64 w-full rounded-md" />
  if (text.isError) {
    return (
      <div role="alert" className="text-text-tertiary space-y-3 py-8 text-center text-sm">
        <p>That document could not be read.</p>
        <Button
          variant="outline"
          size="sm"
          disabled={text.isFetching}
          onClick={() => void text.refetch()}
        >
          {text.isFetching ? 'Retrying…' : 'Retry text'}
        </Button>
      </div>
    )
  }

  return (
    <div className="text-text-secondary text-sm">
      {isPdf ? (
        <p className="text-text-tertiary mb-4 text-xs">
          Extracted document text. Page layout and diagrams are available in the page view.
        </p>
      ) : null}
      {text.data.text.trim() ? (
        <pre className="font-sans break-words whitespace-pre-wrap">{text.data.text}</pre>
      ) : (
        <p>No extracted text is available. Use the page view or open your original file.</p>
      )}
      {text.data.truncated ? (
        <p className="text-text-tertiary mt-4 text-xs">
          Only the first part of this document is shown here.
        </p>
      ) : null}
    </div>
  )
}
