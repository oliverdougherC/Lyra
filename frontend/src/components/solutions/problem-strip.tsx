'use client'

import { Spinner } from '@/components/ui/spinner'
import { cn } from '@/lib/utils'
import type { SolutionPart, Verdict } from '@/types'

/**
 * The dot beside a problem number, which is the verdict at a glance.
 *
 * Deliberately quieter than `VerdictBadge`: this is a spine for navigating by, and a row
 * of ten coloured pills would read as ten warnings. The badge on the problem itself still
 * says the word, so colour is never carrying the meaning on its own.
 */
const DOTS: Record<Verdict, string> = {
  verified: 'bg-success-text',
  refuted: 'bg-danger-text',
  uncheckable: 'bg-border',
  unchecked: 'bg-border',
}

/** What the sheet calls a problem, cut down to what fits in a chip. */
export function chipLabel(label: string | null, ordinal: number): string {
  const number = label?.match(/\d+[a-z]?/i)?.[0]
  return number ?? label?.slice(0, 3) ?? String(ordinal + 1)
}

type ProblemStripProps = {
  problems: SolutionPart[]
  /** The problem currently under the top of the reading pane. */
  activeId: number | null
  onSelect: (problemId: number) => void
  /**
   * The problem the pointer is resting on, or null when it has left the strip.
   *
   * The magnifier trains on this, so running the pointer along the numbers scans the
   * document through them without committing to any of them.
   */
  onHover?: (problemId: number | null) => void
}

/**
 * Every problem in the set, as a row of numbered chips.
 *
 * A solutions document is read straight through, so the pane below this scrolls rather
 * than collapsing. That leaves getting to problem nine a matter of scrolling past eight,
 * which this fixes: the strip is the spine of the document, it says where you are, and it
 * takes one click to be somewhere else.
 */
export function ProblemStrip({ problems, activeId, onSelect, onHover }: ProblemStripProps) {
  if (problems.length < 2) return null

  return (
    <nav
      aria-label="Problems in this set"
      // Scrolls in its own right rather than wrapping: a second row here would push the
      // two panes' content out of line with each other.
      className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto"
      onMouseLeave={() => onHover?.(null)}
    >
      {problems.map((problem, index) => {
        const active = problem.id === activeId
        const working = problem.status === 'solving' || problem.status === 'verifying'
        return (
          <button
            key={problem.id}
            type="button"
            onClick={() => onSelect(problem.id)}
            onMouseEnter={() => onHover?.(problem.id)}
            // Keyboard reaches the same preview: tabbing the strip scans it the way the
            // pointer does, rather than the magnifier being a mouse-only affordance.
            onFocus={() => onHover?.(problem.id)}
            onBlur={() => onHover?.(null)}
            aria-current={active ? 'true' : undefined}
            title={problem.label ?? `Problem ${index + 1}`}
            className={cn(
              'focus-visible:ring-ring flex h-6 shrink-0 items-center gap-1.5 rounded-full px-2 text-xs tabular-nums transition-colors focus-visible:ring-2 focus-visible:outline-none',
              active
                ? 'bg-accent-secondary text-accent-secondary-foreground'
                : 'text-text-tertiary hover:bg-muted hover:text-foreground',
            )}
          >
            {working ? (
              <Spinner className="size-2.5" />
            ) : (
              <span
                aria-hidden
                className={cn('size-1.5 shrink-0 rounded-full', DOTS[problem.verdict])}
              />
            )}
            {chipLabel(problem.label, index)}
          </button>
        )
      })}
    </nav>
  )
}
