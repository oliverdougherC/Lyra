'use client'

import { AlertCircle, History, MoreHorizontal, RefreshCw, XCircle } from 'lucide-react'
import { Fragment } from 'react'

import { MathText } from '@/components/solutions/math-text'
import { chipLabel } from '@/components/solutions/problem-strip'
import { SolutionStep } from '@/components/solutions/solution-step'
import { ToolCallTrace } from '@/components/solutions/tool-call-trace'
import { VerdictBadge } from '@/components/solutions/verdict-badge'
import { AccordionContent, AccordionItem, AccordionTrigger } from '@/components/ui/accordion'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Spinner } from '@/components/ui/spinner'
import { formatCount } from '@/lib/format'
import { statementLeadIn } from '@/lib/statement'
import { cn } from '@/lib/utils'
import type { SolutionPart } from '@/types'

export type ProblemTree = {
  problem: SolutionPart
  /** Lettered sub-parts of the question, which are parts of the problem, not the answer. */
  subParts: SolutionPart[]
  steps: SolutionPart[]
  answer: SolutionPart | null
}

type ProblemPanelProps = {
  node: ProblemTree
  onAsk: (step: SolutionPart) => void
  onMarkWrong: (problem: SolutionPart) => void
  onRegenerate: (problem: SolutionPart) => void
  onHistory: (part: SolutionPart) => void
  onRetry: (problem: SolutionPart) => void
  /** The step a conversation is open on, or null. */
  askingAboutId?: number | null
  /** That conversation, rendered directly under the step it is about. */
  thread?: React.ReactNode
  /** Position in the set, for the number chip when the label carries no number. */
  index?: number
}

/**
 * One problem in the solution outline: its statement, its steps, and what checking said.
 *
 * A refuted solution is still shown in full. Hiding it would leave the student with
 * nothing, and they may well spot the error themselves; the refutation opens the body
 * instead, naming the check that disagreed.
 */
