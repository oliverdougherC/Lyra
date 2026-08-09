import { cn } from '@/lib/utils'

/**
 * The Mark. A bare ring and check in verdigris beside the word "Verified" and a plain detail
 * ("SymPy, 2 calls"). It appears only when deterministic verification actually passed, and
 * verdigris plus this ring-and-check shape belong to machine verification alone, in either
 * mode — that exclusivity is what makes it a logo of trust rather than a decorative icon
 * (design system sections 3.3, 6). It fades in at 150ms; trust does not perform, so there is
 * no rotation, no gold, no theatrics.
 *
 * A non-passing verdict must NOT render this; it stays a printed word (see VerdictBadge).
 */
export function TheMark({
  detail,
  className,
  animate = true,
}: {
  /** The plain provenance detail, e.g. "SymPy, 2 calls". Optional. */
  detail?: string
  className?: string
  /** The 150ms fade-in on first reveal; off for static contexts like print. */
  animate?: boolean
}) {
  return (
    <span
      className={cn(
        'text-trust inline-flex items-center gap-1.5 text-xs font-medium',
        animate && 'the-mark-reveal',
        className,
      )}
    >
      <svg aria-hidden viewBox="0 0 24 24" fill="none" className="size-4 shrink-0">
        <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.75" />
        <path
          d="M8 12.2 L11 15 L16.2 9"
          stroke="currentColor"
          strokeWidth="1.75"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      <span>Verified</span>
      {detail ? (
        <span className="text-text-tertiary font-normal tabular-nums">· {detail}</span>
      ) : null}
    </span>
  )
}
