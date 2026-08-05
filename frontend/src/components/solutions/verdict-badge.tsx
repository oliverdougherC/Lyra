'use client'

import { AlertTriangle, Check, Circle } from 'lucide-react'
import Link from 'next/link'

import { HoverCard, HoverCardContent, HoverCardTrigger } from '@/components/ui/hover-card'
import { cn } from '@/lib/utils'
import type { Verdict } from '@/types'

type VerdictPresentation = {
  label: string
  className: string
  icon: typeof Check
  /** What this verdict means, said plainly. Shown on hover and printed in the export. */
  explanation: string
}

/**
 * Colour is never the only signal: every label is a different sentence, and the two that
 * share a colour do not share a weight.
 *
 * Three states, not two. `refuted` is the only one that went wrong. `unchecked` is the only
 * one where something was owed and did not arrive — an endpoint that cannot run tools, a
 * loop that timed out, calculations that failed — and it stays grey and open, because it is
 * a job unfinished. Between them sits `uncheckable`: a solution whose steps are prose, or
 * an argument, with nothing in it a calculation could settle. Nothing went wrong there, so
 * it is green, and it is the one thing a student can safely skip past.
 *
 * What green does *not* say here is "checked". `uncheckable` is outlined rather than
 * filled, its label is its own sentence, and its explanation opens by saying no calculation
 * was run. The rule the file exists to hold is unchanged and is narrower than it looks: no
 * presentation may claim a check that did not happen. Reading as "fine" is not that claim —
 * marking every prose proof in a set with a warning grey was, in the other direction,
 * telling the student something was wrong with work nobody had found anything wrong with.
 */
export const VERDICTS: Record<Verdict, VerdictPresentation> = {
  verified: {
    label: 'Checked',
    className: 'bg-success-fill text-success-text',
    icon: Check,
    explanation: 'Every check Lyra ran agreed with this solution.',
  },
  refuted: {
    label: 'Check failed',
    className: 'bg-danger-fill text-danger-text',
    icon: AlertTriangle,
    explanation: 'A check disagreed with this solution. It is shown in full so you can judge it.',
  },
  uncheckable: {
    label: 'Nothing to check',
    className: 'text-success-text border-success-text/35 border',
    icon: Check,
    explanation:
      'Nothing here could be settled by a calculation, so Lyra read the working through instead and found nothing to disagree with. This is the normal outcome for a proof or a conceptual answer.',
  },
  unchecked: {
    label: 'Not checked',
    className: 'text-text-tertiary border-border border',
    icon: Circle,
    explanation: 'Lyra did not check this solution.',
  },
}

type VerdictBadgeProps = {
  verdict: Verdict
  /** The backend's own sentence, which is more specific than the generic explanation. */
  detail?: string | null
  className?: string
}

export function VerdictBadge({ verdict, detail, className }: VerdictBadgeProps) {
  const presentation = VERDICTS[verdict]
  const Icon = presentation.icon
  const explanation = detail?.trim() || presentation.explanation

  return (
    <HoverCard openDelay={150}>
      <HoverCardTrigger asChild>
        <span
          className={cn(
            'inline-flex shrink-0 items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium',
            presentation.className,
            className,
          )}
        >
          <Icon className="size-3" aria-hidden />
          {presentation.label}
        </span>
      </HoverCardTrigger>
      <HoverCardContent align="end" className="text-text-secondary">
        <p>{explanation}</p>
        {verdict === 'unchecked' ? (
          <p className="mt-2">
            <Link href="/settings" className="text-accent-primary underline underline-offset-2">
              Check your endpoint settings
            </Link>
          </p>
        ) : null}
      </HoverCardContent>
    </HoverCard>
  )
}