export function ProblemPanel({
  node,
  onAsk,
  onMarkWrong,
  onRegenerate,
  onHistory,
  onRetry,
  askingAboutId = null,
  thread = null,
  index = 0,
}: ProblemPanelProps) {
  const { problem, subParts, steps, answer } = node
  const label = problem.label ?? 'Problem'
  const solving = problem.status === 'solving' || problem.status === 'verifying'
  const grounded = steps.filter((step) => step.provenance.length > 0).length
  const leadIn = statementLeadIn(
    problem.content,
    subParts.map((part) => part.label),
  )

  return (
    <AccordionItem
      value={String(problem.id)}
      // Both an anchor for the strip to jump to and the marker the pane reads to work out
      // which problem is being looked at, which is what the source page follows.
      id={`problem-${problem.id}`}
      data-problem-id={problem.id}
      // Space above rather than a rule between: one problem ends where the next one's
      // heading begins, and a solutions document that runs continuously is very easy to
      // read straight past the seam of.
      className="scroll-mt-2 border-b-0 pt-8 first:pt-0 print:break-inside-avoid"
    >
      {/* Pinned while you are inside this problem. The strip says which number you are on;
          this says it in the reading column itself, where the eye already is. `bg-card`
          because it slides over the text underneath it. */}
      <div className="bg-background border-border sticky top-0 z-10 -mx-4 flex items-start gap-2 border-b px-4">
        <AccordionTrigger className="min-w-0 flex-1 py-3">
          <span className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
            {/* The same token the strip uses, so the spine and the document agree. */}
            <span
              className={cn(
                'flex size-6 shrink-0 items-center justify-center rounded-full text-xs font-medium tabular-nums',
                'bg-accent-secondary text-accent-secondary-foreground',
              )}
              aria-hidden
            >
              {chipLabel(problem.label, index)}
            </span>
            <span className="font-heading text-text-primary text-base tracking-tight">{label}</span>
            {solving ? (
              <span className="text-text-tertiary inline-flex items-center gap-1.5 text-xs">
                <Spinner className="size-3" />
                {problem.status === 'verifying' ? 'Checking' : 'Solving'}
              </span>
            ) : problem.status === 'failed' ? (
              <span className="text-danger-text inline-flex items-center gap-1.5 text-xs">
                <AlertCircle className="size-3.5" aria-hidden />
                Could not be solved
              </span>
            ) : (
              <VerdictBadge verdict={problem.verdict} detail={problem.verdict_detail} />
            )}
          </span>
        </AccordionTrigger>
        <ProblemMenu
          problem={problem}
          disabled={solving}
          onMarkWrong={onMarkWrong}
          onRegenerate={onRegenerate}
          onHistory={onHistory}
        />
      </div>

      {/* The rail runs the length of the problem, so where one ends and the next begins is
          a thing you can see rather than a thing you have to notice. */}
      <AccordionContent className="border-border/70 ml-3 flex flex-col gap-5 border-l pt-4 pl-5">
        {/* The statement is verbatim from the sheet, so it usually already contains the
            sub-part lines the model extracted. `statementLeadIn` cuts them off where the
            labels say the list began; `addsToStatement` covers what it could not cut.
            Between them, no sub-part is printed twice. */}
        <MathText className="text-text-secondary text-sm">{leadIn}</MathText>
        {leadIn !== problem.content || addsToStatement(subParts, problem.content) ? (
          <ul className="text-text-secondary flex flex-col gap-1 text-sm">
            {subParts.map((part) => (
              <li key={part.id} className="flex gap-2">
                <span className="text-text-tertiary shrink-0">{part.label}</span>
                <MathText className="min-w-0">{part.content}</MathText>
              </li>
            ))}
          </ul>
        ) : null}

        {problem.verdict === 'refuted' && problem.verdict_detail ? (
          // Never quiet. The badge says a check failed; this says which one and what it
          // returned, and it is about the solution rather than about the student.
          <p className="bg-danger-fill text-danger-foreground rounded-md px-3 py-2 text-sm">
            {problem.verdict_detail}
          </p>
        ) : null}

        {problem.status === 'failed' ? (
          <div className="flex flex-wrap items-center gap-3">
            <p className="text-danger-text text-sm">
              {problem.error_message ?? 'Something went wrong solving this problem.'}
            </p>
            <Button size="sm" variant="outline" onClick={() => onRetry(problem)}>
              Try again
            </Button>
          </div>
        ) : null}

        {steps.map((step, index) => (
          <Fragment key={step.id}>
            <SolutionStep
              step={step}
              index={index + 1}
              onAsk={onAsk}
              onHistory={onHistory}
              dimmed={solving}
              asking={askingAboutId === step.id}
            />
            {askingAboutId === step.id ? thread : null}
          </Fragment>
        ))}
        {answer ? (
          <Fragment>
            <SolutionStep
              step={answer}
              onAsk={onAsk}
              onHistory={onHistory}
              dimmed={solving}
              asking={askingAboutId === answer.id}
            />
            {askingAboutId === answer.id ? thread : null}
          </Fragment>
        ) : null}

        {steps.length === 0 && !answer && problem.status !== 'failed' ? (
          <p className={cn('text-text-tertiary text-sm')}>
            {solving ? 'Lyra is working on this one.' : 'Not solved yet.'}
          </p>
        ) : null}

        {/* Grounding is a count of steps carrying provenance, not a score, and it belongs
            beside the checks rather than under the title: read first it looks like a mark
            out of ten for work the student has not started reading yet. */}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <ToolCallTrace checks={problem.checks} />
          {steps.length > 0 ? (
            <span className="text-text-tertiary text-xs print:hidden">
              {grounded} of {formatCount(steps.length, 'step')} grounded in your material
            </span>
          ) : null}
        </div>
      </AccordionContent>
    </AccordionItem>
  )
}

/**
 * Whether the sub-part list says anything the statement above it does not.
 *
 * Compared on collapsed whitespace, because the statement carries the sheet's own line
 * breaks and the extracted sub-part does not. A single sub-part the statement omits is
 * enough to show the whole list: dropping only the duplicates would leave a list that
 * looks like it lost entries.
 */
function addsToStatement(subParts: SolutionPart[], statement: string): boolean {
  if (subParts.length === 0) return false
  const flattened = collapse(statement)
  return subParts.some((part) => !flattened.includes(collapse(part.content)))
}

function collapse(value: string): string {
  return value.replace(/\s+/g, ' ').trim()
}

function ProblemMenu({
  problem,
  disabled,
  onMarkWrong,
  onRegenerate,
  onHistory,
}: {
  problem: SolutionPart
  disabled: boolean
  onMarkWrong: (problem: SolutionPart) => void
  onRegenerate: (problem: SolutionPart) => void
  onHistory: (part: SolutionPart) => void
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className="text-text-tertiary mt-3 size-8 shrink-0 p-0 print:hidden"
          aria-label={`Actions for ${problem.label ?? 'this problem'}`}
        >
          <MoreHorizontal className="size-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem onSelect={() => onMarkWrong(problem)} disabled={disabled}>
          <XCircle className="size-4" />
          Mark wrong and re-solve
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => onRegenerate(problem)} disabled={disabled}>
          <RefreshCw className="size-4" />
          Regenerate
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => onHistory(problem)}>
          <History className="size-4" />
          History
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
