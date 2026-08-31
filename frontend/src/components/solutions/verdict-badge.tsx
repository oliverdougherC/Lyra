'use client'

import Link from '@/router/link'

import { StatusWord, TheMark } from '@/components/ex-libris'
import { HoverCard, HoverCardContent, HoverCardTrigger } from '@/components/ui/hover-card'
import { cn } from '@/lib/utils'
import type { Verdict } from '@/types'

/**
 * The phase's central honesty rule, in the one place a reader meets it: nothing that is not
 * a check may look like a pass. Under Ex Libris that rule sharpens into the Mark's
 * exclusivity (design system sections 3.3, 6). Only the passing verdict earns the verdigris
 * ring and check; every other verdict is a printed word, and the words carry the whole
 * distinction, since color no longer can.
 *
 * Three failure-shaped states, told apart by their sentences. `refuted` is the only one that
 * went wrong, so it takes a red pencil. `unchecked` is the only one where something was owed
 * and did not arrive, and it must not read as fine. Between them sits `uncheckable`: prose or
 * an argument with nothing a calculation could settle. Nothing went wrong there, so it reads
 * as quiet, but it still may not borrow the Mark or its color: reading as fine is not the
 * same as claiming a check.
 */
type VerdictTone = 'mark' | 'warn' | 'nominal'

type VerdictPresentation = {
  label: string
  tone: VerdictTone
  /** What this verdict means, said plainly. Shown on hover and printed in the export. */
  explanation: string
}

export const VERDICTS: Record<Verdict, VerdictPresentation> = {
  verified: {
    label: 'Verified',
    tone: 'mark',
    explanation: 'Every check Lyra ran agreed with this solution.',
  },
  refuted: {
    label: 'Check failed',
    tone: 'warn',
    explanation: 'A check disagreed with this solution. It is shown in full so you can judge it.',
  },
  uncheckable: {
    label: 'Nothing to check',
    tone: 'nominal',
    explanation:
      'Nothing here could be settled by a calculation, so Lyra read the working through instead and found nothing to disagree with. This is the normal outcome for a proof or a conceptual answer.',
  },
  unchecked: {
    label: 'Not checked',
    tone: 'nominal',
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
  const explanation = detail?.trim() || presentation.explanation

  return (
    <HoverCard openDelay={150}>
      <HoverCardTrigger asChild>
        <span className={cn('inline-flex shrink-0 cursor-default items-center', className)}>
          {presentation.tone === 'mark' ? (
            // The one verdict that passed: the Mark, and only here.
            <TheMark />
          ) : (
            <StatusWord tone={presentation.tone === 'warn' ? 'warn' : 'nominal'}>
              {presentation.label}
            </StatusWord>
          )}
        </span>
      </HoverCardTrigger>
      <HoverCardContent align="end" className="text-text-secondary">
        <p>{explanation}</p>
        {verdict === 'unchecked' ? (
          <p className="mt-2">
            <Link href="/settings" className="text-accent-text underline underline-offset-2">
              Check your endpoint settings
            </Link>
          </p>
        ) : null}
      </HoverCardContent>
    </HoverCard>
  )
}
