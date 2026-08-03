'use client'

import { Check } from 'lucide-react'

import { cn } from '@/lib/utils'
import type { DocumentState } from '@/types'

/**
 * Verbs, not internal stage names. There are four rather than five because OCR is not in
 * Phase 1, so nothing sits between reading and splitting.
 */
const STEPS: { state: DocumentState; label: string; subtitle?: string }[] = [
  { state: 'parsing', label: 'Reading' },
  { state: 'chunking', label: 'Splitting' },
  { state: 'embedding', label: 'Indexing' },
  {
    state: 'extracting',
    label: 'Analyzing',
    subtitle: 'Reading your syllabus for dates and topics',
  },
]

const ORDER: DocumentState[] = ['pending', 'parsing', 'chunking', 'embedding', 'extracting']

type IngestionProgressProps = {
  state: DocumentState
  pagesDone: number
  pagesTotal: number | null
}

export function IngestionProgress({ state, pagesDone, pagesTotal }: IngestionProgressProps) {
  const activeIndex = ORDER.indexOf(state) - 1
  const active = STEPS[activeIndex]

  return (
    <div className="space-y-2" aria-live="polite">
      <ol className="grid grid-cols-4">
        {STEPS.map((step, index) => {
          const done = index < activeIndex
          const current = index === activeIndex
          return (
            <li
              key={step.state}
              className={cn(
                'relative flex min-w-0 flex-col items-center gap-1 text-center after:absolute after:top-2 after:left-1/2 after:h-px after:w-full after:bg-border last:after:hidden',
                done && 'after:bg-success-text',
              )}
            >
              <span
                className={cn(
                  'z-10 flex size-4 items-center justify-center rounded-full border bg-card text-[10px]',
                  done && 'border-success-text bg-success-fill text-success-text',
                  current && 'border-accent-primary bg-accent-surface text-accent-primary',
                  !done && !current && 'border-text-tertiary/50 text-text-tertiary',
                )}
                aria-hidden
              >
                {done ? <Check className="size-3" /> : null}
              </span>
              <span
                className={cn(
                  'min-w-0 text-[10px] leading-3',
                  done && 'text-success-text',
                  current && 'font-medium text-accent-primary',
                  !done && !current && 'text-text-tertiary',
                )}
              >
                {step.label}
              </span>
            </li>
          )
        })}
      </ol>

      <p className="text-text-tertiary text-xs">
        {active?.subtitle ?? 'Preparing this document'}
        {pagesTotal ? ` · page ${Math.max(pagesDone, 1)} of ${pagesTotal}` : ''}
      </p>
    </div>
  )
}
