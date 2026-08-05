'use client'

import { Printer } from 'lucide-react'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'

import { FocusToggle } from '@/components/solutions/focus-toggle'
import { MarkWrongDialog } from '@/components/solutions/mark-wrong-dialog'
import { ProblemPanel, type ProblemTree } from '@/components/solutions/problem-panel'
import { ProblemStrip } from '@/components/solutions/problem-strip'
import { RevisionHistory } from '@/components/solutions/revision-history'
import { SourcePane, type ProblemRegion } from '@/components/solutions/source-pane'
import { StepThread } from '@/components/solutions/step-thread'
import { Accordion } from '@/components/ui/accordion'
import { Button } from '@/components/ui/button'
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from '@/components/ui/resizable'
import type { GroupImperativeHandle } from 'react-resizable-panels'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { ApiError } from '@/lib/api'
import { useDocuments } from '@/lib/hooks/use-documents'
import { useLocalStorageState } from '@/lib/hooks/use-local-storage-state'
import { useMediaQuery } from '@/lib/hooks/use-media-query'
import { useRegeneratePart } from '@/lib/hooks/use-solutions'
import type { SolutionDetail, SolutionPart } from '@/types'

/**
 * Where the split sits before the page has been measured, and the fallback for a document
 * whose page never renders. Once the first page is decoded the source column is sized to
 * fit that page whole, which is what a reader actually wants: a sheet they can see all of.
 */
const DEFAULT_SPLIT = 45

/**
 * The narrowest the source column may be, in pixels.
 *
 * A pixel floor rather than a share, because the column itself is now a pixel width: a
 * share floor of a quarter is 320px on a laptop and 860px on an ultrawide, and on the
 * ultrawide it would take 160px off the solutions column to show margin around a page that
 * already stood whole.
 */
const MIN_SOURCE_PX = 320

/** The most of the group the source column may take, so the solutions stay readable. */
const MAX_SPLIT = 70

/**
 * How wide the magnifier is allowed to push the document.
 *
 * Short of `MAX_SPLIT`: a band is far wider than it is tall, so it always asks for more
 * width than the window has and simply takes whatever ceiling it is given. At the full 70
 * the solutions beside it are squeezed into a column that wraps its own headings, and the
 * two are meant to be read together.
 */
const MAX_MAGNIFIED_SPLIT = 64

/**
 * One panel's share of the group, as a percentage.
 *
 * The library reports a layout as flex-grow values whose total is whatever the panels
 * happen to sum to, so the share is computed rather than read: assuming they add to 100
 * would silently drift.
 */
function shareOf(layout: Record<string, number>, id: string): number {
  const total = Object.values(layout).reduce((sum, value) => sum + value, 0)
  if (!total) return DEFAULT_SPLIT
  return Math.round(((layout[id] ?? 0) / total) * 100)
}

/**
 * A width the student dragged the source column to, in pixels. Zero means they never did.
 *
 * Pixels rather than a share of the window, because a share is not what a reader chooses
 * when they drag: they widen the column until the page looks right, and that is a size in
 * pixels which the window getting wider should leave alone. A stored value narrower than
 * the column's own minimum would make it unusable, so a corrupt entry falls back rather
 * than being honoured.
 */
function parseSourceWidth(raw: string): number | null {
  const parsed = Number(raw)
  return Number.isFinite(parsed) && parsed >= MIN_SOURCE_PX ? parsed : null
}

/** Whether the reader has asked not to be moved around. */
function prefersReducedMotion(): boolean {
  return (
    typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  )
}

/** How long a jump between problems takes to travel. Matches the house motion ceiling. */
const JUMP_DURATION_MS = 240

/** Breathing room left above a problem's heading once a jump has landed. */
const JUMP_GAP_PX = 8

function easeOutCubic(progress: number): number {
  return 1 - (1 - progress) ** 3
}

/**
 * Travel `viewport` to `top`, returning a function that abandons the trip.
 *
 * Animated frame by frame rather than through `scrollTo({ behavior: 'smooth' })`. Native
 * smooth scrolling is silently dropped in some environments — the container never moves at
 * all, verified here against a bare probe element — and a jump that quietly does nothing is
 * far worse than one that does not animate. A frame loop behaves the same everywhere, and
 * it can be abandoned the instant the reader takes the scroll back.
 */
