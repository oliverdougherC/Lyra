'use client'

import { Check, FileText, FileWarning } from 'lucide-react'

import { Skeleton } from '@/components/ui/skeleton'
import { formatFileSize, truncateMiddle } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { DocumentRead } from '@/types'

const STAGE_LABELS: Record<string, string> = {
  pending: 'Queued',
  parsing: 'Reading',
  chunking: 'Splitting',
  embedding: 'Indexing',
  extracting: 'Analyzing',
}

type SourcePickerProps = {
  documents: DocumentRead[]
  loading: boolean
  selected: readonly number[]
  onToggle: (documentId: number) => void
  /** Documents already claimed by the other picker, shown but not selectable here. */
  claimed?: readonly number[]
  emptyLabel: string
  name: string
}

/**
 * A checkbox list over the class's documents.
 *
 * Documents that cannot be read are shown rather than filtered out. A student who just
 * dropped a file needs to see it in the list and understand why it is not available yet;
 * omitting it looks like the upload was lost.
 */
export function SourcePicker({
  documents,
  loading,
  selected,
  onToggle,
  claimed = [],
  emptyLabel,
  name,
}: SourcePickerProps) {
  if (loading) {
    return (
      <div className="flex flex-col gap-2" aria-busy="true">
        {[0, 1, 2].map((row) => (
          <Skeleton key={row} className="h-14 w-full rounded-md" />
        ))}
      </div>
    )
  }

  if (documents.length === 0) {
    return <p className="text-text-tertiary text-sm">{emptyLabel}</p>
  }

  return (
    <ul className="flex flex-col gap-2">
      {documents.map((document) => {
        const isSelected = selected.includes(document.id)
        const isClaimed = claimed.includes(document.id)
        const ready = document.state === 'ready'
        const disabled = !ready || isClaimed
        return (
          <li key={document.id}>
            <label
              className={cn(
                'border-border bg-card flex cursor-pointer items-center gap-3 rounded-md border px-3 py-2.5 transition-colors',
                'has-[:focus-visible]:ring-ring has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-offset-2',
                isSelected && 'border-accent-primary bg-accent-surface/40',
                disabled && 'cursor-not-allowed opacity-60',
              )}
            >
              <input
                type="checkbox"
                name={name}
                className="sr-only"
                checked={isSelected}
                disabled={disabled}
                onChange={() => onToggle(document.id)}
              />
              <span
                aria-hidden
                className={cn(
                  'border-border-strong flex size-5 shrink-0 items-center justify-center rounded-sm border',
                  isSelected && 'border-accent-primary bg-accent-primary',
                )}
              >
                {isSelected ? <Check className="text-accent-foreground size-3.5" /> : null}
              </span>
              {document.state === 'unsupported' ? (
                <FileWarning className="text-info-text size-4 shrink-0" aria-hidden />
              ) : (
                <FileText className="text-text-tertiary size-4 shrink-0" aria-hidden />
              )}
              <span className="min-w-0 flex-1">
                {/* Truncated from the middle so the extension survives, which is what
                    tells the student what kind of file a row is. The default budget is
                    what the document list already uses, and it fits the narrowest
                    breakpoint; a CSS clip on top of it would cut the extension back off. */}
                <span className="text-text-primary block text-sm" title={document.filename}>
                  {truncateMiddle(document.filename)}
                </span>
                <span className="text-text-tertiary block text-xs">
                  {describe(document, isClaimed)}
                </span>
              </span>
            </label>
          </li>
        )
      })}
    </ul>
  )
}

function describe(document: DocumentRead, claimed: boolean): string {
  if (claimed) return 'Already used above'
  if (document.state === 'ready') return formatFileSize(document.byte_size)
  if (document.state === 'unsupported') return 'Needs text recognition, so Lyra cannot read it yet'
  if (document.state === 'failed') return document.error_message ?? 'Could not be processed'
  return `${STAGE_LABELS[document.state] ?? 'Processing'}, not ready yet`
}
