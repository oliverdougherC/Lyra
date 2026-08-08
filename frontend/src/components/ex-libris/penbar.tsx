import { cn } from '@/lib/utils'

/**
 * Progress is a printed track filled by a single pen stroke, with plain numerals beside it
 * ("4 / 8") — nothing the student has to count (design system section 6). The stroke's
 * length is data, not animation: it is set from the fraction and a CSS transition eases
 * width changes, but motion-off and reduced-motion never alter where it lands. This is the
 * honest-machinery rule made visual — the bar can only ever show real progress.
 */
export function Penbar({
  value,
  total,
  label,
  showFraction = true,
  className,
  tone = 'hand',
}: {
  /** Completed units. Clamped to [0, total]. */
  value: number
  /** Total units. A non-positive total renders an empty track. */
  total: number
  /** Accessible name for the bar; defaults to the fraction. */
  label?: string
  showFraction?: boolean
  className?: string
  /** The stroke color: the pen by default, verdigris only where the Mark owns it. */
  tone?: 'hand' | 'trust'
}) {
  const safeTotal = total > 0 ? total : 0
  const clamped = Math.max(0, Math.min(value, safeTotal || value))
  const pct = safeTotal > 0 ? (clamped / safeTotal) * 100 : 0
  const fraction = `${clamped} / ${safeTotal}`

  return (
    <div className={cn('flex items-center gap-2.5', className)}>
      <div
        role="progressbar"
        aria-valuenow={clamped}
        aria-valuemin={0}
        aria-valuemax={safeTotal || undefined}
        aria-label={label ?? `Progress: ${fraction}`}
        className="border-border bg-bg-tertiary relative h-1.5 min-w-16 flex-1 overflow-hidden rounded-full border"
      >
        {/* The pen stroke. Width is the datum; the transition only eases a real change and is
            suppressed under reduced motion (globals.css `.penbar-fill`). */}
        <div
          className={cn(
            'penbar-fill absolute inset-y-0 left-0 rounded-full',
            tone === 'trust' ? 'bg-trust' : 'bg-hand',
          )}
          style={{ width: `${pct}%` }}
        />
      </div>
      {showFraction ? (
        <span className="text-text-secondary shrink-0 text-xs font-medium tabular-nums">
          {fraction}
        </span>
      ) : null}
    </div>
  )
}
