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
 * Color is never the only signal: the label always differs, and the icon differs too.
 *
 * `uncheckable` and `unchecked` deliberately look alike and read differently. Both are
 * honest non-answers and neither is a pass, which is the single rule this file exists to
 * hold: there is no presentation here that reads as agreement without one.
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
    className: 'text-text-tertiary border-border border',
    icon: Circle,
    explanation:
      'Nothing in this solution could be checked by calculation. This is normal for a proof or a conceptual answer.',
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
