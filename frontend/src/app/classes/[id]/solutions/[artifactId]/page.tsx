'use client'

import { useQueryClient } from '@tanstack/react-query'
import { Maximize2, Minimize2 } from 'lucide-react'
import { useParams, useRouter } from '@/router/hooks'
import { useEffect, useRef } from 'react'
import { toast } from 'sonner'

import {
  HeaderActions,
  HeaderCrumb,
  useFullBleed,
  useImmersiveChrome,
} from '@/components/layout/page-chrome'
import { SegmentationReview } from '@/components/solutions/segmentation-review'
import { SolutionWorkspace } from '@/components/solutions/solution-workspace'
import { SolveProgress } from '@/components/solutions/solve-progress'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { ApiError } from '@/lib/api'
import { formatCount } from '@/lib/format'
import { cn } from '@/lib/utils'
import { useClasses } from '@/lib/hooks/use-classes'
import { useLocalStorageState } from '@/lib/hooks/use-local-storage-state'
import {
  solutionKeys,
  useCancelSolution,
  useDeleteSolution,
  useResegmentSolution,
  useSolution,
  useSolutionStatus,
  useStartSolution,
} from '@/lib/hooks/use-solutions'
import type { SolutionDetail } from '@/types'

function readId(value: string | string[] | undefined): number | null {
  const raw = Array.isArray(value) ? value[0] : value
  const parsed = Number(raw)
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null
}

