'use client'

import { useEffect, useState } from 'react'

import { LyraMark } from '@/components/chat/lyra-mark'
import { Penbar } from '@/components/ex-libris'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
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
  /**
   * `block` centers this in the workbench, for a wait with nothing to read yet. `strip`
   * puts it above results that are already landing, which is what solving looks like.
   * `band` is that same row once the workbench itself is on screen: a rule-bottomed bar
   * across the window rather than a card, because by then it is one of the bars the
   * workspace is already made of and a card floating over two panes reads as a dialog
   * that forgot to open.
   */
  variant?: 'block' | 'strip' | 'band'
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
  variant = 'block',
}: SolveProgressProps) {
  const elapsed = useElapsed()
  const counted = problemsTotal !== null && problemsTotal > 0
  const label = STAGE_LABELS[state] ?? 'Working'
  const headline =
    counted && state === 'solving'
      ? `Solving problem ${Math.min(problemsDone + 1, problemsTotal)} of ${problemsTotal}`
      : label

  if (variant === 'strip' || variant === 'band') {
    return (
      <div
        className={cn(
          'flex flex-wrap items-center gap-3 print:hidden',
          variant === 'band'
            ? // The same measurements as the panes' own headers below it — the rule, the
              // px-4 — so the three read as one piece of chrome rather than as a notice
              // resting on top of one. No card surface and no radius: nothing here is a
              // separate object, and the row leaves without a trace when the run ends.
              'border-border shrink-0 border-b px-4 py-2'
            : 'border-border bg-card rounded-lg border px-4 py-3',
        )}
      >
        <span className="text-accent-primary size-5 shrink-0">
          <LyraMark thinking />
        </span>
        <span className="flex min-w-0 flex-col">
          <span className="text-text-primary text-sm font-medium">{headline}</span>
          {detail ? <span className="text-text-tertiary truncate text-xs">{detail}</span> : null}
        </span>
        {counted ? (
          <Penbar
            value={problemsDone}
            total={problemsTotal}
            label={`${problemsDone} of ${problemsTotal} problems solved`}
            className="w-48"
          />
        ) : null}
        {elapsed >= ELAPSED_AFTER_MS ? (
          <span className="text-text-tertiary text-xs">{formatElapsed(elapsed)}</span>
        ) : null}
        <span className="flex-1" />
        {onCancel ? (
          <Button variant="outline" size="sm" onClick={onCancel} disabled={cancelling}>
            {cancelling ? 'Stopping' : 'Stop'}
          </Button>
        ) : null}
      </div>
    )
  }

  return (
    <div className="flex flex-col items-center gap-4 py-16 text-center">
      <span className="text-accent-primary size-8">
        <LyraMark thinking />
      </span>
      <div className="flex flex-col gap-1">
        <p className="text-text-primary text-base font-medium">{headline}</p>
        {detail ? <p className="text-text-secondary text-sm">{detail}</p> : null}
        {elapsed >= ELAPSED_AFTER_MS ? (
          <p className="text-text-tertiary text-xs">{formatElapsed(elapsed)}</p>
        ) : null}
      </div>
      {/* No bar until the count is real. A bar sitting at zero implies a denominator
          nobody has computed yet, which is a guess dressed as information. */}
      {counted ? (
        <Penbar
          value={problemsDone}
          total={problemsTotal}
          label={`${problemsDone} of ${problemsTotal} problems solved`}
          className="w-64"
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
