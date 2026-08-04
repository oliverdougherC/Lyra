'use client'

import { useQueryClient } from '@tanstack/react-query'
import { useParams, useRouter } from 'next/navigation'
import { useEffect, useRef } from 'react'
import { toast } from 'sonner'

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
import { useClasses } from '@/lib/hooks/use-classes'
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
  const solvedCount = countSolved(artifact)
  const workspace = state === 'ready' || state === 'cancelled' || solvedCount > 0
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

  return (
    <div
      className={
        state === 'ready' || state === 'solving' || state === 'cancelled'
          ? 'flex w-full flex-col gap-4'
          : 'mx-auto flex w-full max-w-[860px] flex-col gap-6'
      }
    >
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <h1 className="font-heading text-text-primary truncate text-2xl tracking-tight">
            {artifact.title}
          </h1>
          <p className="text-text-tertiary text-xs">
            {artifact.sources.map((source) => source.filename).join(', ')}
          </p>
        </div>
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
      </header>

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
        <SegmentationReview
          solution={artifact}
          onResegment={() => resegment.mutate()}
          resegmenting={resegment.isPending}
          onSolve={handleStart}
          solving={start.isPending}
        />
      ) : null}

      {state === 'solving' ? (
        // The strip, not the centered block: results land underneath it as they complete,
        // and the reader can be reading problem 1 while problem 7 is still running.
        <SolveProgress
          variant="strip"
          state={state}
          problemsTotal={problemsTotal}
          problemsDone={problemsDone}
          detail={stageDetail}
          onCancel={() => cancel.mutate()}
          cancelling={cancel.isPending}
        />
      ) : null}

      {state === 'failed' ? (
        <div className="flex flex-col gap-4">
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
        <div className="flex flex-col gap-4">
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
        <SolutionWorkspace solution={artifact} classId={classId} className={className} />
      ) : null}
    </div>
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
