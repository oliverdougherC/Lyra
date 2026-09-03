'use client'

import { AlertCircle, ChevronRight, History, MoreHorizontal, RefreshCw, XCircle } from 'lucide-react'
import { Fragment } from 'react'

import { MathText } from '@/components/solutions/math-text'
import { chipLabel } from '@/components/solutions/problem-strip'
import { SolutionStep } from '@/components/solutions/solution-step'
import { ToolCallTrace } from '@/components/solutions/tool-call-trace'
import { VerdictBadge } from '@/components/solutions/verdict-badge'
import { AccordionContent, AccordionItem, AccordionTrigger } from '@/components/ui/accordion'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Spinner } from '@/components/ui/spinner'
import { FigureBlock } from '@/components/solutions/figure-block'
import { formatCount } from '@/lib/format'
import { statementLeadIn } from '@/lib/statement'
import { cn } from '@/lib/utils'
import type { SolutionPart, Verdict } from '@/types'

/** One lettered part of a question, with the solution it carries when it has one. */
export type SubPartTree = {
  problem: SolutionPart
  steps: SolutionPart[]
  answer: SolutionPart | null
}

export type ProblemTree = {
  problem: SolutionPart
  /** Lettered sub-parts of the question, which are parts of the problem, not the answer. */
  subParts: SubPartTree[]
  /**
   * Whether each of those sub-parts was solved as a question of its own. When it was,
   * the steps and the answer below are empty and each sub-part carries its own.
   */
  separate: boolean
  steps: SolutionPart[]
  answer: SolutionPart | null
  /** Diagrams from the source page, which belong to the question rather than the answer. */
  figures: SolutionPart[]
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
  const { problem, subParts, separate, steps, answer, figures } = node
  const section = sectionVerdict(subParts)
  const label = problem.label ?? 'Problem'
  const solving = problem.status === 'solving' || problem.status === 'verifying'
  const grounded = steps.filter((step) => step.provenance.length > 0).length
  const leadIn = statementLeadIn(
    problem.content,
    subParts.map((part) => part.problem.label),
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
      <div
        // What a jump aims at. The item's own box starts a whole `pt-8` above this, and
        // scrolling to that box left the title stranded 56px down the pane with nothing
        // above it — the gap that separates two problems, hoisted to the top of the screen
        // where it separates the problem from nothing at all.
        data-problem-heading
        className="bg-background border-border sticky top-0 z-10 -mx-4 flex items-start gap-2 border-b px-4"
      >
        {/* The chevron's own `translate-y-0.5` centres it against a 20px line of text,
            which is what an accordion header usually opens with. This one opens with the
            24px number chip, so it needs the extra 2px to sit on the same line as the
            title and as the menu button beside it. */}
        <AccordionTrigger className="min-w-0 flex-1 py-3 [&>svg]:translate-y-1">
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
            {separate ? (
              // Read off the parts, because the section itself is never solved and its
              // own row would say `pending` under five finished answers.
              section.solving ? (
                <span className="text-text-tertiary inline-flex items-center gap-1.5 text-xs">
                  <Spinner className="size-3" />
                  {formatCount(subParts.length, 'part')}
                </span>
              ) : (
                <VerdictBadge verdict={section.verdict} detail={section.detail} />
              )
            ) : solving ? (
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
        {/* A section holds no solution of its own, so there is nothing here to re-solve,
            mark wrong, or show a history of. Each part carries its own menu instead. */}
        {separate ? null : (
          <ProblemMenu
            problem={problem}
            disabled={solving}
            onMarkWrong={onMarkWrong}
            onRegenerate={onRegenerate}
            onHistory={onHistory}
          />
        )}
      </div>

      {/* The rail runs the length of the problem, so where one ends and the next begins is
          a thing you can see rather than a thing you have to notice. */}
      <AccordionContent className="border-border/70 ml-3 flex flex-col gap-5 border-l pt-4 pl-5">
        {/* The statement is verbatim from the sheet, so it usually already contains the
            sub-part lines the model extracted. `statementLeadIn` cuts them off where the
            labels say the list began; `addsToStatement` covers what it could not cut.
            Between them, no sub-part is printed twice. */}
        <MathText className="text-text-secondary text-sm">{leadIn}</MathText>
        {/* Only where the parts are read as one question. Where each is a question of its
            own, printing them here as a list and then again as headings below says the
            same five things twice, and the second time is the one carrying the answers. */}
        {!separate && (leadIn !== problem.content || addsToStatement(subParts, problem.content)) ? (
          <ul className="text-text-secondary flex flex-col gap-1 text-sm">
            {subParts.map(({ problem: part }) => (
              <li key={part.id} className="flex gap-2">
                <span className="text-text-tertiary shrink-0">{part.label}</span>
                <MathText className="min-w-0">{part.content}</MathText>
              </li>
            ))}
          </ul>
        ) : null}

        {/* Between the question and the work: a block diagram is what the question is
            about, so it reads before the first step rather than as an appendix. */}
        {figures.map((figure) => (
          <FigureBlock key={figure.id} figure={figure} />
        ))}

        {separate ? (
          subParts.map((part) => (
            <SolvedPart
              key={part.problem.id}
              node={part}
              onAsk={onAsk}
              onMarkWrong={onMarkWrong}
              onRegenerate={onRegenerate}
              onHistory={onHistory}
              onRetry={onRetry}
              askingAboutId={askingAboutId}
              thread={thread}
            />
          ))
        ) : (
          <Working
            problem={problem}
            steps={steps}
            answer={answer}
            solving={solving}
            grounded={grounded}
            onAsk={onAsk}
            onHistory={onHistory}
            onRetry={onRetry}
            askingAboutId={askingAboutId}
            thread={thread}
          />
        )}
      </AccordionContent>
    </AccordionItem>
  )
}

/**
 * One lettered question inside a section, with the solution it carries.
 *
 * A heading rather than a list row, because this part has a body: its own working, its
 * own answer, its own verdict, and its own menu. It is a problem in every way except
 * that the sentence asking it is the one printed above the section.
 */
function SolvedPart({
  node,
  onAsk,
  onMarkWrong,
  onRegenerate,
  onHistory,
  onRetry,
  askingAboutId,
  thread,
}: {
  node: SubPartTree
  onAsk: (step: SolutionPart) => void
  onMarkWrong: (problem: SolutionPart) => void
  onRegenerate: (problem: SolutionPart) => void
  onHistory: (part: SolutionPart) => void
  onRetry: (problem: SolutionPart) => void
  askingAboutId: number | null
  thread: React.ReactNode
}) {
  const { problem, steps, answer } = node
  const solving = problem.status === 'solving' || problem.status === 'verifying'
  const grounded = steps.filter((step) => step.provenance.length > 0).length

  return (
    <section
      // Addressable in its own right: this is what a click on the page image, a link, or
      // a jump from the strip can land on once a part is a question of its own.
      id={`part-${problem.id}`}
      className="flex flex-col gap-4 print:break-inside-avoid"
    >
      <div className="flex items-start gap-2">
        <span className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-2 gap-y-1">
          <span className="text-text-primary shrink-0 text-sm font-medium tabular-nums">
            {problem.label ?? 'Part'}
          </span>
          <MathText className="text-text-secondary min-w-0 text-sm">{problem.content}</MathText>
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
        <ProblemMenu
          problem={problem}
          disabled={solving}
          onMarkWrong={onMarkWrong}
          onRegenerate={onRegenerate}
          onHistory={onHistory}
        />
      </div>
      {/* Indented under its own heading, so a run of five questions reads as five and not
          as one long column of working. */}
      <div className="border-border/50 ml-1 flex flex-col gap-5 border-l pl-4">
        <Working
          problem={problem}
          steps={steps}
          answer={answer}
          solving={solving}
          grounded={grounded}
          onAsk={onAsk}
          onHistory={onHistory}
          onRetry={onRetry}
          askingAboutId={askingAboutId}
          thread={thread}
        />
      </div>
    </section>
  )
}

/**
 * The solution to one question: what disagreed with it, its steps, its answer, the checks
 * behind it.
 *
 * The same body under a problem and under one part of a split problem, because it is the
 * same thing in both places. What differs above it is the heading; what a student reads
 * below the heading should not be a second design.
 */
function Working({
  problem,
  steps,
  answer,
  solving,
  grounded,
  onAsk,
  onHistory,
  onRetry,
  askingAboutId,
  thread,
}: {
  problem: SolutionPart
  steps: SolutionPart[]
  answer: SolutionPart | null
  solving: boolean
  grounded: number
  onAsk: (step: SolutionPart) => void
  onHistory: (part: SolutionPart) => void
  onRetry: (problem: SolutionPart) => void
  askingAboutId: number | null
  thread: React.ReactNode
}) {
  return (
    <>
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

      <CheckedDisclosure problem={problem} steps={steps} grounded={grounded} />
    </>
  )
}

/**
 * The machinery behind a verdict, one disclosure deep.
 *
 * The worked answer is the primary content. What changes what the student should trust is
 * already on the page - a refutation is never quiet, a failed solve says so in red. What
 * does not is the audit itself: which checks ran, their raw arguments and results, and how
 * many steps the working was grounded in. That is the debugging surface, and it sits
 * under "How Lyra checked this" rather than in the reading flow.
 */
function CheckedDisclosure({
  problem,
  steps,
  grounded,
}: {
  problem: SolutionPart
  steps: SolutionPart[]
  grounded: number
}) {
  const checks = problem.checks
  const hasGrounding = steps.length > 0 && grounded > 0
  if (checks.length === 0 && !hasGrounding) return null

  return (
    <Collapsible className="print:hidden">
      <CollapsibleTrigger className="text-text-tertiary hover:text-text-secondary focus-visible:ring-ring flex items-center gap-1 rounded-sm text-xs focus-visible:ring-2 focus-visible:outline-none [&[data-state=open]>svg]:rotate-90">
        <ChevronRight className="size-3 transition-transform duration-200" aria-hidden />
        How Lyra checked this
        {checks.length > 0 ? (
          <span aria-hidden>· {formatCount(checks.length, 'check')} run</span>
        ) : null}
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-2 flex flex-col gap-3">
        {checks.length > 0 ? <ToolCallTrace checks={checks} /> : null}
        {hasGrounding ? (
          <span className="text-text-tertiary text-xs">
            {grounded} of {formatCount(steps.length, 'step')} grounded in your material
          </span>
        ) : null}
      </CollapsibleContent>
    </Collapsible>
  )
}

/**
 * What a section of separately solved parts is doing, and what checking made of it.
 *
 * A section is never solved itself, so its own row says `pending` while five questions
 * inside it are answered and checked. Everything the header shows about it is therefore
 * read off its parts.
 *
 * The verdict is the worst one among them, not an average and not a tally. A section
 * holding one refuted part is a section with something wrong in it, and a header that
 * called it `Checked` because four of five passed would be the one thing this project
 * does not do: claim a check that did not conclude what it appears to claim.
 */
function sectionVerdict(subParts: SubPartTree[]): {
  solving: boolean
  verdict: Verdict
  detail: string | null
} {
  const parts = subParts.map((part) => part.problem)
  const solving = parts.some(
    (part) => part.status === 'solving' || part.status === 'verifying' || part.status === 'pending',
  )
  const refuted = parts.filter((part) => part.verdict === 'refuted')
  if (refuted.length > 0) {
    const names = refuted.map((part) => part.label ?? 'a part').join(', ')
    return {
      solving,
      verdict: 'refuted',
      detail: `A check disagreed with ${names}. The rest of this problem is unaffected.`,
    }
  }
  for (const verdict of ['unchecked', 'uncheckable'] as const) {
    if (parts.some((part) => part.verdict === verdict)) {
      return { solving, verdict, detail: null }
    }
  }
  return { solving, verdict: 'verified', detail: null }
}

/**
 * Whether the sub-part list says anything the statement above it does not.
 *
 * Compared on collapsed whitespace, because the statement carries the sheet's own line
 * breaks and the extracted sub-part does not. A single sub-part the statement omits is
 * enough to show the whole list: dropping only the duplicates would leave a list that
 * looks like it lost entries.
 */
function addsToStatement(subParts: SubPartTree[], statement: string): boolean {
  if (subParts.length === 0) return false
  const flattened = collapse(statement)
  return subParts.some((part) => !flattened.includes(collapse(part.problem.content)))
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
          // `mt-2` puts this 32px button's centre on 24px, the centre of the header's
          // first line, which is where the collapse chevron sits too. The header is
          // `items-start`, so a title that wraps to two lines leaves both of them here
          // rather than dragging them down to the middle of the block.
          className="text-text-tertiary mt-2 size-8 shrink-0 p-0 print:hidden"
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
