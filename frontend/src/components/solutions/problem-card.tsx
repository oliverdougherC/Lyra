'use client'

import { ChevronDown, MoreHorizontal, X } from 'lucide-react'
import { useState } from 'react'

import { MathText } from '@/components/solutions/math-text'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { statementLeadIn } from '@/lib/statement'
import { cn } from '@/lib/utils'

/** One problem as the review screen holds it, before it goes back to the backend. */
export type DraftProblem = {
  /** A stable key for React. Not the part id: a merged problem has no single source. */
  key: string
  /** The part this came from, when it came from one. Carries provenance through a save. */
  id: number | null
  label: string
  statement: string
  parts: { key: string; label: string; statement: string }[]
  source: string | null
  page: number | null
  edited: boolean
}

type ProblemCardProps = {
  problem: DraftProblem
  index: number
  /** True when the source line differs from the card above, per the profile view's rule. */
  showSource: boolean
  canMerge: boolean
  onChange: (problem: DraftProblem) => void
  onMerge: () => void
  onSplit: () => void
  onRemove: () => void
  /**
   * Separate from `onChange` even though it only drops an entry from `parts`. The parent
   * keeps the undo stack, and it cannot tell a delete from a keystroke if both arrive as
   * a whole replacement problem.
   */
  onRemovePart: (position: number) => void
}

const PREVIEW_LINES = 2

export function ProblemCard({
  problem,
  index,
  showSource,
  canMerge,
  onChange,
  onMerge,
  onSplit,
  onRemove,
  onRemovePart,
}: ProblemCardProps) {
  const [open, setOpen] = useState(false)
  const [editingLabel, setEditingLabel] = useState(false)

  return (
    <div className="border-border bg-card rounded-md border shadow-sm">
      <div className="flex items-start gap-3 p-4">
        <span className="text-text-tertiary w-6 shrink-0 pt-0.5 text-sm tabular-nums">
          {index + 1}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            {editingLabel ? (
              <Input
                autoFocus
                value={problem.label}
                aria-label="Problem label"
                className="h-8 w-48"
                onChange={(event) => onChange({ ...problem, label: event.target.value })}
                onBlur={() => setEditingLabel(false)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === 'Escape') {
                    event.preventDefault()
                    setEditingLabel(false)
                  }
                }}
              />
            ) : (
              <button
                type="button"
                className="text-text-primary rounded-sm text-sm font-medium hover:underline"
                onClick={() => setEditingLabel(true)}
              >
                {problem.label || `Problem ${index + 1}`}
              </button>
            )}
            {problem.edited ? (
              <Badge variant="secondary" className="text-xs">
                Edited
              </Badge>
            ) : null}
          </div>

          {showSource && problem.source ? (
            <p className="text-text-tertiary mt-1 text-xs">
              {problem.source}
              {problem.page !== null ? `, page ${problem.page}` : ''}
            </p>
          ) : null}

          {open ? (
            <Textarea
              className="mt-3 min-h-32 font-sans text-sm"
              aria-label={`${problem.label} statement`}
              value={problem.statement}
              onChange={(event) =>
                onChange({ ...problem, statement: event.target.value, edited: true })
              }
            />
          ) : (
            // Typeset, because this is the screen where the student checks Lyra's reading
            // against the sheet in front of them, and a flattened exponent is precisely
            // the kind of misreading the check is for. The editor above stays raw text:
            // the LaTeX is what they would need to correct.
            <MathText
              className="text-text-secondary mt-2 text-sm"
              style={{
                display: '-webkit-box',
                WebkitLineClamp: PREVIEW_LINES,
                WebkitBoxOrient: 'vertical',
                overflow: 'hidden',
              }}
            >
              {statementLeadIn(
                problem.statement,
                problem.parts.map((part) => part.label),
              )}
            </MathText>
          )}

          {problem.parts.length > 0 ? (
            <ul className="border-border mt-3 flex flex-col gap-2 border-l pl-3">
              {problem.parts.map((part, position) => (
                <li key={part.key} className="flex items-start gap-2">
                  <span className="text-text-tertiary shrink-0 text-xs">{part.label}</span>
                  <MathText inline className="text-text-secondary min-w-0 flex-1 truncate text-sm">
                    {part.statement}
                  </MathText>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="size-6 shrink-0"
                    aria-label={`Remove part ${part.label} of ${problem.label}`}
                    onClick={() => onRemovePart(position)}
                  >
                    <X className="size-3.5" />
                  </Button>
                </li>
              ))}
            </ul>
          ) : null}

          <button
            type="button"
            className="text-text-tertiary hover:text-text-secondary mt-3 flex items-center gap-1 rounded-sm text-xs"
            onClick={() => setOpen((current) => !current)}
            aria-expanded={open}
          >
            <ChevronDown className={cn('size-3.5 transition-transform', open && 'rotate-180')} />
            {open ? 'Show less' : 'Read and edit the full statement'}
          </button>
        </div>

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon" aria-label={`Actions for ${problem.label}`}>
              <MoreHorizontal className="size-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={onMerge} disabled={!canMerge}>
              Merge with next
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={onSplit}>Split in two</DropdownMenuItem>
            <DropdownMenuItem onSelect={onRemove} variant="destructive">
              Remove
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  )
}
