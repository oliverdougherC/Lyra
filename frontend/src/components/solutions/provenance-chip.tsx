'use client'

import { BookOpen } from 'lucide-react'

import { truncateMiddle } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { Provenance } from '@/types'

type ProvenanceChipProps = {
  entries: Provenance[]
  className?: string
}

/**
 * Where a step came from, on the step's own row rather than inline in its prose.
 *
 * A step with no provenance renders nothing at all. There is no confidence percentage
 * anywhere in the solver, because a number nobody can audit reads as precision that does
 * not exist; this is the whole of what "grounded" means, and it is either there or not.
 */
export function ProvenanceChip({ entries, className }: ProvenanceChipProps) {
  const cited = entries.filter((entry) => entry.filename || entry.page_number !== null)
  if (cited.length === 0) return null

  return (
    <ul className={cn('flex flex-wrap gap-1.5', className)}>
      {cited.map((entry, index) => (
        <li
          key={`${entry.chunk_id ?? 'none'}-${index}`}
          className="border-border text-text-tertiary inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs"
        >
          <BookOpen className="size-3 shrink-0" aria-hidden />
          <span>{describe(entry)}</span>
        </li>
      ))}
    </ul>
  )
}

function describe(entry: Provenance): string {
  // The filename survives a deleted document as null while the page number stays true, so
  // each half is rendered on its own terms rather than as one string that may be half
  // missing.
  const filename = entry.filename ? truncateMiddle(entry.filename, 28) : 'A deleted document'
  return entry.page_number !== null ? `${filename}, page ${entry.page_number}` : filename
}
