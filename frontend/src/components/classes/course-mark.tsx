import { initialsFor } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { ClassRead } from '@/types'

/**
 * Sage, tan, and muted clay are decorative only, so each is used with its paired
 * foreground token. The tone is keyed by class id, which makes a class keep the same
 * mark everywhere it appears.
 */
const COURSE_TONES = [
  'bg-accent-surface text-accent-surface-foreground',
  'bg-accent-secondary text-accent-secondary-foreground',
  'bg-accent-tertiary text-accent-tertiary-foreground',
] as const

const SIZES = {
  sm: 'size-6 rounded-[5px] text-[10px]',
  md: 'size-10 rounded-md text-sm',
} as const

export function courseToneFor(classId: number): string {
  return COURSE_TONES[classId % COURSE_TONES.length]
}

/** The rectangular class initials. Decorative: the class name is always beside it. */
export function CourseMark({
  klass,
  size = 'md',
  className,
}: {
  klass: Pick<ClassRead, 'id' | 'name' | 'code'>
  size?: keyof typeof SIZES
  className?: string
}) {
  return (
    <span
      aria-hidden
      className={cn(
        'flex shrink-0 items-center justify-center font-semibold tracking-tight',
        SIZES[size],
        courseToneFor(klass.id),
        className,
      )}
    >
      {initialsFor(klass.name, klass.code)}
    </span>
  )
}
