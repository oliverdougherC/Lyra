import { cn } from '@/lib/utils'

/**
 * A pen stroke under the word it marks: the active tab, the current nav item, a hovered row
 * (design system section 6). Slightly wobbled so it reads as drawn, not printed; it draws in
 * at 280ms and, per the note that has bitten twice, an absolutely positioned SVG keeps its
 * intrinsic width, so this one is explicitly `width:100%` and stretches to the word.
 *
 * `pathLength="1"` normalizes the stroke so the dasharray draw-in is geometry-independent.
 * Reduced motion lands it complete (globals.css). The stroke inherits `--hand` unless the
 * caller recolors it (a red-pencil flag passes `text-hand-red`).
 */
export function HandUnderline({
  className,
  animate = true,
}: {
  className?: string
  /** When false the stroke is present but static (a persistent underline, not a reveal). */
  animate?: boolean
}) {
  return (
    <svg
      aria-hidden
      viewBox="0 0 100 6"
      preserveAspectRatio="none"
      className={cn(
        // `size-full` (not `w-full`) so the class carries a `size-` token: some hosts, the
        // tab trigger among them, clamp any bare descendant svg to `size-4`, which would
        // shrink the underline to an icon. `!h-[6px]` then restores the stroke's height.
        'pointer-events-none absolute -bottom-1 left-0 size-full !h-[6px] overflow-visible text-hand',
        animate && 'hand-underline',
        className,
      )}
    >
      <path
        d="M1 3.4 C 20 1.6, 34 4.2, 52 2.8 S 82 1.8, 99 3.2"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        pathLength={1}
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  )
}
