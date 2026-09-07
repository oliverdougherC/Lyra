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
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
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
  /**
   * Whether those parts are questions in their own right rather than steps of one
   * solution. Lyra reads it off the sheet and the student confirms it here, which is the
   * one field on this screen that works that way round.
   */
  separateParts: boolean
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
  onAddPart: () => void
}

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
  onAddPart,
}: ProblemCardProps) {
  const [open, setOpen] = useState(false)
  const [editingLabel, setEditingLabel] = useState(false)

  return (
    <div className="border-border bg-card rounded-md border shadow-sm">
      <div className="flex items-start gap-3 p-4">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            {editingLabel ? (
              <Input
                autoFocus
                value={problem.label}
                aria-label="Problem label"
                className="h-8 w-full max-w-64"
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
                className="text-text-primary rounded-sm text-left text-base font-medium break-words hover:underline"
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

          {(showSource && problem.source) || problem.page !== null ? (
            <p className="text-text-secondary mt-1 text-sm break-words">
              {showSource && problem.source ? <span>{problem.source}</span> : null}
              {showSource && problem.source && problem.page !== null ? ' · ' : null}
              {problem.page !== null ? <span>Page {problem.page}</span> : null}
            </p>
          ) : null}

          {open ? (
            <Textarea
              className="mt-3 min-h-32 font-sans text-lg leading-relaxed md:text-lg"
              aria-label={`${problem.label} statement`}
              value={problem.statement}
              onChange={(event) =>
                onChange({ ...problem, statement: event.target.value, edited: true })
              }
            />
          ) : (
            // Whole, and typeset. This is the screen where the student checks Lyra's
            // reading against the sheet in front of them, and two clamped lines ending in
            // an ellipsis hid the half of the problem most likely to be misread: the
            // equations. A gate whose contents cannot be read is not a gate. The editor
            // above stays raw text, because the LaTeX is what a correction has to change.
            <MathText className="text-text-primary mt-3 text-lg leading-relaxed">
              {problem.statement}
            </MathText>
          )}

          {problem.parts.length > 0 ? (
            <ul className="border-border mt-3 flex flex-col gap-2 border-l pl-3">
              {problem.parts.map((part, position) => (
                <li key={part.key} className="flex items-start gap-2">
                  {open ? (
                    <div className="min-w-0 flex-1 space-y-2">
                      <Input
                        aria-label={`Part ${position + 1} label of ${problem.label}`}
                        value={part.label}
                        placeholder="Part label"
                        className="h-8 w-full max-w-40"
                        onChange={(event) =>
                          onChange({
                            ...problem,
                            edited: true,
                            parts: problem.parts.map((entry, i) =>
                              i === position ? { ...entry, label: event.target.value } : entry,
                            ),
                          })
                        }
                      />
                      <Textarea
                        className="text-lg leading-relaxed md:text-lg"
                        aria-label={`Part ${position + 1} statement of ${problem.label}`}
                        value={part.statement}
                        onChange={(event) =>
                          onChange({
                            ...problem,
                            edited: true,
                            parts: problem.parts.map((entry, i) =>
                              i === position ? { ...entry, statement: event.target.value } : entry,
                            ),
                          })
                        }
                      />
                    </div>
                  ) : (
                    <>
                      <span className="text-text-secondary shrink-0 pt-1 text-sm">
                        {part.label}
                      </span>
                      <MathText className="text-text-primary min-w-0 flex-1 text-lg leading-relaxed">
                        {part.statement}
                      </MathText>
                    </>
                  )}
                  {open ? (
                    <Button
                      variant="ghost"
                      size="icon"
                      className="size-9 shrink-0"
                      aria-label={`Remove part ${part.label || position + 1} of ${problem.label}`}
                      onClick={() => onRemovePart(position)}
                    >
                      <X className="size-4" />
                    </Button>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : null}

          {open ? (
            <Button variant="outline" size="sm" className="mt-3" onClick={onAddPart}>
              Add a part
            </Button>
          ) : null}

          {/* The one reading on this screen Lyra makes and the student confirms, rather
              than the other way round. It is here, under the parts it is about, because
              it is a claim about them: whether answering (a) tells you anything about
              (b). What it decides is whether each part gets a solution, an answer, and a
              check of its own, or whether one solution answers all of them. */}
          {problem.parts.length > 1 ? (
            <label className="mt-3 flex cursor-pointer items-start gap-2.5">
              <Switch
                checked={problem.separateParts}
                onCheckedChange={(checked) =>
                  onChange({ ...problem, separateParts: checked === true })
                }
                aria-label={`Solve each part of ${problem.label} on its own`}
                className="mt-0.5"
              />
              <span className="text-text-secondary text-sm">
                Split into {problem.parts.length} questions
              </span>
            </label>
          ) : null}

          <button
            type="button"
            className="text-text-secondary hover:text-text-primary mt-3 flex min-h-9 items-center gap-1 rounded-sm text-sm"
            onClick={() => setOpen((current) => !current)}
            aria-expanded={open}
          >
            <ChevronDown className={cn('size-3.5 transition-transform', open && 'rotate-180')} />
            {/* The statement is already on screen in full, so this no longer reveals it:
                it swaps the typeset reading for the raw text a correction is made in. */}
            {open ? 'Done editing' : 'Edit'}
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
