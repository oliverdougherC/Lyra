'use client'

import { useEffect, useState } from 'react'

import { LyraMark } from '@/components/chat/lyra-mark'
import { Button } from '@/components/ui/button'
import { Progress } from '@/components/ui/progress'
import type { SolutionState } from '@/types'

/**
 * Verbs, not internal state names. Segmentation is a model pass over a whole document and
 * takes a minute on local hardware, so silence would read as a hang.
 */
const STAGE_LABELS: Partial<Record<SolutionState, string>> = {
  pending: 'Queued',
  segmenting: 'Reading your problem set',
  solving: 'Solving',
}

const ELAPSED_AFTER_MS = 3000

type SolveProgressProps = {
  state: SolutionState
  problemsTotal: number | null
  problemsDone: number
  /** What is being read or solved right now, when that is known. */
  detail?: string | null
  onCancel?: () => void
  cancelling?: boolean
}

/**
 * The visible answer to "is it working".
 *
 * Driven entirely by polled backend state. Nothing here advances on a timer and nothing
 * narrates a step that has not happened, which is why this is written rather than taken
 * off the shelf: canned loading components narrate fixed sequences on a timer, and a
 * progress bar that arrives before its data is a fiction.
 *
 * The one thing that does run on a clock is the elapsed counter, which is honest: it
 * reports how long the reader has been waiting, not how far the work has got.
 */
export function SolveProgress({
  state,
  problemsTotal,
  problemsDone,
  detail,
  onCancel,
  cancelling = false,
}: SolveProgressProps) {
  const elapsed = useElapsed()
  const counted = problemsTotal !== null && problemsTotal > 0
  const label = STAGE_LABELS[state] ?? 'Working'

  return (
    <div className="flex flex-col items-center gap-4 py-16 text-center">
      <span className="text-accent-primary size-8">
        <LyraMark thinking />
      </span>
      <div className="flex flex-col gap-1">
        <p className="text-text-primary text-base font-medium">
          {counted && state === 'solving'
            ? `Solving problem ${Math.min(problemsDone + 1, problemsTotal)} of ${problemsTotal}`
            : label}
        </p>
        {detail ? <p className="text-text-secondary text-sm">{detail}</p> : null}
        {elapsed >= ELAPSED_AFTER_MS ? (
          <p className="text-text-tertiary text-xs">{formatElapsed(elapsed)}</p>
        ) : null}
      </div>
      {/* No bar until the count is real. A bar sitting at zero implies a denominator
          nobody has computed yet, which is a guess dressed as information. */}
      {counted ? (
        <Progress
          value={(problemsDone / problemsTotal) * 100}
          className="w-64"
          aria-label={`${problemsDone} of ${problemsTotal} problems solved`}
        />
      ) : null}
      {onCancel ? (
        <Button variant="outline" size="sm" onClick={onCancel} disabled={cancelling}>
          {cancelling ? 'Stopping' : 'Stop'}
        </Button>
      ) : null}
    </div>
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
  return `${minutes} minute${minutes === 1 ? '' : 's'} ${seconds % 60} seconds`
}
