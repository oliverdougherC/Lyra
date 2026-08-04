'use client'

import { useQueryClient } from '@tanstack/react-query'
import { useParams, useRouter } from 'next/navigation'
import { useEffect } from 'react'
import { toast } from 'sonner'

import { SegmentationReview } from '@/components/solutions/segmentation-review'
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
import {
  isSettled,
  solutionKeys,
  useCancelSolution,
  useDeleteSolution,
  useResegmentSolution,
  useSolution,
  useSolutionStatus,
} from '@/lib/hooks/use-solutions'

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
  const resegment = useResegmentSolution(artifactId ?? Number.NaN, classId ?? Number.NaN)
  const cancel = useCancelSolution(artifactId ?? Number.NaN, classId ?? Number.NaN)
  const remove = useDeleteSolution(classId ?? Number.NaN)

  // The poll is the live source of truth for state; the detail query carries the parts.
  // Refetching the detail when the state settles is what makes a run that finished while
  // the tab was closed appear without a reload.
  const polledState = status.data?.state
  useEffect(() => {
    if (polledState && isSettled(polledState) && artifactId !== null) {
      queryClient.invalidateQueries({ queryKey: solutionKeys.detail(artifactId) })
    }
  }, [polledState, artifactId, queryClient])

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

  const handleDelete = () =>
    remove.mutate(artifactId, {
      onSuccess: () => router.push(`/classes/${classId}`),
      onError: (error) =>
        toast.error(
          error instanceof ApiError ? error.message : 'Could not delete this solution set.',
        ),
    })

  return (
    <div className="mx-auto flex w-full max-w-[860px] flex-col gap-6">
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
            <Button variant="ghost" size="sm">
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
        />
      ) : null}

      {state === 'failed' ? (
        <div className="flex flex-col gap-4">
          <Alert variant="destructive">
            <AlertTitle>Lyra could not read this problem set</AlertTitle>
            <AlertDescription>
              {errorMessage ?? 'Something went wrong while reading it.'}
            </AlertDescription>
          </Alert>
          <div className="flex gap-2">
            <Button onClick={() => resegment.mutate()} disabled={resegment.isPending}>
              {resegment.isPending ? 'Trying again' : 'Try again'}
            </Button>
          </div>
        </div>
      ) : null}

      {state === 'cancelled' ? (
        <div className="flex flex-col gap-4">
          {/* Stopping is not a failure and is not styled as one. */}
          <Alert>
            <AlertTitle>You stopped this run</AlertTitle>
            <AlertDescription>
              Nothing was lost. Lyra can read the problem set again whenever you want.
            </AlertDescription>
          </Alert>
          <div className="flex gap-2">
            <Button onClick={() => resegment.mutate()} disabled={resegment.isPending}>
              {resegment.isPending ? 'Reading again' : 'Read it again'}
            </Button>
          </div>
        </div>
      ) : null}

      {state === 'solving' || state === 'ready' ? (
        <Alert>
          <AlertTitle>Solving is not built yet</AlertTitle>
          <AlertDescription>
            The problem list is confirmed. Generating solutions is the next thing Lyra learns to do.
          </AlertDescription>
        </Alert>
      ) : null}
    </div>
  )
}