function travelTo(viewport: HTMLElement, top: number): () => void {
  // A hidden document is served no animation frames at all, so an animated trip would
  // simply never depart and the reader would come back to find nothing had moved. Arriving
  // matters more than travelling; the same holds when stillness was asked for.
  if (prefersReducedMotion() || (typeof document !== 'undefined' && document.hidden)) {
    viewport.scrollTop = top
    return () => undefined
  }

  const from = viewport.scrollTop
  const distance = top - from
  if (distance === 0) return () => undefined

  let frame = 0
  let startedAt = 0

  const abandon = () => {
    if (frame) cancelAnimationFrame(frame)
    frame = 0
    viewport.removeEventListener('wheel', abandon)
    viewport.removeEventListener('touchstart', abandon)
    viewport.removeEventListener('keydown', abandon)
  }

  const step = (now: number) => {
    if (!startedAt) startedAt = now
    const progress = Math.min(1, (now - startedAt) / JUMP_DURATION_MS)
    viewport.scrollTop = from + distance * easeOutCubic(progress)
    if (progress < 1) frame = requestAnimationFrame(step)
    else abandon()
  }

  // Whatever the reader does with the scroll outranks where this was taking them.
  viewport.addEventListener('wheel', abandon, { passive: true })
  viewport.addEventListener('touchstart', abandon, { passive: true })
  viewport.addEventListener('keydown', abandon)
  frame = requestAnimationFrame(step)
  return abandon
}

type SolutionWorkspaceProps = {
  solution: SolutionDetail
  classId: number
  className: string
}

/**
 * The solution document: source left, solutions right.
 *
 * Below 1024px the panes become line tabs rather than splitting, because two 300px
 * columns are two columns nobody can read. The split is persisted per class, so a student
 * who widened the source pane for a diagram-heavy course keeps it.
 */
