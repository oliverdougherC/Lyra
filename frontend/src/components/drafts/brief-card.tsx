'use client'

import { Check, Pencil } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Textarea } from '@/components/ui/textarea'
import { useBrief, useConfirmBrief, useSaveBrief } from '@/lib/hooks/use-drafts'
import { cn } from '@/lib/utils'
import type { DraftBrief } from '@/types'

/**
 * The brief: what this document is, sitting above the writer's conversation because it
 * is what every answer below it is grounded in.
 *
 * Three states, honestly distinct. No brief is an invitation, not an error - the writer
 * offers to work it out, and this card offers the form. A proposed brief is Lyra's
 * guess and looks like one: accent-edged, labelled, with Confirm as the primary act. A
 * confirmed brief folds down to one quiet line, because a settled fact should take up
 * a settled fact's amount of room.
 */
export function BriefCard({ draftId }: { draftId: number }) {
  const { data: brief, isPending, isError, refetch } = useBrief(draftId)
  const [editing, setEditing] = useState(false)
  const errorNotice = isError ? (
    <div className="mb-4 space-y-2" role="alert">
      <p className="text-danger-text text-sm">
        The brief could not be refreshed.{' '}
        {brief ? 'Showing the saved brief.' : 'Try again to load it.'}
      </p>
      <Button variant="outline" size="sm" onClick={() => void refetch()}>
        Retry brief
      </Button>
    </div>
  ) : null

  if (isPending) return <Skeleton className="mb-4 h-10 w-full" />
  if (editing) {
    return <BriefForm draftId={draftId} brief={brief ?? null} onDone={() => setEditing(false)} />
  }
  if (!brief) {
    if (isError) return errorNotice
    return (
      <div className="border-border/70 text-text-tertiary mb-4 flex items-center gap-2 rounded-md border border-dashed px-3 py-2 text-xs">
        <span className="min-w-0 flex-1">
          No brief yet. Ask Lyra what this document should be, or set it yourself.
        </span>
        <Button
          variant="ghost"
          size="sm"
          className="h-6 shrink-0 px-2 text-xs"
          onClick={() => setEditing(true)}
        >
          Set up
        </Button>
      </div>
    )
  }
  return (
    <>
      {errorNotice}
      <BriefSummary brief={brief} draftId={draftId} onEdit={() => setEditing(true)} />
    </>
  )
}

function BriefSummary({
  brief,
  draftId,
  onEdit,
}: {
  brief: DraftBrief
  draftId: number
  onEdit: () => void
}) {
  const confirm = useConfirmBrief(draftId)
  const proposed = brief.status === 'proposed'
  const described = [brief.assignment_type, brief.length_target, brief.audience]
    .map((field) => field.trim())
    .filter(Boolean)
    .join(' · ')

  return (
    <section
      aria-label="What this document is"
      className={cn(
        'mb-4 rounded-md border px-3 py-2',
        // Lyra's guess wears Lyra's accent; the student's settled brief sits quiet.
        proposed ? 'border-accent-primary/50 bg-accent-surface/30' : 'border-border/70',
      )}
    >
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          {proposed ? (
            <p className="eyebrow text-accent-primary mb-0.5">Lyra&apos;s guess</p>
          ) : null}
          <p className="text-text-primary text-sm leading-5">
            {brief.summary.trim() || 'An untitled assignment.'}
          </p>
          {described ? <p className="text-text-tertiary mt-0.5 text-xs">{described}</p> : null}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {proposed ? (
            <Button
              size="sm"
              className="h-7 px-2.5 text-xs"
              disabled={confirm.isPending}
              onClick={async () => {
                try {
                  await confirm.mutateAsync()
                } catch {
                  toast.error('Could not confirm the brief.')
                }
              }}
            >
              <Check className="size-3.5" />
              Confirm
            </Button>
          ) : null}
          <Button
            variant="ghost"
            size="sm"
            className="text-text-tertiary hover:text-text-primary size-7 p-0"
            onClick={onEdit}
            aria-label="Edit the brief"
          >
            <Pencil className="size-3.5" />
          </Button>
        </div>
      </div>
    </section>
  )
}

/**
 * The form, for setting the brief by hand or correcting the guess. Saving is
 * confirming: the words are the student's own.
 */
function BriefForm({
  draftId,
  brief,
  onDone,
}: {
  draftId: number
  brief: DraftBrief | null
  onDone: () => void
}) {
  const save = useSaveBrief(draftId)
  const [summary, setSummary] = useState(brief?.summary ?? '')
  const [assignmentType, setAssignmentType] = useState(brief?.assignment_type ?? '')
  const [audience, setAudience] = useState(brief?.audience ?? '')
  const [lengthTarget, setLengthTarget] = useState(brief?.length_target ?? '')

  return (
    <form
      aria-label="Edit the brief"
      className="@container border-border/70 mb-4 flex flex-col gap-2.5 rounded-md border px-3 py-2.5"
      onSubmit={async (event) => {
        event.preventDefault()
        try {
          await save.mutateAsync({
            summary,
            assignment_type: assignmentType,
            audience,
            length_target: lengthTarget,
            source_document_id: brief?.source_document_id ?? null,
          })
          onDone()
        } catch {
          toast.error('Could not save the brief.')
        }
      }}
    >
      <div className="flex flex-col gap-1">
        <Label htmlFor={`brief-summary-${draftId}`} className="text-xs">
          What is this?
        </Label>
        <Textarea
          id={`brief-summary-${draftId}`}
          value={summary}
          onChange={(event) => setSummary(event.target.value)}
          placeholder="A lab report on the pendulum experiment: period vs. length."
          rows={2}
          className="text-sm"
        />
      </div>
      <div className="grid grid-cols-1 gap-3 @min-[28rem]:grid-cols-3">
        <div className="flex min-w-0 flex-col gap-1">
          <Label htmlFor={`brief-type-${draftId}`} className="text-xs">
            Kind
          </Label>
          <Input
            id={`brief-type-${draftId}`}
            value={assignmentType}
            onChange={(event) => setAssignmentType(event.target.value)}
            placeholder="lab report"
            className="h-8 text-sm"
          />
        </div>
        <div className="flex min-w-0 flex-col gap-1">
          <Label htmlFor={`brief-length-${draftId}`} className="text-xs">
            Length
          </Label>
          <Input
            id={`brief-length-${draftId}`}
            value={lengthTarget}
            onChange={(event) => setLengthTarget(event.target.value)}
            placeholder="5 pages"
            className="h-8 text-sm"
          />
        </div>
        <div className="flex min-w-0 flex-col gap-1">
          <Label htmlFor={`brief-audience-${draftId}`} className="text-xs">
            For
          </Label>
          <Input
            id={`brief-audience-${draftId}`}
            value={audience}
            onChange={(event) => setAudience(event.target.value)}
            placeholder="the TA"
            className="h-8 text-sm"
          />
        </div>
      </div>
      <div className="flex items-center justify-end gap-1.5">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-7 px-2.5 text-xs"
          onClick={onDone}
        >
          Cancel
        </Button>
        <Button type="submit" size="sm" className="h-7 px-2.5 text-xs" disabled={save.isPending}>
          Save
        </Button>
      </div>
    </form>
  )
}
