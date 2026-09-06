'use client'

import { useEffect, useRef, useState } from 'react'

import { useMediaQuery } from '@/lib/hooks/use-media-query'
import { cn } from '@/lib/utils'

/**
 * What Lyra shows between a question being sent and the first word arriving.
 *
 * This replaces a three-row stage tracker. On a machine that can run the model at all,
 * prompt assembly and retrieval finish in milliseconds, so a checklist of them spent a
 * card's worth of screen narrating work nobody waits for. What the reader actually waits
 * for is the model, and the honest report of that is one line: a loader that is visibly
 * running and a label naming the current stage.
 *
 * The stages are still tracked rather than dropped, because on a large class they stop
 * being instant, and a reader who waits deserves to know which part is slow.
 */

export type ProcessingStage = 'prompt_processing' | 'reviewing_documents' | 'composing_answer'

const STAGE_LABELS: Record<ProcessingStage, string> = {
  prompt_processing: 'Reading your question',
  reviewing_documents: 'Looking through your material',
  composing_answer: 'Thinking',
}

export function isProcessingStage(value: unknown): value is ProcessingStage {
  return typeof value === 'string' && value in STAGE_LABELS
}

export function stageLabel(stage: ProcessingStage | null): string {
  return stage ? STAGE_LABELS[stage] : STAGE_LABELS.composing_answer
}

/**
 * The `breathe` loader from the cli-loaders set: a braille cell that fills from one dot to
 * all eight and empties again. It is one character wide at every frame, so the label beside
 * it never shifts, and the two blank frames at the turn read as the pause between breaths.
 */
const BREATHE_FRAMES = [
  '⠀',
  '⠂',
  '⠌',
  '⡑',
  '⢕',
  '⢝',
  '⣫',
  '⣟',
  '⣿',
  '⣟',
  '⣫',
  '⢝',
  '⢕',
  '⡑',
  '⠌',
  '⠂',
  '⠀',
] as const

const BREATHE_INTERVAL_MS = 100

/** Below this the elapsed counter is noise; past it, silence starts to read as a hang. */
const ELAPSED_VISIBLE_AFTER_MS = 3000

/** The cell at full brightness, which is what a reader who has asked for stillness gets. */
const BREATHE_STILL = BREATHE_FRAMES.indexOf('⣿')

export function BreatheLoader({ className }: { className?: string }) {
  const reducedMotion = useMediaQuery('(prefers-reduced-motion: reduce)')
  const [tick, setTick] = useState(0)

  useEffect(() => {
    if (reducedMotion) return
    const timer = window.setInterval(() => setTick((current) => current + 1), BREATHE_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [reducedMotion])

  // A still loader is not a lie: the elapsed counter beside it is what reports progress.
  const frame = reducedMotion ? BREATHE_STILL : tick % BREATHE_FRAMES.length

  return (
    <span
      aria-hidden
      className={cn(
        'text-accent-primary w-[1ch] shrink-0 text-center font-mono text-base leading-none',
        className,
      )}
    >
      {BREATHE_FRAMES[frame]}
    </span>
  )
}

/**
 * Seconds since `startedAt`, ticking once a second, or null before the counter is worth
 * showing. The clock is only ever read from the interval, never resynchronised on the way
 * in, which keeps the wait honest without a render cascade at the start of every turn.
 */
function useElapsedSeconds(startedAt: number | null): number | null {
  const [now, setNow] = useState(0)

  useEffect(() => {
    if (startedAt === null) return
    const timer = window.setInterval(() => setNow(performance.now()), 250)
    return () => window.clearInterval(timer)
  }, [startedAt])

  if (startedAt === null) return null
  const elapsed = now - startedAt
  return elapsed < ELAPSED_VISIBLE_AFTER_MS ? null : Math.floor(elapsed / 1000)
}

type ThinkingIndicatorProps = {
  label: string
  /** When set, an elapsed counter appears once the wait is long enough to feel like one. */
  startedAt?: number | null
  className?: string
}

export function ThinkingIndicator(props: ThinkingIndicatorProps) {
  // Retry may reuse the streaming row. A fresh turn must never inherit its old label
  // or a pending throttled update.
  return <LiveThinkingIndicator key={props.startedAt ?? 'untracked'} {...props} />
}

const LABEL_MIN_INTERVAL_MS = 800

function LiveThinkingIndicator({ label, startedAt = null, className }: ThinkingIndicatorProps) {
  const [visibleLabel, setVisibleLabel] = useState(label)
  const lastChange = useRef(0)

  useEffect(() => {
    if (label === visibleLabel) return
    const delay = Math.max(0, LABEL_MIN_INTERVAL_MS - (performance.now() - lastChange.current))
    const timer = window.setTimeout(() => {
      lastChange.current = performance.now()
      setVisibleLabel(label)
    }, delay)
    return () => window.clearTimeout(timer)
  }, [label, visibleLabel])

  useEffect(() => {
    lastChange.current = performance.now()
  }, [])

  const seconds = useElapsedSeconds(startedAt)

  return (
    <div className={cn('flex items-center gap-2', className)} aria-busy="true">
      <BreatheLoader />
      <span className="min-w-0 text-sm">
        <span aria-live="polite">
          <span key={visibleLabel} className="lyra-shimmer lyra-label-enter">
            {visibleLabel}
          </span>
        </span>
        {seconds === null ? null : (
          <span aria-hidden className="text-text-tertiary ml-1.5 tabular-nums">
            {seconds}s
          </span>
        )}
      </span>
    </div>
  )
}
