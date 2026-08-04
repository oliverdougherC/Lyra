'use client'

import { Printer } from 'lucide-react'
import { useMemo, useState } from 'react'
import { toast } from 'sonner'

import { MarkWrongDialog } from '@/components/solutions/mark-wrong-dialog'
import { ProblemPanel, type ProblemTree } from '@/components/solutions/problem-panel'
import { RevisionHistory } from '@/components/solutions/revision-history'
import { SourcePane } from '@/components/solutions/source-pane'
import { StepGuidePanel } from '@/components/solutions/step-guide-panel'
import { Accordion } from '@/components/ui/accordion'
import { Button } from '@/components/ui/button'
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from '@/components/ui/resizable'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { ApiError } from '@/lib/api'
import { formatCount } from '@/lib/format'
import { useDocuments } from '@/lib/hooks/use-documents'
import { useLocalStorageState } from '@/lib/hooks/use-local-storage-state'
import { useMediaQuery } from '@/lib/hooks/use-media-query'
import { useRegeneratePart } from '@/lib/hooks/use-solutions'
import type { SolutionDetail, SolutionPart } from '@/types'

/** Source left at 45%, solutions right at 55%, per the layout in docs/ui-phase-2.md. */
const DEFAULT_SPLIT = 45

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

function parseSplit(raw: string): number | null {
  const parsed = Number(raw)
  // A stored value outside the panes' own minimums would make one column unusable, so a
  // corrupt entry falls back rather than being honoured.
  return Number.isFinite(parsed) && parsed >= 25 && parsed <= 70 ? parsed : null
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
  const [split, setSplit] = useLocalStorageState(
    `lyra-solution-split-${classId}`,
    DEFAULT_SPLIT,
    parseSplit,
  )
  const [open, setOpen] = useState<string[]>([])
  const [askingAbout, setAskingAbout] = useState<SolutionPart | null>(null)
  const [markingWrong, setMarkingWrong] = useState<SolutionPart | null>(null)
  const [showingHistory, setShowingHistory] = useState<SolutionPart | null>(null)

  const documents = useDocuments(classId)
  const regenerate = useRegeneratePart(solution.id)
  const tree = useMemo(() => buildTree(solution.parts), [solution.parts])

  // The first problem opens by default; the rest stay collapsed so the outline is
  // readable. Derived once from the tree rather than held in state, so a problem landing
  // mid-solve does not collapse what the reader has open.
  const expanded = open.length > 0 ? open : tree[0] ? [String(tree[0].problem.id)] : []
  const selected = tree.find((node) => String(node.problem.id) === expanded[0])
  const anchor = selected ? anchorOf(selected.problem) : null

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
        <span className="text-text-tertiary text-xs tracking-wide uppercase">
          Solutions · {formatCount(tree.length, 'problem')}
        </span>
        <Button variant="ghost" size="sm" className="-mr-2 h-7" onClick={() => window.print()}>
          <Printer className="size-4" />
          Export
        </Button>
      </header>
      <ScrollArea className="min-h-0 flex-1">
        {/* The last problem needs somewhere to end. Matching the top padding left it
            touching the bottom edge of the pane. */}
        <div className="px-4 pt-2 pb-10">
          <Accordion
            type="multiple"
            value={expanded}
            onValueChange={(value) => setOpen(value.length > 0 ? value : [''])}
          >
            {tree.map((node) => (
              <ProblemPanel
                key={node.problem.id}
                node={node}
                onAsk={setAskingAbout}
                onMarkWrong={setMarkingWrong}
                onRegenerate={(problem) => handleRegenerate(problem, '')}
                onHistory={setShowingHistory}
                onRetry={(problem) => handleRegenerate(problem, '')}
              />
            ))}
          </Accordion>
        </div>
      </ScrollArea>
    </section>
  )

  const sourcePane = (
    <SourcePane sources={solution.sources} documents={documents.data ?? []} anchor={anchor} />
  )

  return (
    <>
      {/* Sized from what the shell has left rather than from a viewport calculation. The
          old `calc(100vh - 11rem)` encoded the header's height as a magic number, so it
          was wrong the moment the header changed, and it ignored the page's own padding
          in either direction. */}
      <div className="border-border bg-card min-h-[520px] flex-1 overflow-hidden rounded-lg border shadow-sm print:h-auto print:min-h-0 print:flex-none print:overflow-visible print:rounded-none print:border-0 print:shadow-none">
        {wide ? (
          <ResizablePanelGroup
            orientation="horizontal"
            defaultLayout={{ source: split, solutions: 100 - split }}
            // `onLayoutChanged` rather than `onLayoutChange`: the latter fires on every
            // pointer move, and this writes to localStorage.
            onLayoutChanged={(layout) => setSplit(shareOf(layout, 'source'))}
          >
            <ResizablePanel id="source" minSize="25" className="print:hidden">
              {sourcePane}
            </ResizablePanel>
            <ResizableHandle withHandle className="print:hidden" />
            <ResizablePanel id="solutions" minSize="30">
              {solutionPane}
            </ResizablePanel>
          </ResizablePanelGroup>
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

      <StepGuidePanel
        classId={classId}
        className={className}
        step={askingAbout}
        onClose={() => setAskingAbout(null)}
      />
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
