'use client'

import { Plus, SquareCheckBig } from 'lucide-react'
import Link from 'next/link'
import { useParams } from 'next/navigation'

import { SolutionRow } from '@/components/solutions/solution-row'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { Skeleton } from '@/components/ui/skeleton'
import { ApiError } from '@/lib/api'
import { useSolutions } from '@/lib/hooks/use-solutions'

function readId(value: string | string[] | undefined): number | null {
  const raw = Array.isArray(value) ? value[0] : value
  const parsed = Number(raw)
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null
}

/**
 * What the solver will actually do, in the order it does it. The middle one is here
 * because it is the step nobody expects: solving stops and waits for the student to
 * check the problem list, and finding that out mid-run reads like a fault.
 */
const STEPS = [
  {
    title: 'Pick the files',
    body: 'The homework, and optionally worked solutions for Lyra to follow.',
  },
  {
    title: 'Check the problems',
    body: 'Lyra lists what it found. Merge, split, or edit anything it got wrong.',
  },
  {
    title: 'Read the solutions',
    body: 'Each problem lands as it finishes, with its working and its checks.',
  },
]

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

  // With nothing to list, the card below is the whole screen: it explains the feature and
  // starts it. Repeating that explanation and that button up here as well said the same
  // thing twice in the same glance.
  const listed = solutions.data !== undefined && solutions.data.length > 0

  return (
    <div className="mx-auto flex w-full max-w-[860px] flex-col gap-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <h1 className="font-heading text-text-primary text-2xl tracking-tight">Solutions</h1>
          {listed ? (
            <p className="text-text-secondary text-sm">
              Every problem set you have handed to Lyra in this class.
            </p>
          ) : null}
        </div>
        {listed ? (
          <Button asChild>
            <Link href={`/classes/${classId}/solutions/new`}>
              <Plus className="size-4" />
              New solution set
            </Link>
          </Button>
        ) : null}
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
        // that has to say what it does — and, since the solver takes a couple of minutes
        // and asks for a decision halfway through, what it is going to ask of them.
        <Empty className="gap-0 px-6 py-12">
          <EmptyHeader className="max-w-lg">
            <EmptyMedia
              variant="icon"
              className="bg-accent-surface text-accent-surface-foreground size-14 rounded-full"
            >
              <SquareCheckBig className="size-7" />
            </EmptyMedia>
            <EmptyTitle className="text-xl">Solve a problem set</EmptyTitle>
            <EmptyDescription>
              Hand Lyra a homework PDF. It works through every problem, checks the mathematics
              against a computer algebra system, and shows you which steps came from your own course
              material.
            </EmptyDescription>
          </EmptyHeader>

          {/* `text-pretty` because `Empty` sets `text-balance`, which is right for one
              centred paragraph and wrong for three short columns: it evens the line
              lengths and leaves every card ragged. */}
          <ol className="mt-8 grid w-full max-w-2xl gap-3 text-left text-pretty sm:grid-cols-3">
            {STEPS.map((step, index) => (
              <li key={step.title} className="border-border bg-muted/40 rounded-md border p-4">
                <span
                  aria-hidden
                  className="bg-accent-surface text-accent-surface-foreground flex size-6 items-center justify-center rounded-full text-xs font-medium tabular-nums"
                >
                  {index + 1}
                </span>
                <p className="text-text-primary mt-3 text-sm font-medium">{step.title}</p>
                <p className="text-text-tertiary mt-1 text-xs leading-relaxed">{step.body}</p>
              </li>
            ))}
          </ol>

          <Button asChild size="lg" className="mt-8">
            <Link href={`/classes/${classId}/solutions/new`}>Solve a problem set</Link>
          </Button>
          <p className="text-text-tertiary mt-3 text-xs">
            Every problem shows what was checked, and says so plainly when nothing could be.
          </p>
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
