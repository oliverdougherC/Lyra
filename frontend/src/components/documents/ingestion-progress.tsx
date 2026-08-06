'use client'

import { useEffect, useState } from 'react'
import { Check } from 'lucide-react'

import { formatCount } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { DocumentState } from '@/types'

/**
 * What the backend writes into `stage_detail` while it is reading pages as images.
 *
 * Text recognition is deliberately not a fifth step. A student does not have two concepts
 * here: there is text in the file or there is not, and either way what Lyra is doing is
 * reading the document. Splitting it out would ask the reader to learn the difference
 * between an extractable text layer and a transcribed one in order to watch a progress bar.
 */
export const RECOGNIZING_DETAIL = 'recognizing'

/**
 * How long a recognition run has to be waiting before the elapsed counter appears.
 *
 * Longer than the three seconds the solver and the chat loader use, on purpose. Those
 * report a wait that is unexpected; this one reports a wait that is expected to be minutes,
 * and a timer that starts ticking immediately reads as alarm rather than as reassurance.
 */
const ELAPSED_AFTER_MS = 10000

/**
 * Verbs, not internal stage names. There are four rather than five, and recognition renders
 * under Reading rather than adding a fifth.
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
  /** Pages settled so far. Only meaningful while recognition is running. */
  pagesDone?: number
  /** The backend's own note about what this stage is doing, when it has one. */
  stageDetail?: string | null
}

export function IngestionProgress({
  state,
  pagesTotal,
  pagesDone = 0,
  stageDetail,
}: IngestionProgressProps) {
  const activeIndex = ORDER.indexOf(state) - 1
  const active = STEPS[activeIndex]
  // The only stage whose per-page progress is real, and therefore the only one allowed a
  // counter. Parsing a text-based PDF takes under a second for 608 pages, so a counter
  // there would flash and mean nothing.
  const recognizing = stageDetail === RECOGNIZING_DETAIL && Boolean(pagesTotal)

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

      {/* A number that moves is shown; a number that would not move is not.
          `pages_done` is a tally over per-page rows only while recognition is running. Every
          other stage writes it once, at the very end, so a counter there would read
          "page 1 of 32" from the first second to the last and look like a stall. The
          document's size is what is known then, so the size is what is said. */}
      {recognizing ? <RecognitionCounter done={pagesDone} total={pagesTotal ?? 0} /> : null}
      {recognizing ? null : (
        <p className="text-text-tertiary text-xs">
          {active?.subtitle ?? 'Preparing this document'}
          {pagesTotal ? ` · ${formatCount(pagesTotal, 'page')}` : ''}
        </p>
      )}
    </div>
  )
}

/**
 * `Reading page 41 of 608`, advancing on polled state.
 *
 * No estimated finish and no bar. Recognition is seconds of model time a page and hardware
 * varies by an order of magnitude, so an estimate is a promise the machine cannot keep and
 * a countdown that overruns is worse than no countdown. The page it is on and how long it
 * has been going are what is actually known.
 */
function RecognitionCounter({ done, total }: { done: number; total: number }) {
  const elapsed = useElapsed()
  const page = Math.min(done + 1, total)

  return (
    <p className="text-text-tertiary text-xs tabular-nums">
      Reading page {page} of {total}
      {elapsed >= ELAPSED_AFTER_MS ? ` · ${formatElapsed(elapsed)}` : ''}
    </p>
  )
}

function useElapsed(): number {
  const [startedAt] = useState(() => Date.now())
  const [now, setNow] = useState(startedAt)

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  return now - startedAt
}

function formatElapsed(ms: number): string {
  const seconds = Math.floor(ms / 1000)
  if (seconds < 60) return `${seconds} seconds`

  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'}`

  // A 600-page book is hours of model time at the rate this runs, and "153 minutes" is a
  // number the reader has to convert before it means anything.
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  const hourPart = `${hours} hour${hours === 1 ? '' : 's'}`
  return rest === 0 ? hourPart : `${hourPart} ${rest} min`
}
