import { cn } from '@/lib/utils'

/**
 * The engraved lintel and its parts (design system section 6). Gold is engraving, never a
 * control and never in motion; the budget is two gilded moments per screen (the slab frame
 * plus one earned Mark), so these are used sparingly, at the top of a screen.
 */

/**
 * The dentil course: a row of small teeth in gold-line, the classical detail that runs
 * beneath a cornice. Printed, still, decorative. `aria-hidden` — it carries no information.
 */
export function Dentil({ className }: { className?: string }) {
  return (
    <div
      aria-hidden
      className={cn('h-1 w-full', className)}
      style={{
        // 4px teeth with 4px gaps, at 40% of the gold line so the course reads as a texture
        // rather than a rule.
        backgroundImage:
          'repeating-linear-gradient(90deg, color-mix(in srgb, var(--gold) 40%, transparent) 0 4px, transparent 4px 8px)',
        backgroundRepeat: 'repeat-x',
        backgroundPosition: 'bottom',
        maskImage: 'linear-gradient(90deg, transparent, black 8%, black 92%, transparent)',
      }}
    />
  )
}

/**
 * A hairline rule broken at center by a small laurel, the detail that sits under a nameplate.
 * The laurel and the rule are gold; the whole thing is `aria-hidden` ornament.
 */
export function LaurelRule({ className }: { className?: string }) {
  return (
    <div aria-hidden className={cn('flex items-center gap-2', className)}>
      <span className="bg-gold-line h-px flex-1" />
      <svg viewBox="0 0 44 16" fill="none" className="text-gold h-3.5 w-11 shrink-0">
        {/* Two symmetric sprigs meeting at the stem, three leaves each. */}
        <path d="M22 14 V4" stroke="currentColor" strokeWidth="1" strokeLinecap="round" />
        <g stroke="currentColor" strokeWidth="1" fill="none" strokeLinecap="round">
          <path d="M22 12 C18 12 15 10.5 14 8" />
          <path d="M22 9.5 C18.5 9.5 16 8 15 5.5" />
          <path d="M22 7 C19 7 17 5.5 16.5 3.5" />
          <path d="M22 12 C26 12 29 10.5 30 8" />
          <path d="M22 9.5 C25.5 9.5 28 8 29 5.5" />
          <path d="M22 7 C25 7 27 5.5 27.5 3.5" />
        </g>
        <circle cx="22" cy="3" r="1.2" fill="currentColor" />
      </svg>
      <span className="bg-gold-line h-px flex-1" />
    </div>
  )
}

/**
 * A nameplate: text cut into stone. Cinzel with the incised treatment (`.font-display`),
 * for names 17px and up — the wordmark, class names, workspace titles. Not for navigation
 * and not below 17px, where plain print caps do the work instead.
 */
export function Engraved({
  as: Tag = 'h2',
  className,
  children,
  ...props
}: {
  as?: React.ElementType
  className?: string
  children: React.ReactNode
} & React.HTMLAttributes<HTMLElement>) {
  return (
    <Tag className={cn('font-display text-text-primary', className)} {...props}>
      {children}
    </Tag>
  )
}
