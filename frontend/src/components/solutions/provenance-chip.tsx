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
          className="border-border text-text-tertiary inline-flex max-w-full items-center gap-1 rounded-full border px-2 py-0.5 text-xs"
          // The full path, for a section nested deeply enough to be shortened on screen.
          title={entry.section_path ?? undefined}
        >
          <BookOpen className="size-3 shrink-0" aria-hidden />
          <span className="min-w-0 truncate">{describe(entry)}</span>
        </li>
      ))}
    </ul>
  )
}

/** How the backend joins a section path. Titles, not numbers. */
const PATH_SEPARATOR = ' / '

/** Beyond this many levels a path stops being a location and becomes a sentence. */
const MAX_PATH_LEVELS = 2

function describe(entry: Provenance): string {
  // The filename survives a deleted document as null while the page number stays true, so
  // each half is rendered on its own terms rather than as one string that may be half
  // missing.
  const filename = entry.filename ? truncateMiddle(entry.filename, 28) : 'A deleted document'
  const where = entry.page_number !== null ? `page ${entry.page_number}` : null

  // A section is where a reader would actually look, so it leads. The page stays because it
  // is what they turn to, and the two together are what "cites the section by its path
  // rather than by a page number alone" means.
  const section = shortenPath(entry.section_path)
  if (section) return [filename, section, where].filter(Boolean).join(', ')
  return where ? `${filename}, ${where}` : filename
}

/**
 * The last couple of levels of a path, which is the part that locates something.
 *
 * A four-level path spends most of its width on the book's outermost divisions, which the
 * filename has already said. The full path is on the chip's `title`.
 */
function shortenPath(path: string | null): string | null {
  if (!path) return null
  const levels = path.split(PATH_SEPARATOR).filter(Boolean)
  if (levels.length === 0) return null
  return levels.slice(-MAX_PATH_LEVELS).join(PATH_SEPARATOR)
}
