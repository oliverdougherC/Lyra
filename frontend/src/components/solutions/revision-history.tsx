'use client'

import { toast } from 'sonner'

import { MathText } from '@/components/solutions/math-text'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Skeleton } from '@/components/ui/skeleton'
import { ApiError, DraftBodyConflictError } from '@/lib/api'
import type { SaveConflict } from '@/lib/drafts/save-engine'
import { formatRelativeTime } from '@/lib/format'
import { usePartRevisions, useRestoreRevision } from '@/lib/hooks/use-solutions'
import type { PartOrigin, SolutionPart } from '@/types'

const ORIGIN_LABELS: Record<PartOrigin, string> = {
  generated: 'Generated',
  regenerated: 'Regenerated',
  user_corrected: 'Your edit',
}

type RevisionHistoryProps = {
  artifactId: number
  part: SolutionPart | null
  onClose: () => void
  /** What the part belongs to, in the sheet's description. A draft passes 'draft'. */
  noun?: string
  /**
   * Draft-only: confirm the current body is on disk before restoring, and report the
   * version to restore against. Returns `ok: false` to abort - the save failure or conflict
   * is already surfaced. Flushing first also makes the student's current text its own
   * revision, so restoring an older one loses nothing (PLA-289).
   */
  saveBeforeRestore?: () => Promise<{ ok: boolean; version: number }>
  /** Draft-only: a stale-version restore was refused; hand the conflict to the save engine. */
  onBodyConflict?: (conflict: SaveConflict) => void
}

/**
 * Every stored version of one part, newest first.
 *
 * Restoring writes a new revision rather than rewinding the list, so what was there in
 * between stays readable and restoring is itself undoable.
 */
export function RevisionHistory({
  artifactId,
  part,
  onClose,
  noun = 'solution set',
  saveBeforeRestore,
  onBodyConflict,
}: RevisionHistoryProps) {
  const revisions = usePartRevisions(artifactId, part?.id ?? null)
  const restore = useRestoreRevision(artifactId)

  const handleRestore = async (revision: number) => {
    if (!part) return
    // Draft bodies restore version-aware: confirm the current text first (so it is itself a
    // recoverable revision) and carry its version, so a stale tab cannot replace a body that
    // changed elsewhere. Solutions pass no barrier and restore unchanged.
    let expectedVersion: number | undefined
    if (saveBeforeRestore) {
      const barrier = await saveBeforeRestore()
      if (!barrier.ok) return
      expectedVersion = barrier.version
    }
    restore.mutate(
      { partId: part.id, revision, expectedVersion },
      {
        onSuccess: () => {
          toast.success('Restored that version.')
          onClose()
        },
        onError: (error) => {
          if (error instanceof DraftBodyConflictError && onBodyConflict) {
            // The body moved elsewhere since this tab last read it: reconcile rather than
            // replace. Nothing was written.
            onBodyConflict({ serverVersion: error.currentVersion, serverBody: error.serverBody })
            onClose()
            return
          }
          toast.error(error instanceof ApiError ? error.message : 'Could not restore that version.')
        },
      },
    )
  }

  return (
    <Sheet open={part !== null} onOpenChange={(open) => (open ? null : onClose())}>
      <SheetContent side="right" className="w-full gap-0 p-0 sm:max-w-[480px]">
        <SheetHeader className="border-border border-b">
          <SheetTitle>History</SheetTitle>
          <SheetDescription>
            {part?.label ?? 'This part'} of your {noun}, newest version first.
          </SheetDescription>
        </SheetHeader>

        <ScrollArea className="min-h-0 flex-1">
          <div className="flex flex-col gap-4 p-4">
            {revisions.isPending ? (
              [0, 1].map((row) => <Skeleton key={row} className="h-24 w-full rounded-md" />)
            ) : revisions.isError ? (
              <p className="text-text-tertiary text-sm">Could not load the history.</p>
            ) : revisions.data.length === 0 ? (
              <p className="text-text-tertiary text-sm">Nothing has been written here yet.</p>
            ) : (
              revisions.data.map((revision, index) => (
                <article
                  key={revision.revision}
                  className="border-border bg-card flex flex-col gap-2 rounded-md border p-3"
                >
                  <header className="flex flex-wrap items-baseline justify-between gap-2">
                    <span className="text-text-primary text-sm font-medium">
                      {ORIGIN_LABELS[revision.origin]}
                    </span>
                    <span className="text-text-tertiary text-xs">
                      {formatRelativeTime(revision.created_at)}
                    </span>
                  </header>
                  {revision.note ? (
                    // Why this version exists: the student's own correction, or the
                    // refutation that prompted the re-solve.
                    <p className="text-text-secondary text-xs">{revision.note}</p>
                  ) : null}
                  {/* Typeset and whole. A version is chosen by reading it, and six
                      clamped lines of raw LaTeX made restoring one a guess. The sheet
                      scrolls, so length costs nothing here. */}
                  <MathText className="text-text-secondary text-sm">{revision.content}</MathText>
                  {index === 0 ? (
                    <span className="text-text-tertiary text-xs">This is what is shown now.</span>
                  ) : (
                    <Button
                      variant="outline"
                      size="sm"
                      className="self-start"
                      onClick={() => void handleRestore(revision.revision)}
                      disabled={restore.isPending}
                    >
                      Restore
                    </Button>
                  )}
                </article>
              ))
            )}
          </div>
        </ScrollArea>
      </SheetContent>
    </Sheet>
  )
}