export default function SolutionWorkspacePage() {
  const params = useParams<{ id: string; artifactId: string }>()
  const classId = readId(params.id)
  const artifactId = readId(params.artifactId)
  const router = useRouter()
  const queryClient = useQueryClient()

  const solution = useSolution(artifactId ?? Number.NaN)
  const status = useSolutionStatus(artifactId ?? Number.NaN)
  const classes = useClasses()
  const start = useStartSolution(artifactId ?? Number.NaN, classId ?? Number.NaN)
  const resegment = useResegmentSolution(artifactId ?? Number.NaN, classId ?? Number.NaN)
  const cancel = useCancelSolution(artifactId ?? Number.NaN, classId ?? Number.NaN)
  const remove = useDeleteSolution(classId ?? Number.NaN)

  // The poll is the live source of truth for state; the detail query carries the parts.
  // Refetching the detail on every change to what the poll reports is what makes a
  // solution land in the pane as it completes rather than on a reload.
  const polledState = status.data?.state
  const partSignature = status.data?.parts
    .map((part) => `${part.id}:${part.status}:${part.verdict}`)
    .join('|')
  useEffect(() => {
    if (artifactId === null) return
    queryClient.invalidateQueries({ queryKey: solutionKeys.detail(artifactId) })
  }, [polledState, partSignature, artifactId, queryClient])

  // A finished run confirms with a toast if the tab was still open, and only on the
  // transition: announcing it again on every poll would be noise.
  const announced = useRef<string | null>(null)
  useEffect(() => {
    if (!polledState || announced.current === null) {
      announced.current = polledState ?? null
      return
    }
    if (announced.current !== 'ready' && polledState === 'ready') {
      toast.success('Your solutions are ready.')
    }
    announced.current = polledState
  }, [polledState])

  // Derived before the guards below, because `useFullBleed` is a hook and cannot sit after
  // an early return. The solution route is a workspace only once there is something to
  // work in: segmenting and the review gate are ordinary centred pages.
  const loaded = solution.data
  const solved = loaded ? countSolved(loaded) : 0
  const currentState = polledState ?? loaded?.state
  const isWorkspace =
    loaded !== undefined && (currentState === 'ready' || currentState === 'cancelled' || solved > 0)
  useFullBleed(isWorkspace)
  // Whether the sidebar and header are folded away to give the workbench the whole window.
  // Kept per browser rather than per set: a reader who wants the room wants it on every
  // solution, and being asked again on the next one is the whole complaint. Gated on the
  // workbench being up so a stored `true` never strands a skeleton or the review gate with
  // no navigation on screen.
  const [immersive, setImmersive] = useLocalStorageState(
    'lyra-solution-immersive',
    false,
    parseImmersive,
  )
  useImmersiveChrome(immersive && isWorkspace)

  if (classId === null || artifactId === null) {
    return (
      <Alert variant="destructive">
        <AlertTitle>That link is not valid</AlertTitle>
        <AlertDescription>Open a solution set from your class workspace.</AlertDescription>
      </Alert>
    )
  }

  if (solution.isPending) {
    return (
      <div className="mx-auto flex w-full max-w-[860px] flex-col gap-4" aria-busy="true">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-4 w-96" />
        {[0, 1, 2].map((row) => (
          <Skeleton key={row} className="h-28 w-full rounded-md" />
        ))}
      </div>
    )
  }

  if (solution.isError) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load this solution set</AlertTitle>
        <AlertDescription>
          {solution.error instanceof ApiError ? solution.error.message : 'Something went wrong.'}
        </AlertDescription>
      </Alert>
    )
  }

  const artifact = solution.data
  // The poll is fresher than the detail query, so state comes from it when it has landed.
  const state = polledState ?? artifact.state
  const problemsTotal = status.data?.problems_total ?? artifact.problems_total
  const problemsDone = status.data?.problems_done ?? artifact.problems_done
  const errorMessage = status.data?.error_message ?? artifact.error_message
  const stageDetail = status.data?.stage_detail ?? artifact.stage_detail
  const solvedCount = solved
  const workspace = isWorkspace
  const className = classes.data?.find((entry) => entry.id === classId)?.name ?? 'Class'

  const handleDelete = () =>
    remove.mutate(artifactId, {
      onSuccess: () => router.push(`/classes/${classId}`),
      onError: (error) =>
        toast.error(
          error instanceof ApiError ? error.message : 'Could not delete this solution set.',
        ),
    })

  const handleStart = () =>
    start.mutate(undefined, {
      onError: (error) =>
        toast.error(error instanceof ApiError ? error.message : 'Could not start solving.'),
    })

  const deleteAction = (
    <AlertDialog>
      <AlertDialogTrigger asChild>
        <Button variant="ghost" size="sm" className="print:hidden">
          Delete
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete {artifact.title}?</AlertDialogTitle>
          <AlertDialogDescription>
            This removes the problem list and anything Lyra has solved so far. Your uploaded
            documents are not touched.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Keep it</AlertDialogCancel>
          <AlertDialogAction onClick={handleDelete}>Delete</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )

  return (
    <div
      className={
        workspace
          ? // No gap: what stacks here is the progress band and the workbench, and they
            // are meant to meet on a rule the way the panes below them do. Anything else
            // that can stand here carries its own padding.
            'flex min-h-0 w-full flex-1 flex-col'
          : state === 'solving'
            ? 'flex min-h-0 w-full flex-1 flex-col gap-3'
            : 'mx-auto flex w-full max-w-[860px] flex-col gap-6'
      }
    >
      {/* The title and Delete used to own a 48px row of their own above the panes. On a
          13-inch laptop that row cost more than the problem strip and said less, so the
          title is a breadcrumb and Delete sits in the header with it. */}
      {workspace ? (
        <>
          <HeaderCrumb>{artifact.title}</HeaderCrumb>
          <HeaderActions>{deleteAction}</HeaderActions>
        </>
      ) : (
        <header className="flex flex-wrap items-center justify-between gap-3">
          <h1 className="font-heading text-text-primary min-w-0 truncate text-2xl tracking-tight">
            {artifact.title}
          </h1>
          {deleteAction}
        </header>
      )}

      {state === 'pending' || state === 'segmenting' ? (
        <SolveProgress
          state={state}
          problemsTotal={problemsTotal}
          problemsDone={problemsDone}
          detail={artifact.sources[0]?.filename ?? null}
          onCancel={() => cancel.mutate()}
          cancelling={cancel.isPending}
        />
      ) : null}

      {state === 'awaiting_review' ? (
        // Padded here rather than by the column, which no longer spaces its children: a
        // set sent back to the review gate after part of it was solved stands above a
        // workbench that is already up, and only the progress band wants to touch it.
        <div className={cn('flex flex-col', workspace && 'shrink-0 p-4')}>
          <SegmentationReview
            solution={artifact}
            onResegment={() => resegment.mutate()}
            resegmenting={resegment.isPending}
            onSolve={handleStart}
            solving={start.isPending}
          />
        </div>
      ) : null}

      {state === 'solving' ? (
        // The strip, not the centered block: results land underneath it as they complete,
        // and the reader can be reading problem 1 while problem 7 is still running. And
        // once they have — once the workbench is up — a bar rather than a card, so the run
        // reports itself in the chrome instead of on top of the work.
        <SolveProgress
          variant={workspace ? 'band' : 'strip'}
          state={state}
          problemsTotal={problemsTotal}
          problemsDone={problemsDone}
          detail={stageDetail}
          onCancel={() => cancel.mutate()}
          cancelling={cancel.isPending}
        />
      ) : null}

      {state === 'failed' ? (
        <div className={cn('flex flex-col gap-4', workspace && 'shrink-0 p-4')}>
          <Alert variant="destructive">
            <AlertTitle>{failureHeading(artifact)}</AlertTitle>
            <AlertDescription>
              {errorMessage ?? 'Something went wrong while working on it.'}
            </AlertDescription>
          </Alert>
          <div className="flex gap-2">
            <Button onClick={handleStart} disabled={start.isPending}>
              {start.isPending ? 'Trying again' : 'Try again'}
            </Button>
            <Button
              variant="outline"
              onClick={() => resegment.mutate()}
              disabled={resegment.isPending}
            >
              {resegment.isPending ? 'Reading again' : 'Read the problem set again'}
            </Button>
          </div>
        </div>
      ) : null}

      {state === 'cancelled' ? (
        <div className={cn('flex flex-col gap-4', workspace && 'shrink-0 p-4')}>
          {/* Stopping is not a failure and is not styled as one. */}
          <Alert>
            <AlertTitle>
              {solvedCount > 0 && problemsTotal
                ? `Stopped at problem ${Math.min(solvedCount + 1, problemsTotal)} of ${problemsTotal}`
                : 'You stopped this run'}
            </AlertTitle>
            <AlertDescription>
              {solvedCount > 0
                ? `${formatCount(solvedCount, 'problem')} finished before you stopped, and they are below.`
                : 'Nothing was lost. Lyra can pick this up again whenever you want.'}
            </AlertDescription>
          </Alert>
          <div className="flex gap-2 print:hidden">
            <Button onClick={handleStart} disabled={start.isPending}>
              {solvedCount > 0 ? 'Solve the rest' : 'Solve'}
            </Button>
          </div>
        </div>
      ) : null}

      {workspace ? (
        // The immersive toggle rides in the workbench rather than the app header: immersive
        // mode folds that header away, and a control that hides itself the moment it is used
        // is one the reader cannot use to come back. The pane header stays on screen in both
        // states, so the way out is always where the way in was.
        <SolutionWorkspace
          solution={artifact}
          classId={classId}
          className={className}
          immersiveToggle={
            <ImmersiveToggle immersive={immersive} onToggle={() => setImmersive(!immersive)} />
          }
        />
      ) : null}
    </div>
  )
}

