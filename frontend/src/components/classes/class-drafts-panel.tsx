'use client'

import { useState } from 'react'
import { FileWarning, MoreVertical, Pencil, PenLine, Plus, Trash2 } from 'lucide-react'
import Link from '@/router/link'
import { useRouter } from '@/router/hooks'
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
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { Skeleton } from '@/components/ui/skeleton'
import { Spinner } from '@/components/ui/spinner'
import { ApiError } from '@/lib/api'
import { formatRelativeTime } from '@/lib/format'
import { untitledDraftTitle } from '@/lib/handoff'
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
export function ClassDraftsPanel({
  classId,
  query = '',
  limit,
  managedRecovery = false,
}: {
  classId: number
  query?: string
  limit?: number
  managedRecovery?: boolean
}) {
  const router = useRouter()
  const drafts = useDrafts(classId)
  const createDraft = useCreateDraft(classId)
  const renameDraft = useRenameDraft(classId)
  const deleteDraft = useDeleteDraft(classId)
  const [renaming, setRenaming] = useState<DraftTarget | null>(null)
  const [deleting, setDeleting] = useState<DraftTarget | null>(null)

  /**
   * A draft used to ask for its name before it existed, which is backwards: the name is
   * the hardest thing to know about a piece of writing before any of it is written. New
   * draft now opens the page immediately, stamped to the minute and numbered past any
   * sibling with the same stamp, and the workspace title edits in place whenever the
   * work has earned its real name.
   */
  function startDraft() {
    createDraft.mutate(untitledDraftTitle((drafts.data ?? []).map((draft) => draft.title)), {
      onSuccess: (artifact) => router.push(`/classes/${classId}/drafts/${artifact.id}`),
      onError: (caught) =>
        toast.error(caught instanceof ApiError ? caught.message : 'Could not start a draft.'),
    })
  }

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

  const errorNotice = (
    <Alert variant="destructive">
      <AlertTitle>Could not load your drafts</AlertTitle>
      <AlertDescription>
        <p>{drafts.error instanceof ApiError ? drafts.error.message : 'Something went wrong.'}</p>
        <Button variant="outline" size="sm" className="mt-3" onClick={() => void drafts.refetch()}>
          Retry
        </Button>
      </AlertDescription>
    </Alert>
  )
  if (drafts.isError && !drafts.data) return managedRecovery ? null : errorNotice

  const list = drafts.data ?? []
  const matches = list.filter((draft) =>
    draft.title.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()),
  )
  const empty = list.length === 0

  return (
    <div className="flex flex-col gap-6">
      {drafts.isError && !managedRecovery ? errorNotice : null}
      <div className="flex items-center justify-between gap-3">
        <p className="text-text-secondary text-sm">
          Documents you write, with Lyra drafting passages and suggesting revisions.
        </p>
        {!empty ? (
          <Button
            size="sm"
            className="shrink-0"
            onClick={startDraft}
            disabled={createDraft.isPending}
          >
            {createDraft.isPending ? <Spinner /> : <Plus className="size-4" />}
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
            <Button onClick={startDraft} disabled={createDraft.isPending}>
              {createDraft.isPending ? <Spinner /> : null}
              New draft
            </Button>
          </div>
        </Empty>
      ) : matches.length === 0 ? (
        <p role="status">No work matches this search.</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {matches.slice(0, limit).map((draft) => (
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

      <RenameDialog
        target={renaming ? { id: renaming.id, name: renaming.title } : null}
        title="Rename draft"
        description="Choose a name for this draft."
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
          <span className="text-text-primary break-words font-medium">{draft.title}</span>
          <span className="text-text-tertiary break-words text-xs">
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
              className="size-8"
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
