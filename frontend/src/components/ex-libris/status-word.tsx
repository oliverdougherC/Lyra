import { cn } from '@/lib/utils'

/**
 * Status is a word, never a bare icon and never a bare color (design system sections 3.3,
 * 10). The nominal state is quiet print: "Ready", "Indexed", "Queued" render in muted ink,
 * so a screen where everything is fine reads as silence. Color marks only the exceptions
 * (the red-pencil family) and, sparingly, the student's own act in progress (the pen).
 *
 * An optional leading glyph may accompany the word for scanning, but the word is the status;
 * the glyph is `aria-hidden` decoration that never appears alone.
 */
type StatusTone = 'nominal' | 'active' | 'warn' | 'info'

const TONES: Record<StatusTone, string> = {
  // The quiet default. Everything nominal prints here.
  nominal: 'text-text-tertiary',
  // The student is doing this right now: the pen, not print.
  active: 'text-hand',
  // An exception that needs a red pencil.
  warn: 'text-danger-text',
  // A neutral fact that wants a little more presence than tertiary.
  info: 'text-info-text',
}

export function StatusWord({
  tone = 'nominal',
  icon,
  children,
  className,
}: {
  tone?: StatusTone
  /** Optional decorative leading glyph. Never rendered without the word beside it. */
  icon?: React.ReactNode
  children: React.ReactNode
  className?: string
}) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 text-xs font-medium tabular-nums',
        TONES[tone],
        className,
      )}
    >
      {icon ? (
        <span aria-hidden className="[&_svg]:size-3.5">
          {icon}
        </span>
      ) : null}
      <span>{children}</span>
    </span>
  )
}