export function SolutionWorkspace({ solution, classId, className }: SolutionWorkspaceProps) {
  const wide = useMediaQuery('(min-width: 1024px)')
  // A width, not a share: see `parseSourceWidth`. The share-keyed entries the earlier keys
  // hold are deliberately abandoned rather than migrated — a share cannot say what the
  // window it was measured against was, so there is nothing in one to convert.
  const [chosenWidth, setChosenWidth] = useLocalStorageState(
    `lyra-solution-source-width-${classId}`,
    0,
    parseSourceWidth,
  )
  const groupRef = useRef<GroupImperativeHandle | null>(null)
  const groupElementRef = useRef<HTMLDivElement | null>(null)
  // The width the source column needs for its page to stand whole, in pixels, as measured
  // by the pane that renders it. Remeasured whenever the window changes height, because
  // that is what a page has to fit into.
  const [fitWidth, setFitWidth] = useState<number | null>(null)
  const [collapsed, setCollapsed] = useState<string[]>([])
  // Which pane, if either, has the window to itself. Not persisted: it is a thing you do
  // to read one page closely, not a layout you live in.
  const [focused, setFocused] = useState<'source' | 'solutions' | null>(null)
  // Crop the document to the problem in view and scale it up. Not persisted, like `focused`:
  // it is a way of reading one problem closely, not a layout to live in.
  const [magnified, setMagnified] = useState(false)
  const [hoveredId, setHoveredId] = useState<number | null>(null)
  // Whether the reader has dragged the split since the magnifier was last switched. Reset
  // during render rather than from an effect: the layout effect that applies the fit runs
  // before any passive effect could clear this, and would spend the commit the magnifier
  // turns on reading the previous session's answer.
  const [magnifyOverridden, setMagnifyOverridden] = useState(false)
  const [lastMagnified, setLastMagnified] = useState(magnified)
  if (lastMagnified !== magnified) {
    setLastMagnified(magnified)
    setMagnifyOverridden(false)
  }
  const [askingAbout, setAskingAbout] = useState<SolutionPart | null>(null)
  const [markingWrong, setMarkingWrong] = useState<SolutionPart | null>(null)
  const [showingHistory, setShowingHistory] = useState<SolutionPart | null>(null)

  const documents = useDocuments(classId)
  const regenerate = useRegeneratePart(solution.id)
  const tree = useMemo(() => buildTree(solution.parts), [solution.parts])
  const problems = useMemo(() => tree.map((node) => node.problem), [tree])

  // A solutions document is read straight through, so every problem is open and the pane
  // scrolls. Collapsing is still there for a set of fourteen, but it is a thing the reader
  // does rather than a state they have to undo. Held as the set that is *shut* so a
  // problem landing mid-solve arrives open rather than hidden.
  const expanded = useMemo(
    () => tree.map((node) => String(node.problem.id)).filter((id) => !collapsed.includes(id)),
    [collapsed, tree],
  )

  // Held in state as well as in a ref. The pane is rebuilt when the layout crosses the
  // 1024px split, so the scrolling element arrives after the first commit and an effect
  // that only read a ref would attach its listener to nothing and stay attached to it.
  const viewportRef = useRef<HTMLDivElement | null>(null)
  const [viewport, setViewport] = useState<HTMLDivElement | null>(null)
  const attachViewport = useCallback((node: HTMLDivElement | null) => {
    viewportRef.current = node
    setViewport(node)
  }, [])

  // Only the problems Lyra could actually find on a page. A set built from a text file,
  // or one whose marker did not turn up, simply has no bands, and the pane is what it was.
  const regions = useMemo(() => tree.flatMap((node) => regionOf(node.problem)), [tree])

  const [activeId, setActiveId] = useActiveProblem(
    viewport,
    solution.parts.length,
    tree[0]?.problem.id ?? null,
  )
  // Which problem the document is showing. Normally the one being read; while magnified,
  // the pointer may borrow it, so running along the strip scans the sheet through the
  // numbers. Only while magnified: unasked-for, a hover that turned the page would move the
  // document out from under someone who was only on their way to clicking something.
  const previewId = magnified ? hoveredId : null
  const shownId = previewId ?? activeId
  const shown = tree.find((node) => node.problem.id === shownId)
  const anchor = shown ? anchorOf(shown.problem) : null

  // The problem to scroll to once there is a pane to scroll. Clicking a band on the page
  // while the document has the window to itself has to bring the solutions back first, and
  // the pane it needs to scroll does not exist until it does.
  const pendingJump = useRef<number | null>(null)
  const [jumpVersion, setJumpVersion] = useState(0)

  const jumpTo = useCallback(
    (problemId: number) => {
      // Opened first: scrolling to a collapsed problem lands on a title with nothing under
      // it, which reads as an empty answer rather than a closed one.
      setCollapsed((current) => current.filter((id) => id !== String(problemId)))
      // Said outright rather than waited for. The reader has just named the problem they
      // want, so the strip and the source page should not hang back until a scroll event
      // confirms it.
      setActiveId(problemId)
      // Asking for a solution is asking to see it.
      setFocused((current) => (current === 'source' ? null : current))
      pendingJump.current = problemId
      setJumpVersion((version) => version + 1)
    },
    [setActiveId],
  )

  useLayoutEffect(() => {
    const problemId = pendingJump.current
    if (problemId === null || !viewport) return
    const item = viewport.querySelector(`[data-problem-id="${problemId}"]`)
    pendingJump.current = null
    if (!item) return
    // The heading, not the item that contains it: the item opens with the margin that
    // separates it from the problem above, and aiming at that put the title 56px down an
    // otherwise empty pane. Nothing above a heading is the point of jumping to it.
    const target = item.querySelector('[data-problem-heading]') ?? item
    // Positioned rather than left to `scrollIntoView`, which lands the problem hard against
    // the top of the pane and, in some browsers, does nothing at all when it is asked to do
    // it smoothly.
    const top =
      viewport.scrollTop +
      target.getBoundingClientRect().top -
      viewport.getBoundingClientRect().top -
      JUMP_GAP_PX
    // Travelled rather than cut to. A jump between two problems that look alike leaves the
    // reader to work out whether the pane moved at all and in which direction; the movement
    // itself is the answer. Returned as cleanup, so a second jump abandons the first rather
    // than two animations fighting over the same scrollTop.
    return travelTo(viewport, Math.max(0, top))
  }, [jumpVersion, viewport])

  /**
   * Size the source column so its page stands whole.
   *
   * Run as a layout effect, in the same commit that first paints the page image: the pane
   * reports its fit from the load that reveals the page, so the column is already the right
   * width by the time there is anything in it to see, and no resize is ever visible.
   *
   * Only the column's own reasons to change width run through here — a page of a different
   * shape, a window of a different height, the magnifier. A window that changed *width* is
   * not one of them: the source column keeps its pixels across that (see the panel's
   * `groupResizeBehavior`) and the solutions column spends the difference, which is the
   * only reading of a wider window that leaves the page exactly as readable as it was.
   */
  useLayoutEffect(() => {
    if (!wide || focused !== null) return
    const group = groupRef.current
    const total = groupElementRef.current?.clientWidth ?? 0
    if (!group || !total) return

    // Magnifying is an explicit request to make the document bigger, so it outranks a width
    // the reader dragged earlier — and switching it off hands that width straight back.
    // Unless they have since dragged *while* magnified, which is a choice about this view
    // and would otherwise be undone by the next render.
    const target = (magnified && !magnifyOverridden) || chosenWidth === 0 ? fitWidth : chosenWidth
    if (target === null) return
    const ceiling = magnified ? MAX_MAGNIFIED_SPLIT : MAX_SPLIT
    const width = Math.min(Math.max(target, MIN_SOURCE_PX), (total * ceiling) / 100)
    const share = Math.round((width / total) * 100)
    if (Math.abs(shareOf(group.getLayout(), 'source') - share) < 1) return
    group.setLayout({ source: share, solutions: 100 - share })
  }, [chosenWidth, fitWidth, focused, magnified, magnifyOverridden, wide])

  const handleRegenerate = (problem: SolutionPart, correction: string) => {
    regenerate.mutate(
      { partId: problem.id, correction },
      {
        onSuccess: () => {
          setMarkingWrong(null)
          toast.success(`Solving ${problem.label ?? 'that problem'} again.`)
        },
        onError: (error) =>
          toast.error(error instanceof ApiError ? error.message : 'Could not start that again.'),
      },
    )
  }

  const solutionPane = (
    <section aria-label="Solutions" className="flex h-full min-h-0 flex-col">
      {/* Same explicit height as the source pane's header, so the rule under the two
          columns is one line. Deriving it from this row's controls made this side taller
          by exactly the height of the Export button. */}
      <header className="border-border flex h-9 shrink-0 items-center justify-between gap-2 border-b px-4 lg:h-10 print:hidden">
        {/* The strip stands where the pane's title used to. A row of numbers says what it
            is more directly than the word "Solutions" did, and it says where you are. */}
        {tree.length > 1 ? (
          <ProblemStrip
            problems={problems}
            activeId={activeId}
            onSelect={jumpTo}
            onHover={setHoveredId}
          />
        ) : (
          <span className="text-text-tertiary text-xs tracking-wide uppercase">Solutions</span>
        )}
        <span className="flex shrink-0 items-center gap-1">
          <Button variant="ghost" size="sm" className="h-7 shrink-0" onClick={() => window.print()}>
            <Printer className="size-4" />
            Export
          </Button>
          {wide ? (
            <FocusToggle
              focused={focused === 'solutions'}
              pane="the solutions"
              onToggle={() => setFocused(focused === 'solutions' ? null : 'solutions')}
            />
          ) : null}
        </span>
      </header>
      <ScrollArea viewportRef={attachViewport} className="min-h-0 flex-1">
        {/* The last problem needs somewhere to end. Matching the top padding left it
            touching the bottom edge of the pane. */}
        <div className="px-4 pt-2 pb-10">
          <Accordion
            type="multiple"
            value={expanded}
            onValueChange={(value) =>
              setCollapsed(
                tree.map((node) => String(node.problem.id)).filter((id) => !value.includes(id)),
              )
            }
          >
            {tree.map((node, position) => (
              <ProblemPanel
                key={node.problem.id}
                node={node}
                index={position}
                onAsk={(step) =>
                  setAskingAbout((current) => (current?.id === step.id ? null : step))
                }
                onMarkWrong={setMarkingWrong}
                onRegenerate={(problem) => handleRegenerate(problem, '')}
                onHistory={setShowingHistory}
                onRetry={(problem) => handleRegenerate(problem, '')}
                askingAboutId={askingAbout?.id ?? null}
                thread={
                  askingAbout ? (
                    <StepThread
                      key={askingAbout.id}
                      classId={classId}
                      className={className}
                      step={askingAbout}
                      scrollViewportRef={viewportRef}
                      onClose={() => setAskingAbout(null)}
                    />
                  ) : null
                }
              />
            ))}
          </Accordion>
        </div>
      </ScrollArea>
    </section>
  )

  const sourcePane = (
    <SourcePane
      sources={solution.sources}
      documents={documents.data ?? []}
      anchor={anchor}
      regions={regions}
      activeProblemId={shownId}
      onSelectProblem={jumpTo}
      onFitWidth={setFitWidth}
      magnified={magnified}
      onMagnifiedChange={setMagnified}
      focusToggle={
        wide ? (
          <FocusToggle
            focused={focused === 'source'}
            pane="the document"
            onToggle={() => setFocused(focused === 'source' ? null : 'source')}
          />
        ) : null
      }
    />
  )

  return (
    <>
      {/* Sized from what the shell has left rather than from a viewport calculation. The
          old `calc(100vh - 11rem)` encoded the header's height as a magic number, so it
          was wrong the moment the header changed, and it ignored the page's own padding
          in either direction.

          No card. The two panes are the page: a border and a corner radius around them
          only said "this is a widget on a screen", and on a 13-inch laptop the widget was
          most of the screen anyway. */}
      {/* No `border-t`: the app header directly above already draws that rule with its own
          `border-b`, and stacking the two made the line 2px on this side of the window
          while the rail's was 1px. */}
      <div className="bg-background min-h-[420px] flex-1 overflow-hidden print:h-auto print:min-h-0 print:flex-none print:overflow-visible">
        {wide && focused === null ? (
          <ResizablePanelGroup
            orientation="horizontal"
            groupRef={groupRef}
            elementRef={groupElementRef}
            defaultLayout={{ source: DEFAULT_SPLIT, solutions: 100 - DEFAULT_SPLIT }}
            // `onLayoutChanged` rather than `onLayoutChange`: the latter fires on every
            // pointer move, and this writes to localStorage.
            onLayoutChanged={(layout, meta) => {
              // Only a drag is a choice. The library reports its own work under the same
              // callback — the fit applied above, the recompute after a window resize — and
              // storing that is exactly how the old key filled up with widths nobody picked.
              if (!meta.isUserInteraction) return
              const total = groupElementRef.current?.clientWidth ?? 0
              if (!total) return
              // Stops the fit from putting the magnified width straight back.
              setMagnifyOverridden(true)
              setChosenWidth(Math.round((shareOf(layout, 'source') / 100) * total))
            }}
          >
            {/* The document column is a width in pixels, and a window that grows or shrinks
                horizontally is not a reason to change it: the page inside was already the
                size it needed to be. The solutions column absorbs the whole difference,
                which is where a wider window is worth something. */}
            <ResizablePanel
              id="source"
              minSize={MIN_SOURCE_PX}
              groupResizeBehavior="preserve-pixel-size"
              className="print:hidden"
            >
              {sourcePane}
            </ResizablePanel>
            <ResizableHandle withHandle className="print:hidden" />
            <ResizablePanel id="solutions" minSize="30">
              {solutionPane}
            </ResizablePanel>
          </ResizablePanelGroup>
        ) : wide ? (
          // One pane, the whole width. On a small laptop this is the difference between
          // reading the sheet and squinting at a thumbnail of it.
          <div className="h-full">{focused === 'source' ? sourcePane : solutionPane}</div>
        ) : (
          // Tabs below 1024px, not a stack. Two half-height panes would leave neither one
          // tall enough to read, and the student is only ever reading one of them.
          <Tabs defaultValue="solutions" className="h-full gap-0">
            <TabsList variant="line" className="shrink-0 px-4 print:hidden">
              <TabsTrigger value="solutions">Solutions</TabsTrigger>
              <TabsTrigger value="source">Source</TabsTrigger>
            </TabsList>
            <TabsContent value="solutions" className="min-h-0 flex-1">
              {solutionPane}
            </TabsContent>
            <TabsContent value="source" className="min-h-0 flex-1 print:hidden">
              {sourcePane}
            </TabsContent>
          </Tabs>
        )}
      </div>

      <MarkWrongDialog
        key={markingWrong?.id ?? 'none'}
        problem={markingWrong}
        onClose={() => setMarkingWrong(null)}
        onSubmit={(correction) =>
          markingWrong ? handleRegenerate(markingWrong, correction) : undefined
        }
        pending={regenerate.isPending}
      />
      <RevisionHistory
        artifactId={solution.id}
        part={showingHistory}
        onClose={() => setShowingHistory(null)}
      />
    </>
  )
}

