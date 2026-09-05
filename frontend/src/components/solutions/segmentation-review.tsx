'use client'

import { FileQuestion, Plus, Undo2 } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { toast } from 'sonner'

import { DraftProblem, ProblemCard } from '@/components/solutions/problem-card'
import { Button } from '@/components/ui/button'
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { Reveal } from '@/components/ui/reveal'
import { ApiError } from '@/lib/api'
import { formatCount } from '@/lib/format'
import { useUpdateSegmentation } from '@/lib/hooks/use-solutions'
import type { SolutionDetail, SolutionPart } from '@/types'

/** Deep enough to walk back a run of deletions, shallow enough not to hold the sheet twice. */
const HISTORY_LIMIT = 25

type SegmentationReviewProps = {
  solution: SolutionDetail
  onResegment: () => void
  resegmenting: boolean
  /** Confirms the list and starts solving. Absent in tests that only exercise the list. */
  onSolve?: () => void
  solving?: boolean
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
  onSolve,
  solving = false,
}: SegmentationReviewProps) {
  const [problems, setProblems] = useState<DraftProblem[]>(() => toDrafts(solution.parts))
  const [nextKey, setNextKey] = useState(0)
  const save = useUpdateSegmentation(solution.id)

  /**
   * Structural edits, so they can be taken back.
   *
   * Removing a sub-part is one click on a small X beside text the student is still
   * reading, and until now it was final: the only route back was re-reading the whole
   * sheet. Typing is deliberately not recorded here. The textarea has the browser's own
   * undo, and pushing a snapshot per keystroke would bury the delete the student is
   * actually reaching for.
   */
  const [history, setHistory] = useState<{ problems: DraftProblem[]; label: string }[]>([])

  /** Snapshots the list as it stands, before the caller changes it. */
  const remember = (label: string) =>
    setHistory((current) => [...current.slice(1 - HISTORY_LIMIT), { problems, label }])

  const undo = useCallback(() => {
    const previous = history[history.length - 1]
    if (!previous) {
      toast('Nothing to undo.')
      return
    }
    setProblems(previous.problems)
    setHistory((current) => current.slice(0, -1))
    toast(`Undid ${previous.label}.`)
  }, [history])

  // Cmd/Ctrl+Z, except while typing: a textarea has its own undo stack and taking that
  // key away from it would make editing a statement worse than the delete this fixes.
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key.toLowerCase() !== 'z' || event.shiftKey || event.altKey) return
      if (!(event.metaKey || event.ctrlKey)) return
      const target = event.target
      if (target instanceof HTMLElement) {
        if (target.isContentEditable) return
        if (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) return
      }
      event.preventDefault()
      undo()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [undo])

  // The draft is seeded once, so it has to adopt a list that arrives later. It does:
  // the poll flips the artifact to `awaiting_review` a moment before the detail query
  // refetches, so this screen commonly mounts with no parts at all and would otherwise
  // sit on its empty state ("Lyra could not find separate problems") while the backend
  // holds five. Adjusted during render rather than in an effect, so no frame shows the
  // wrong list. A re-segmentation lands the same way and is also meant to replace the
  // draft, which is why this does not try to preserve unsaved edits across it.
  const signature = partSignature(solution.parts)
  const [lastSignature, setLastSignature] = useState(signature)
  if (signature !== lastSignature) {
    setLastSignature(signature)
    setProblems(toDrafts(solution.parts))
    // The undo stack described a list that no longer exists. Keeping it would let one
    // keystroke restore problems from a previous reading of the sheet.
    setHistory([])
  }

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
    remember('merging two problems')
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
        // Two problems merged into one is not evidence that their parts stand alone, and
        // the two halves may well have disagreed about it. Back to the safe reading.
        separateParts: false,
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
    remember('splitting a problem')
    replace(index, [
      {
        ...problem,
        key: makeKey(),
        id: null,
        statement: problem.statement.slice(0, midpoint).trim(),
        parts: [],
        separateParts: false,
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

  const handleAdd = () => {
    remember('adding a problem')
    setProblems((current) => [
      ...current,
      {
        key: makeKey(),
        id: null,
        label: `Problem ${current.length + 1}`,
        statement: '',
        parts: [],
        separateParts: false,
        source: null,
        page: null,
        edited: true,
      },
    ])
  }

  /** Sends the corrected list. `then` runs only once it has landed. */
  const commit = (then?: () => void) => {
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
          separate_parts: problem.separateParts,
        })),
      },
      {
        onSuccess: (updated) => {
          setProblems(toDrafts(updated.parts))
          if (then) then()
          else toast.success(`Saved ${formatCount(updated.parts.length, 'change')}.`)
        },
        onError: (error) =>
          toast.error(error instanceof ApiError ? error.message : 'Could not save your changes.'),
      },
    )
  }

  const handleSave = () => commit()

  /**
   * Corrections are saved before the solve starts, never alongside it. A run against the
   * list the student has just edited on screen but not sent would solve the wrong
   * problems, which is exactly what this gate exists to prevent.
   */
  const handleSolve = () => {
    if (!onSolve) return
    if (dirty) commit(onSolve)
    else onSolve()
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
          Lyra is least sure about the boundaries: where one problem ends and another begins, and
          whether a part is a question of its own. That is what you are confirming here - fix a
          boundary now and it is cheaper than re-solving later.
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
                onRemove={() => {
                  remember(`removing ${problem.label || `problem ${index + 1}`}`)
                  setProblems((current) => current.filter((_, i) => i !== index))
                }}
                onRemovePart={(position) => {
                  const part = problem.parts[position]
                  remember(`removing part ${part.label || position + 1}`)
                  replace(index, [
                    {
                      ...problem,
                      edited: true,
                      parts: problem.parts.filter((_, other) => other !== position),
                    },
                  ])
                }}
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
        {/* The shortcut is the point, but a shortcut with nothing on screen is a feature
            only its author knows about. This appears the moment there is something to
            take back, and names the key rather than assuming it is guessed. */}
        {history.length > 0 ? (
          <Button variant="ghost" size="sm" onClick={undo}>
            <Undo2 className="size-4" />
            Undo
            <kbd className="text-text-tertiary ml-1 text-xs">⌘Z</kbd>
          </Button>
        ) : null}
        <span className="flex-1" />
        {/* Save stays available but secondary. Solving is what this screen is a gate in
            front of, and the primary action says how many problems it is about to spend
            compute on rather than just "Solve". */}
        <Button variant="outline" onClick={handleSave} disabled={!dirty || save.isPending}>
          {save.isPending ? 'Saving' : 'Save changes'}
        </Button>
        {onSolve ? (
          <Button onClick={handleSolve} disabled={solving || save.isPending}>
            {solving ? 'Starting' : `Solve ${formatCount(problems.length, 'problem')}`}
          </Button>
        ) : null}
      </div>
    </div>
  )
}

/**
 * What the server currently holds, as a value that changes exactly when the list does.
 *
 * Ids and content both, because a re-segmentation can produce the same number of problems
 * with different text, and a count alone would not notice.
 */
function partSignature(parts: SolutionPart[]): string {
  return parts.map((part) => `${part.id}:${part.content.length}:${part.label ?? ''}`).join('|')
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
    separateParts: root.solve_parts === 'separately',
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
      problem.separateParts === other.separateParts &&
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
