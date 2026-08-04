'use client'

import { FileQuestion, Plus } from 'lucide-react'
import { useMemo, useState } from 'react'
import { toast } from 'sonner'

import { DraftProblem, ProblemCard } from '@/components/solutions/problem-card'
import { Button } from '@/components/ui/button'
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { Reveal } from '@/components/ui/reveal'
import { ApiError } from '@/lib/api'
import { formatCount } from '@/lib/format'
import { useUpdateSegmentation } from '@/lib/hooks/use-solutions'
import type { SolutionDetail, SolutionPart } from '@/types'

type SegmentationReviewProps = {
  solution: SolutionDetail
  onResegment: () => void
  resegmenting: boolean
}

/**
 * The gate.
 *
 * This screen exists so a missed or merged problem costs one edit instead of a full
 * re-run, and it has to make that trade obvious rather than feeling like a speed bump.
 * The heading states the count and the reason in the same breath.
 *
 * The whole list is sent back on save rather than a patch of one row: merge and split are
 * the two corrections that matter most here, and neither is expressible per row.
 */
export function SegmentationReview({
  solution,
  onResegment,
  resegmenting,
}: SegmentationReviewProps) {
  const [problems, setProblems] = useState<DraftProblem[]>(() => toDrafts(solution.parts))
  const [nextKey, setNextKey] = useState(0)
  const save = useUpdateSegmentation(solution.id)

  const dirty = useMemo(() => !sameAs(problems, solution.parts), [problems, solution.parts])

  const makeKey = () => {
    const key = `new-${nextKey}`
    setNextKey((current) => current + 1)
    return key
  }

  const replace = (index: number, next: DraftProblem[]) =>
    setProblems((current) => [...current.slice(0, index), ...next, ...current.slice(index + 1)])

  const handleMerge = (index: number) => {
    const [first, second] = [problems[index], problems[index + 1]]
    if (!second) return
    setProblems((current) => [
      ...current.slice(0, index),
      {
        key: makeKey(),
        // A problem assembled from two others came from neither one alone, so it keeps no
        // id and the backend gives it no inherited provenance.
        id: null,
        label: first.label,
        statement: `${first.statement}\n\n${second.statement}`,
        parts: [...first.parts, ...second.parts],
        source: first.source,
        page: first.page,
        edited: true,
      },
      ...current.slice(index + 2),
    ])
  }

  const handleSplit = (index: number) => {
    const problem = problems[index]
    const midpoint = splitPoint(problem.statement)
    if (midpoint <= 0) {
      toast.error('There is nothing to split. Open the statement and cut it where you want.')
      return
    }
    replace(index, [
      {
        ...problem,
        key: makeKey(),
        id: null,
        statement: problem.statement.slice(0, midpoint).trim(),
        parts: [],
        edited: true,
      },
      {
        ...problem,
        key: makeKey(),
        id: null,
        label: `${problem.label} (second part)`,
        statement: problem.statement.slice(midpoint).trim(),
        edited: true,
      },
    ])
  }

  const handleAdd = () =>
    setProblems((current) => [
      ...current,
      {
        key: makeKey(),
        id: null,
        label: `Problem ${current.length + 1}`,
        statement: '',
        parts: [],
        source: null,
        page: null,
        edited: true,
      },
    ])

  const handleSave = () => {
    const blank = problems.findIndex((problem) => !problem.statement.trim())
    if (blank !== -1) {
      toast.error(`${problems[blank].label || `Problem ${blank + 1}`} has no statement yet.`)
      return
    }
    save.mutate(
      {
        problems: problems.map((problem) => ({
          id: problem.id,
          label: problem.label.trim() || null,
          statement: problem.statement.trim(),
          parts: problem.parts.map((part) => ({
            label: part.label.trim() || null,
            statement: part.statement.trim(),
          })),
        })),
      },
      {
        onSuccess: (updated) => {
          setProblems(toDrafts(updated.parts))
          toast.success(`Saved ${formatCount(updated.parts.length, 'change')}.`)
        },
        onError: (error) =>
          toast.error(error instanceof ApiError ? error.message : 'Could not save your changes.'),
      },
    )
  }

  if (problems.length === 0) {
    return (
      <div className="flex flex-col gap-4">
        <Empty className="py-12">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <FileQuestion className="text-text-tertiary size-8" />
            </EmptyMedia>
            <EmptyTitle>Lyra could not find separate problems</EmptyTitle>
            <EmptyDescription>
              This document does not look like a numbered problem set. You can add problems
              yourself, or have Lyra read it again.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
        <div className="flex justify-center gap-2">
          <Button onClick={handleAdd}>Add a problem</Button>
          <Button variant="outline" onClick={onResegment} disabled={resegmenting}>
            {resegmenting ? 'Reading again' : 'Read it again'}
          </Button>
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-col gap-1">
        <h2 className="font-heading text-text-primary text-xl tracking-tight">
          Lyra found {formatCount(problems.length, 'problem')}
        </h2>
        <p className="text-text-secondary text-sm">
          Check these before solving. Fixing a problem now is much faster than re-solving one later.
        </p>
      </header>

      <ul className="flex flex-col gap-3">
        {problems.map((problem, index) => (
          <li key={problem.key}>
            <Reveal
              once={`segment-${solution.id}-${problem.key}`}
              delay={Math.min(index * 0.05, 0.2)}
            >
              <ProblemCard
                problem={problem}
                index={index}
                // A source line is shown only when it differs from the row above, so eight
                // problems from one file do not print eight identical citations.
                showSource={problem.source !== (problems[index - 1]?.source ?? null)}
                canMerge={index < problems.length - 1}
                onChange={(next) => replace(index, [next])}
                onMerge={() => handleMerge(index)}
                onSplit={() => handleSplit(index)}
                onRemove={() => setProblems((current) => current.filter((_, i) => i !== index))}
              />
            </Reveal>
          </li>
        ))}
      </ul>

      <div className="flex flex-wrap items-center gap-2">
        <Button variant="outline" size="sm" onClick={handleAdd}>
          <Plus className="size-4" />
          Add a problem
        </Button>
        <Button variant="ghost" size="sm" onClick={onResegment} disabled={resegmenting}>
          {resegmenting ? 'Reading again' : 'Read it again'}
        </Button>
        <span className="flex-1" />
        <Button onClick={handleSave} disabled={!dirty || save.isPending}>
          {save.isPending ? 'Saving' : 'Save changes'}
        </Button>
      </div>
    </div>
  )
}

function toDrafts(parts: SolutionPart[]): DraftProblem[] {
  const roots = parts.filter((part) => part.parent_part_id === null)
  return roots.map((root) => ({
    key: `part-${root.id}`,
    id: root.id,
    label: root.label ?? '',
    statement: root.content,
    parts: parts
      .filter((part) => part.parent_part_id === root.id)
      .map((part) => ({
        key: `part-${part.id}`,
        label: part.label ?? '',
        statement: part.content,
      })),
    source: root.provenance[0]?.filename ?? null,
    page: root.provenance[0]?.page_number ?? null,
    edited: root.origin === 'user_corrected',
  }))
}

/** Whether the draft still matches what the backend holds, so Save can stay disabled. */
function sameAs(problems: DraftProblem[], parts: SolutionPart[]): boolean {
  const stored = toDrafts(parts)
  if (stored.length !== problems.length) return false
  return problems.every((problem, index) => {
    const other = stored[index]
    return (
      problem.id === other.id &&
      problem.label === other.label &&
      problem.statement === other.statement &&
      problem.parts.length === other.parts.length &&
      problem.parts.every(
        (part, position) =>
          part.label === other.parts[position].label &&
          part.statement === other.parts[position].statement,
      )
    )
  })
}

/**
 * Where to cut a statement in two. The blank line between paragraphs nearest the middle is
 * where a merged pair of problems almost always joins, and when there is no blank line
 * there is nothing to guess at, so the caller says so rather than cutting mid-sentence.
 */
function splitPoint(statement: string): number {
  const middle = Math.floor(statement.length / 2)
  let best = -1
  const breaks = /\n\s*\n/g
  let match = breaks.exec(statement)
  while (match !== null) {
    if (best === -1 || Math.abs(match.index - middle) < Math.abs(best - middle)) {
      best = match.index
    }
    match = breaks.exec(statement)
  }
  return best
}
