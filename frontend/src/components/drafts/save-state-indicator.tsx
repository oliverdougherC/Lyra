import type { SaveStateName } from '@/lib/drafts/save-engine'
import { cn } from '@/lib/utils'

/**
 * saved, saving, dirty, error, conflict: whether the words on screen are the words on disk.
 *
 * The one honest signal in the writing desk. It may say `Saved` only when the server has
 * confirmed the newest known body at the version it holds; a refused stale write shows
 * `Changed elsewhere`, never `Saved` over a body the server no longer has (PLA-289).
 */
export function SaveStateIndicator({
  state,
  detail,
}: {
  state: SaveStateName
  detail: string | null
}) {
  const label =
    state === 'saved'
      ? 'Saved'
      : state === 'saving'
        ? 'Saving'
        : state === 'dirty'
          ? 'Unsaved changes'
          : state === 'conflict'
            ? 'Changed elsewhere'
            : 'Could not save'
  const alarming = state === 'error' || state === 'conflict'
  return (
    <span
      role="status"
      aria-live="polite"
      title={state === 'error' ? (detail ?? undefined) : undefined}
      className={cn(
        'flex items-center gap-1.5 text-xs',
        alarming ? 'text-danger-text' : 'text-text-tertiary',
      )}
    >
      <span
        aria-hidden
        className={cn(
          'size-1.5 rounded-full',
          state === 'saved' && 'bg-success-text',
          state === 'saving' && 'bg-accent-primary animate-pulse',
          state === 'dirty' && 'bg-text-tertiary',
          alarming && 'bg-danger-text',
        )}
      />
      {label}
    </span>
  )
}