/** How far below the top of the pane a problem counts as the one being read. */
const READING_LINE_PX = 24

/**
 * The problem currently under the top of the reading pane.
 *
 * This is what makes the two columns one document rather than two: the source page follows
 * the solution being read, without the reader having to select anything. Measured from
 * scroll position rather than from which panel is open, because with every problem open
 * "which one is selected" is otherwise not a question the layout can answer.
 *
 * Args:
 *   viewport: The scrolling element the problems live in, or null before it mounts.
 *   partCount: Changes as a solve lands, which is when the offsets all move.
 *   fallback: What to report before anything has scrolled.
 *
 * Returns:
 *   The active problem, and a setter for naming one directly. The strip sets it on a
 *   click rather than waiting for the scroll it just started to report back.
 */
function useActiveProblem(
  viewport: HTMLDivElement | null,
  partCount: number,
  fallback: number | null,
): [number | null, (problemId: number) => void] {
  const [activeId, setActiveId] = useState<number | null>(null)

  useEffect(() => {
    if (!viewport) return

    let frame = 0
    const measure = () => {
      frame = 0
      const line = viewport.getBoundingClientRect().top + READING_LINE_PX
      let current: number | null = null
      viewport.querySelectorAll<HTMLElement>('[data-problem-id]').forEach((node) => {
        if (node.getBoundingClientRect().top <= line) current = Number(node.dataset.problemId)
      })
      // Above the first problem nothing has crossed the line yet, and reporting null there
      // would drop the source pane back to its first page every time the reader scrolled
      // to the top.
      if (current !== null) setActiveId(current)
    }
    const onScroll = () => {
      if (!frame) frame = requestAnimationFrame(measure)
    }

    measure()
    viewport.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      viewport.removeEventListener('scroll', onScroll)
      if (frame) cancelAnimationFrame(frame)
    }
  }, [partCount, viewport])

  return [activeId ?? fallback, setActiveId]
}

