'use client'

import { motion, useReducedMotion } from 'motion/react'

import { cn } from '@/lib/utils'

type BatchLoaderProps = {
  /** Current action, e.g. "Uploading notes.pdf" or "Indexing lecture 2". */
  title: string
  /** Secondary detail, e.g. "Reading your syllabus for dates and topics". */
  detail?: string | null
  /** Documents that reached a terminal state out of the whole batch. */
  processed: number
  total: number
  className?: string
}

/**
 * Rotating token-ring progress for a batch of uploads. The rings use solid sage and
 * clay borders instead of gradients so the loader stays inside the design-system
 * surface rules, and rotation stops entirely under reduced motion.
 */
export function BatchLoader({ title, detail, processed, total, className }: BatchLoaderProps) {
  const reduceMotion = useReducedMotion()
  const spin = reduceMotion ? { rotate: 0 } : { rotate: 360 }
  const percent = total > 0 ? Math.round((processed / total) * 100) : 0

  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        'flex items-center gap-3 rounded-md border bg-card p-3 text-sm shadow-sm',
        className,
      )}
    >
      <span className="relative size-9 shrink-0" aria-hidden>
        <span className="absolute inset-0 rounded-full border border-border" />
        <motion.span
          className="absolute inset-0 rounded-full border-2 border-transparent border-t-accent-primary border-r-accent-primary"
          animate={spin}
          transition={{ duration: 1.1, repeat: Infinity, ease: 'linear' }}
        />
        <motion.span
          className="absolute inset-1 rounded-full border border-transparent border-b-accent-tertiary border-l-accent-tertiary"
          animate={spin}
          transition={{ duration: 1.8, repeat: Infinity, ease: 'linear' }}
        />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate font-medium">{title}</span>
        {detail ? (
          <span className="text-muted-foreground block truncate text-xs">{detail}</span>
        ) : null}
      </span>
      <span className="text-text-tertiary shrink-0 text-xs tabular-nums">
        {processed} of {total}
      </span>
      <span aria-hidden className="bg-muted h-1 w-16 shrink-0 overflow-hidden rounded-full">
        <span
          className="bg-accent-primary block h-full rounded-full transition-[width] duration-300"
          style={{ width: `${percent}%` }}
        />
      </span>
    </div>
  )
}
