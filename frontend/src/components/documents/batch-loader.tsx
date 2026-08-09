'use client'

import { AlertCircle, Check } from 'lucide-react'

import { cn } from '@/lib/utils'

type BatchLoaderProps = {
  /** Current action, e.g. "Uploading notes.pdf" or "Indexing lecture 2". */
  title: string
  /** Secondary detail, e.g. "Reading your syllabus for dates and topics". */
  detail?: string | null
  /** Documents that reached a terminal state out of the whole batch. */
  processed: number
  /** Upload or ingestion failures included in the completed count. */
  failed: number
  /** Whether the batch has reached its terminal summary. */
  complete: boolean
  total: number
  className?: string
}

/**
 * Rotating token-ring progress for a batch of uploads. The rings use solid sage and
 * clay borders instead of gradients so the loader stays inside the design-system
 * surface rules, and rotation stops entirely under reduced motion.
 */
export function BatchLoader({
  title,
  detail,
  processed,
  failed,
  total,
  complete,
  className,
}: BatchLoaderProps) {
  const completed = processed + failed
  const percent = total > 0 ? Math.min(Math.round((completed / total) * 100), 100) : 0

  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        'flex items-center gap-3 rounded-md border bg-card p-3 text-sm shadow-sm',
        className,
      )}
    >
      {complete ? (
        <span
          aria-hidden
          className={cn(
            'flex size-9 shrink-0 items-center justify-center rounded-full border',
            failed === 0
              ? 'border-success-text/30 bg-success-fill text-success-text'
              : 'border-danger-text/30 bg-danger-fill text-danger-text',
          )}
        >
          {failed === 0 ? <Check className="size-4" /> : <AlertCircle className="size-4" />}
        </span>
      ) : (
        <span className="relative size-9 shrink-0" aria-hidden>
          <span className="border-border absolute inset-0 rounded-full border" />
          {/* Two rings turning at different rates, in CSS rather than a motion library: the
              rotation stops under reduced motion via `motion-safe`, matching the honest
              machinery rule that decoration never overrides a motion preference. */}
          <span
            className="border-t-accent-primary border-r-accent-primary absolute inset-0 rounded-full border-2 border-transparent motion-safe:animate-spin"
            style={{ animationDuration: '1.1s' }}
          />
          <span
            className="border-b-accent-tertiary border-l-accent-tertiary absolute inset-1 rounded-full border border-transparent motion-safe:animate-spin"
            style={{ animationDuration: '1.8s' }}
          />
        </span>
      )}
      <span className="min-w-0 flex-1">
        <span className="block truncate font-medium">{title}</span>
        {detail ? (
          <span className="text-muted-foreground block truncate text-xs">{detail}</span>
        ) : null}
      </span>
      <span className="text-text-tertiary shrink-0 text-xs tabular-nums">
        {completed} of {total}
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