/**
 * Group the flat part list into problems with their sub-parts, steps, and answer.
 *
 * The backend returns a depth-first walk, so one pass is enough. Sub-parts are `problem`
 * kind under a problem: they are part of the question, not of the answer, and rendering
 * them as steps would present the question as working.
 */
export function buildTree(parts: SolutionPart[]): ProblemTree[] {
  const roots = parts.filter((part) => part.parent_part_id === null && part.kind === 'problem')
  return roots.map((problem) => {
    const children = parts.filter((part) => part.parent_part_id === problem.id)
    return {
      problem,
      subParts: children.filter((part) => part.kind === 'problem'),
      steps: children.filter((part) => part.kind === 'step'),
      answer: children.find((part) => part.kind === 'answer') ?? null,
    }
  })
}

/** Where in the source a problem was found, so the source pane can follow the selection. */
function anchorOf(problem: SolutionPart): { documentId: number; pageNumber: number | null } | null {
  const entry = problem.provenance.find((one) => one.document_id !== null)
  if (!entry?.document_id) return null
  return { documentId: entry.document_id, pageNumber: entry.page_number }
}

/** Where on its page a problem starts, for the band drawn over the page image. */
function regionOf(problem: SolutionPart): ProblemRegion[] {
  const entry = problem.provenance.find(
    (one) => one.document_id !== null && one.page_number !== null && one.bbox?.length === 4,
  )
  if (!entry?.document_id || entry.page_number === null || !entry.bbox) return []
  return [
    {
      problemId: problem.id,
      documentId: entry.document_id,
      page: entry.page_number,
      top: entry.bbox[1],
      label: problem.label ?? 'this problem',
    },
  ]
}
