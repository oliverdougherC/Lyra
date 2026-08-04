'use client'

import { FileWarning, Plus, SquareCheckBig } from 'lucide-react'
import Link from 'next/link'
import { useParams } from 'next/navigation'

import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { Skeleton } from '@/components/ui/skeleton'
import { ApiError } from '@/lib/api'
import { formatCount, formatRelativeTime } from '@/lib/format'
import { useSolutions } from '@/lib/hooks/use-solutions'
import type { SolutionRead, SolutionState } from '@/types'

function readId(value: string | string[] | undefined): number | null {
  const raw = Array.isArray(value) ? value[0] : value
  const parsed = Number(raw)
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null
}

/** Where a run has got to, in words rather than internal state names. */
const STATE_LABELS: Record<SolutionState, string> = {
  pending: 'Queued',
  segmenting: 'Reading the problem set',
  awaiting_review: 'Waiting for you',
  solving: 'Solving',
  ready: 'Ready',
  failed: 'Could not finish',
  cancelled: 'Stopped',
}

/**
 * The solver's front door.
 *
 * This page exists because the feature had nowhere to live. Its only entry point was a
 * sidebar sub-item below the conversation list, which meant a student who had not been
 * told about the solver had no way to find it and no way to learn what it was for. A
 * feature reachable only by people who already know it is there is not shipped.
 */
export default function SolutionsIndexPage() {
  const params = useParams<{ id: string }>()
  const classId = readId(params.id)
  const solutions = useSolutions(classId ?? Number.NaN)

  if (classId === null) {
    return (
      <Alert variant="destructive">
        <AlertTitle>That link is not valid</AlertTitle>
        <AlertDescription>Open a class from the list to see its solution sets.</AlertDescription>
      </Alert>
    )
  }

  return (
    <div className="mx-auto flex w-full max-w-[860px] flex-col gap-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <h1 className="font-heading text-text-primary text-2xl tracking-tight">Solutions</h1>
          <p className="text-text-secondary text-sm">
            Hand Lyra a problem set and it works through every problem, checks the mathematics, and
            shows you which steps came from your own course material.
          </p>
        </div>
        <Button asChild>
          <Link href={`/classes/${classId}/solutions/new`}>
            <Plus className="size-4" />
            New solution set
          </Link>
        </Button>
      </header>

      {solutions.isPending ? (
        <div className="flex flex-col gap-2" aria-busy="true">
          {[0, 1, 2].map((row) => (
            <Skeleton key={row} className="h-16 w-full rounded-md" />
          ))}
        </div>
      ) : solutions.isError ? (
        <Alert variant="destructive">
          <AlertTitle>Could not load your solution sets</AlertTitle>
          <AlertDescription>
            <p>
              {solutions.error instanceof ApiError
                ? solutions.error.message
                : 'Something went wrong.'}
            </p>
            <Button
              variant="outline"
              size="sm"
              className="mt-3"
              onClick={() => void solutions.refetch()}
            >
              Retry
            </Button>
          </AlertDescription>
        </Alert>
      ) : solutions.data.length === 0 ? (
        // Not a bare "nothing here". A student arriving with no solution sets is the
        // student most likely never to have used the solver, so this is the one place
        // that has to say what it does.
        <Empty className="py-12">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <SquareCheckBig className="text-text-tertiary size-8" />
            </EmptyMedia>
            <EmptyTitle>No solution sets yet</EmptyTitle>
            <EmptyDescription>
              Upload a homework PDF and Lyra reads it, shows you the problems it found so you can
              correct them, then solves each one and checks the result against a computer algebra
              system. You can edit any step, ask about it, or have a problem solved again.
            </EmptyDescription>
          </EmptyHeader>
          <Button asChild>
            <Link href={`/classes/${classId}/solutions/new`}>Solve a problem set</Link>
          </Button>
        </Empty>
      ) : (
        <ul className="flex flex-col gap-2">
          {solutions.data.map((solution) => (
            <li key={solution.id}>
              <SolutionRow classId={classId} solution={solution} />
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function SolutionRow({ classId, solution }: { classId: number; solution: SolutionRead }) {
  const waiting = solution.state === 'awaiting_review'
  const failed = solution.state === 'failed'

  return (
    <Link
      href={`/classes/${classId}/solutions/${solution.id}`}
      className="border-border bg-card hover:border-border-strong focus-visible:ring-ring flex flex-col gap-1 rounded-md border px-4 py-3 transition-colors focus-visible:ring-2 focus-visible:outline-none"
    >
      <span className="flex flex-wrap items-center gap-2">
        <span className="text-text-primary min-w-0 flex-1 truncate font-medium">
          {solution.title}
        </span>
        {failed ? (
          <span className="text-danger-text inline-flex items-center gap-1.5 text-xs">
            <FileWarning className="size-3.5" aria-hidden />
            {STATE_LABELS[solution.state]}
          </span>
        ) : (
          <span className={waiting ? 'text-info-text text-xs' : 'text-text-tertiary text-xs'}>
            {STATE_LABELS[solution.state]}
          </span>
        )}
        {/* Deliberately no verdict badge here. Verdicts are per problem, and a set that
            reached `ready` may hold problems that nothing checked; one badge on the row
            would claim a check for all of them. The badges live inside, per problem. */}
      </span>
      <span className="text-text-tertiary text-xs">
        {describe(solution)} · {formatRelativeTime(solution.updated_at)}
      </span>
    </Link>
  )
}

/** The counts, said only when they are real. */
function describe(solution: SolutionRead): string {
  const sources = solution.sources
    .filter((source) => source.role === 'problem_set')
    .map((source) => source.filename)
    .join(', ')
  if (solution.problems_total === null) return sources || 'No sources left'
  if (solution.state === 'solving') {
    return `${solution.problems_done} of ${solution.problems_total} solved`
  }
  return `${formatCount(solution.problems_total, 'problem')} · ${sources}`
}
