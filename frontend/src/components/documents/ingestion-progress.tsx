'use client'

import { Check } from 'lucide-react'

import { formatCount } from '@/lib/format'
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
    // Not "your syllabus". This stage runs over every upload, so it told a student
    // watching a lab handout that Lyra was reading their syllabus. What it actually does
    // is look for course facts wherever they happen to be, and most documents hold none,
    // which is a fine outcome and not worth misdescribing to avoid.
    subtitle: 'Looking for dates, topics, and course details',
  },
]

const ORDER: DocumentState[] = ['pending', 'parsing', 'chunking', 'embedding', 'extracting']

type IngestionProgressProps = {
  state: DocumentState
  pagesTotal: number | null
}

export function IngestionProgress({ state, pagesTotal }: IngestionProgressProps) {
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

      {/* The document's size, not a page counter. `pages_done` is written once, at the very
          end of the run, so it is zero for every frame this is on screen: the old line read
          "page 1 of 32" from the first second to the last, which looks like progress that
          has stalled. How long a document should take is worth saying; a number that never
          moves while claiming to is not. */}
      <p className="text-text-tertiary text-xs">
        {active?.subtitle ?? 'Preparing this document'}
        {pagesTotal ? ` · ${formatCount(pagesTotal, 'page')}` : ''}
      </p>
    </div>
  )
}
