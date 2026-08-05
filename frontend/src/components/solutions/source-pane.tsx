'use client'

import { ChevronLeft, ChevronRight, FileText } from 'lucide-react'
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton } from '@/components/ui/skeleton'
import { api, documentPageUrl } from '@/lib/api'
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
  /** The control that gives this pane the whole window, rendered in its header. */
  focusToggle?: React.ReactNode
}

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
  focusToggle = null,
}: SourcePaneProps) {
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
          {focusToggle}
        </span>
      </header>

      {/* The desk under the sheet: a slightly sunken tone, so the rendered page reads as
          a physical page lying on the workspace rather than a white rectangle in a form. */}
      <ScrollArea className="bg-muted/40 min-h-0 flex-1">
        <div className="p-5">
          {isPdf ? (
            // Keyed so a page change restarts the load rather than showing the previous
            // page while the next one arrives.
            <PageImage
              key={`${documentId}-${page}`}
              documentId={documentId}
              page={page}
              bands={onSelectProblem ? bandsOn(regions, documentId, page) : []}
              activeProblemId={activeProblemId}
              onSelect={onSelectProblem}
            />
          ) : (
            <SourceText documentId={documentId} />
          )}
        </div>
      </ScrollArea>

      {isPdf && pages > 1 ? (
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

function PageImage({
  documentId,
  page,
  bands,
  activeProblemId,
  onSelect,
}: {
  documentId: number
  page: number
  bands: ReturnType<typeof bandsOn>
  activeProblemId: number | null
  onSelect?: (problemId: number) => void
}) {
  const [state, setState] = useState<'loading' | 'ready' | 'failed'>('loading')

  if (state === 'failed') {
    return (
      <p className="text-text-tertiary py-8 text-center text-sm">
        That page could not be rendered.
      </p>
    )
  }

  return (
    <div className="relative">
      {state === 'loading' ? <Skeleton className="aspect-[8.5/11] w-full rounded-md" /> : null}
      {/* eslint-disable-next-line @next/next/no-img-element -- The backend serves these at
          an unknown intrinsic size and Next's loader would proxy a localhost-only route. */}
      <img
        src={documentPageUrl(documentId, page)}
        alt={`Page ${page}`}
        className={cn(
          'border-border/60 w-full rounded-[3px] border bg-white shadow-md',
          state !== 'ready' && 'hidden',
        )}
        onLoad={() => setState('ready')}
        onError={() => setState('failed')}
      />
      {/* Laid over the page rather than drawn into it: the image is a faithful render of
          the student's own sheet, and marking it up would make the two columns disagree
          about what the sheet says. Percentages, because the page is rendered at whatever
          width the pane happens to have. */}
      {state === 'ready' && onSelect
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
  )
}

function SourceText({ documentId }: { documentId: number }) {
  const text = useQuery({
    queryKey: ['document-text', documentId],
    queryFn: ({ signal }) => api.getDocumentText(documentId, signal),
  })

  if (text.isPending) return <Skeleton className="h-64 w-full rounded-md" />
  if (text.isError) {
    return (
      <p className="text-text-tertiary py-8 text-center text-sm">
        That document could not be read.
      </p>
    )
  }

  return (
    <div className="text-text-secondary text-sm">
      <pre className="font-sans break-words whitespace-pre-wrap">{text.data.text}</pre>
      {text.data.truncated ? (
        <p className="text-text-tertiary mt-4 text-xs">
          Only the first part of this document is shown here.
        </p>
      ) : null}
    </div>
  )
}