function parseImmersive(raw: string): boolean | null {
  return raw === 'true' ? true : raw === 'false' ? false : null
}

/**
 * Give the window to the work, or hand the application back.
 *
 * The sidebar and the header are two borders and a row of somewhere else to be, and a set
 * of solutions is read beside the sheet it came from - a page to look at rather than a
 * screen to navigate. This slides both away and keeps the two panes, which is what the
 * writing desk's own button does. It stays on screen in both states, so the mode is never
 * something a reader has to guess their way out of.
 */
function ImmersiveToggle({ immersive, onToggle }: { immersive: boolean; onToggle: () => void }) {
  const label = immersive ? 'Show the sidebar and header' : 'Hide the sidebar and header'
  return (
    <Button
      variant="ghost"
      size="icon"
      className="text-text-tertiary hover:text-text-primary size-8 print:hidden"
      onClick={onToggle}
      aria-pressed={immersive}
      aria-label={label}
      title={label}
    >
      {immersive ? <Minimize2 className="size-4" /> : <Maximize2 className="size-4" />}
    </Button>
  )
}

/** How many top-level problems have finished, which is what a stopped run reports. */
function countSolved(artifact: SolutionDetail): number {
  return artifact.parts.filter(
    (part) => part.parent_part_id === null && part.kind === 'problem' && part.status === 'complete',
  ).length
}

/** Names the stage in plain words, so a failure says what failed rather than that it did. */
function failureHeading(artifact: SolutionDetail): string {
  return artifact.stage_detail === 'solving'
    ? 'Lyra could not finish solving this set'
    : 'Lyra could not read this problem set'
}
