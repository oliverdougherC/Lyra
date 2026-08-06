'use client'

import { useId, useState } from 'react'
import { FileWarning, MoreVertical, Pencil, PenLine, Plus, Trash2 } from 'lucide-react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { toast } from 'sonner'

import { RenameDialog } from '@/components/classes/rename-dialog'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Spinner } from '@/components/ui/spinner'
import { ApiError } from '@/lib/api'
import { formatRelativeTime } from '@/lib/format'
import { useCreateDraft, useDeleteDraft, useDrafts, useRenameDraft } from '@/lib/hooks/use-drafts'
import type { ArtifactState, DraftRead } from '@/types'

/** Where a suggestion run has got to, in words rather than internal state names. */
const DRAFT_STATE_LABELS: Partial<Record<ArtifactState, string>> = {
  pending: 'Queued',
  generating: 'Suggesting',
  ready: 'Ready',
  failed: 'Could not finish',
}

function stateLabel(state: ArtifactState): string {
  return DRAFT_STATE_LABELS[state] ?? state
}

/** A draft being renamed or deleted. */
type DraftTarget = { id: number; title: string }

/** Every draft in a class, with the actions the study panel taught. */
export function ClassDraftsPanel({ classId }: { classId: number }) {
  const drafts = useDrafts(classId)
  const renameDraft = useRenameDraft(classId)
  const deleteDraft = useDeleteDraft(classId)
  const [creating, setCreating] = useState(false)
  const [renaming, setRenaming] = useState<DraftTarget | null>(null)
  const [deleting, setDeleting] = useState<DraftTarget | null>(null)

  async function onConfirmDelete() {
    if (!deleting) return
    try {
      await deleteDraft.mutateAsync(deleting.id)
      toast.success(`${deleting.title} deleted.`)
      setDeleting(null)
    } catch (caught) {
      toast.error(caught instanceof ApiError ? caught.message : 'Could not delete that draft.')
    }
  }

  if (drafts.isPending) {
    return (
      <div className="flex flex-col gap-2" aria-busy="true" aria-label="Loading drafts">
        {[0, 1, 2].map((row) => (
          <Skeleton key={row} className="h-16 w-full rounded-md" />
        ))}
      </div>
    )
  }

  if (drafts.isError) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load your drafts</AlertTitle>
        <AlertDescription>
          <p>{drafts.error instanceof ApiError ? drafts.error.message : 'Something went wrong.'}</p>
          <Button
            variant="outline"
            size="sm"
            className="mt-3"
            onClick={() => void drafts.refetch()}
          >
            Retry
          </Button>
        </AlertDescription>
      </Alert>
    )
  }

  const list = drafts.data
  const empty = list.length === 0

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between gap-3">
        <p className="text-text-secondary text-sm">
          Documents you write, with Lyra drafting passages and suggesting revisions.
        </p>
        {!empty ? (
          <Button size="sm" className="shrink-0" onClick={() => setCreating(true)}>
            <Plus className="size-4" />
            New draft
          </Button>
        ) : null}
      </div>

      {empty ? (
        <Empty className="py-12">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <PenLine className="text-text-tertiary size-8" />
            </EmptyMedia>
            <EmptyTitle>No drafts yet</EmptyTitle>
            <EmptyDescription>
              Start an essay, a report, or a lab write-up, and Lyra works on it with you.
            </EmptyDescription>
          </EmptyHeader>
          <div className="mt-2">
            <Button onClick={() => setCreating(true)}>New draft</Button>
          </div>
        </Empty>
      ) : (
        <ul className="flex flex-col gap-2">
          {list.map((draft) => (
            <li key={draft.id}>
              <DraftRow
                classId={classId}
                draft={draft}
                onRename={() => setRenaming({ id: draft.id, title: draft.title })}
                onDelete={() => setDeleting({ id: draft.id, title: draft.title })}
              />
            </li>
          ))}
        </ul>
      )}

      <CreateDraftDialog classId={classId} open={creating} onOpenChange={setCreating} />

      <RenameDialog
        target={renaming ? { id: renaming.id, name: renaming.title } : null}
        title="Rename draft"
        description="It was named in a hurry, which is rarely what the work is."
        label="Name"
        pending={renameDraft.isPending}
        onOpenChange={(open) => {
          if (!open) setRenaming(null)
        }}
        onRename={(draftId, title) => renameDraft.mutateAsync({ draftId, title })}
      />

      <AlertDialog
        open={deleting !== null}
        onOpenChange={(open) => {
          if (!open) setDeleting(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {deleting?.title}?</AlertDialogTitle>
            <AlertDialogDescription>
              The draft and its history go. The documents it drew on stay. This cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <Button
              variant="destructive"
              disabled={deleteDraft.isPending}
              onClick={() => void onConfirmDelete()}
            >
              {deleteDraft.isPending ? <Spinner /> : null}
              Delete
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

/**
 * One draft in the list. The whole row is the link, and the actions menu sits above it in
 * the stacking order, so a row stays one target while Rename and Delete stay
 * independently clickable - the same arrangement as a study row, for the same reason.
 */
function DraftRow({
  classId,
  draft,
  onRename,
  onDelete,
}: {
  classId: number
  draft: DraftRead
  onRename: () => void
  onDelete: () => void
}) {
  const failed = draft.state === 'failed'
  const working = draft.state === 'pending' || draft.state === 'generating'

  return (
    <div className="group border-border bg-card hover:border-border-strong focus-within:border-border-strong relative flex items-center rounded-md border transition-colors">
      <Link
        href={`/classes/${classId}/drafts/${draft.id}`}
        className="focus-visible:ring-ring flex min-w-0 flex-1 items-center gap-4 rounded-md py-3 pr-1 pl-4 focus-visible:ring-2 focus-visible:outline-none"
      >
        <span className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="text-text-primary truncate font-medium">{draft.title}</span>
          <span className="text-text-tertiary truncate text-xs">
            {working
              ? (draft.stage_detail ?? stateLabel(draft.state))
              : `Edited ${formatRelativeTime(draft.updated_at)}`}
          </span>
        </span>

        {failed ? (
          <span className="text-danger-text inline-flex shrink-0 items-center gap-1.5 text-xs">
            <FileWarning className="size-3.5" aria-hidden />
            {stateLabel(draft.state)}
          </span>
        ) : working ? (
          <span className="text-text-tertiary shrink-0 text-xs">{stateLabel(draft.state)}</span>
        ) : null}
      </Link>

      <div className="shrink-0 pr-2">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              aria-label={`Actions for ${draft.title}`}
              className="size-8 opacity-0 transition-opacity group-focus-within:opacity-100 group-hover:opacity-100 focus-visible:opacity-100 data-[state=open]:opacity-100"
            >
              <MoreVertical />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={onRename}>
              <Pencil />
              Rename
            </DropdownMenuItem>
            <DropdownMenuItem variant="destructive" onSelect={onDelete}>
              <Trash2 />
              Delete
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  )
}

/** A draft asks for one decision up front: what it is called. */
function CreateDraftDialog({
  classId,
  open,
  onOpenChange,
}: {
  classId: number
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const router = useRouter()
  const titleId = useId()
  const createDraft = useCreateDraft(classId)
  const [title, setTitle] = useState('')
  const [error, setError] = useState<string | null>(null)

  // Reset during render rather than in an effect, so reopening the dialog never shows the
  // previous attempt's title for a frame.
  const [openSeen, setOpenSeen] = useState(open)
  if (open !== openSeen) {
    setOpenSeen(open)
    setTitle('')
    setError(null)
  }

  async function onSubmit() {
    const trimmed = title.trim()
    if (!trimmed) return
    try {
      const artifact = await createDraft.mutateAsync(trimmed)
      onOpenChange(false)
      router.push(`/classes/${classId}/drafts/${artifact.id}`)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not create this draft.')
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>New draft</DialogTitle>
          <DialogDescription>
            A blank page in this class. Lyra can draft passages into it and suggest revisions once
            there is something on it.
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-2">
          <Label htmlFor={titleId}>Name</Label>
          <Input
            id={titleId}
            value={title}
            autoFocus
            autoComplete="off"
            placeholder="Essay on feedback systems"
            onChange={(event) => setTitle(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault()
                void onSubmit()
              }
            }}
          />
        </div>
        {error ? (
          <p className="text-danger-text text-sm" role="alert">
            {error}
          </p>
        ) : null}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            disabled={title.trim().length === 0 || createDraft.isPending}
            onClick={() => void onSubmit()}
          >
            {createDraft.isPending ? <Spinner /> : null}
            Create draft
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
