'use client'

import { useState } from 'react'
import { ImageOff } from 'lucide-react'

import { ProvenanceChip } from '@/components/solutions/provenance-chip'
import { figureUrl } from '@/lib/api'
import { cn } from '@/lib/utils'
import type { SolutionPart } from '@/types'

type FigureBlockProps = {
  figure: SolutionPart
  className?: string
}

/**
 * A diagram from the source document, on the problem it belongs to.
 *
 * The first artifact content that is not text. The part stores the figure's id rather than
 * a copy of the image, so the picture follows the document: re-indexing a source under a
 * better reading of it improves the figure a solution shows, instead of freezing the crop
 * that was taken on the day it was solved.
 */
export function FigureBlock({ figure, className }: FigureBlockProps) {
  const [broken, setBroken] = useState(false)
  const name = figure.label ?? 'Figure'

  return (
    <figure className={cn('figure-block flex flex-col gap-1.5', className)}>
      {broken ? (
        // The solution is worth more than the figure. A missing image costs its caption and
        // nothing else: not a broken-image glyph, and not an empty row that reads as a
        // rendering bug in the solution itself.
        <div className="border-border text-text-tertiary flex items-center gap-2 rounded-md border border-dashed px-3 py-4 text-xs">
          <ImageOff className="size-4 shrink-0" aria-hidden />
          Figure not available
        </div>
      ) : (
        // Never wider than the reading column, and a wide figure scales down rather than
        // scrolling. Math scrolls because cutting an equation loses information; a figure
        // twenty percent smaller loses none.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={figureUrl(Number(figure.content))}
          alt={name}
          onError={() => setBroken(true)}
          className="border-border bg-card h-auto max-w-full rounded-md border"
        />
      )}
      <figcaption className="text-text-tertiary flex flex-wrap items-center gap-2 text-xs">
        <span>{name}</span>
        <ProvenanceChip entries={figure.provenance} />
      </figcaption>
    </figure>
  )
}
