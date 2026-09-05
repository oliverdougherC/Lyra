'use client'

import { FileWarning, MoreVertical, Pencil, Trash2 } from 'lucide-react'
import Link from '@/router/link'

import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { formatCount, formatRelativeTime } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { SolutionRead, SolutionState } from '@/types'

/** Where a run has got to, in words rather than internal state names. */
export const SOLUTION_STATE_LABELS: Record<SolutionState, string> = {
  pending: 'Queued',
  segmenting: 'Reading the problem set',
  awaiting_review: 'Waiting for you',
  solving: 'Solving',
  ready: 'Ready',
  failed: 'Could not finish',
  cancelled: 'Stopped',
}

/** The counts, said only when they are real. */
export function describeSolution(solution: SolutionRead): string {
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

type SolutionRowProps = {
  classId: number
  solution: SolutionRead
  /** Supplied where the row is managed rather than merely listed: the class hub. */
  onRename?: (solution: SolutionRead) => void
  onDelete?: (solution: SolutionRead) => void
}

/**
 * One solution set in a list.
 *
 * The whole row is the link, and the actions menu sits above it in the stacking order, so
 * a row stays one target while Rename and Delete stay independently clickable. The same
 * arrangement as a class row on the index, for the same reason.
 */
export function SolutionRow({ classId, solution, onRename, onDelete }: SolutionRowProps) {
  const waiting = solution.state === 'awaiting_review'
  const failed = solution.state === 'failed'
  const managed = Boolean(onRename || onDelete)

  return (
    <div className="group border-border bg-card hover:border-border-strong focus-within:border-border-strong relative flex items-center rounded-md border transition-colors">
      {/* The state is a column of the row, not a word on the title's line. Sat inside the
          two-line stack it aligned to the title's baseline, which on a two-line row reads
          as floating above centre rather than as the row's own status. */}
      <Link
        href={`/classes/${classId}/solutions/${solution.id}`}
        className={cn(
          'focus-visible:ring-ring flex min-w-0 flex-1 items-center gap-4 rounded-md py-3 pl-4 focus-visible:ring-2 focus-visible:outline-none',
          // The actions column already carries the right inset when it is there.
          managed ? 'pr-1' : 'pr-4',
        )}
      >
        <span className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="text-text-primary truncate font-medium">{solution.title}</span>
          <span className="text-text-tertiary truncate text-xs">
            {describeSolution(solution)} · {formatRelativeTime(solution.updated_at)}
          </span>
        </span>

        {/* Deliberately no verdict badge here. Verdicts are per problem, and a set that
            reached `ready` may hold problems that nothing checked; one badge on the row
            would claim a check for all of them. The badges live inside, per problem. */}
        {failed ? (
          <span className="text-danger-text inline-flex shrink-0 items-center gap-1.5 text-xs">
            <FileWarning className="size-3.5" aria-hidden />
            {SOLUTION_STATE_LABELS[solution.state]}
          </span>
        ) : (
          <span
            className={cn('shrink-0 text-xs', waiting ? 'text-info-text' : 'text-text-tertiary')}
          >
            {SOLUTION_STATE_LABELS[solution.state]}
          </span>
        )}
      </Link>

      {managed ? (
        <div className="shrink-0 pr-2">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                aria-label={`Actions for ${solution.title}`}
                className="size-8"
              >
                <MoreVertical />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {onRename ? (
                <DropdownMenuItem onSelect={() => onRename(solution)}>
                  <Pencil />
                  Rename
                </DropdownMenuItem>
              ) : null}
              {onDelete ? (
                <DropdownMenuItem variant="destructive" onSelect={() => onDelete(solution)}>
                  <Trash2 />
                  Delete
                </DropdownMenuItem>
              ) : null}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      ) : null}
    </div>
  )
}
