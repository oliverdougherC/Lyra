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
import { type ArtifactKind, usePartRevisions, useRestoreRevision } from '@/lib/hooks/use-solutions'
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
  /** Which API surface to call: 'solution' (default) or 'draft'. */
  kind?: ArtifactKind
  /**
   * Draft-only: confirm the current body is on disk before restoring, and report the
   * version to restore against. Returns `ok: false` to abort - the save failure or conflict
   * is already surfaced. Landing the current body first is what lets the restore preserve it:
   * the server records the pre-restore body as a revision before writing the target, so
   * restoring an older version loses nothing (PLA-289).
   */
  saveBeforeRestore?: () => Promise<{ ok: boolean; version: number }>
  /** Draft-only: a stale-version restore was refused; hand the conflict to the save engine. */
  onBodyConflict?: (conflict: SaveConflict) => void
  /**
   * Draft-only: the body the editor is showing right now. A draft's live body can be newer
   * than the newest recorded revision (autosave records none), so the "shown now" marker is
   * placed by matching this rather than by assuming the top row is current. Omitted for
   * solutions, where every write records a revision and the newest one is always current.
   */
  currentBody?: string
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
  kind = 'solution',
  saveBeforeRestore,
  onBodyConflict,
  currentBody,
}: RevisionHistoryProps) {
  const revisions = usePartRevisions(artifactId, part?.id ?? null, kind)
  const restore = useRestoreRevision(artifactId, kind)

  // Exactly one row is "shown now". A restore writes a new revision rather than rewinding,
  // so the history can hold duplicate content (e.g. [A, B, A]); matching every row against
  // the editor body would let several rows claim to be current and drop their Restore. The
  // list is newest-first, so the current row is the newest match (the lowest index). For a
  // solution (no `currentBody`) the newest row is always current; when the live draft body
  // is newer than every recorded revision, nothing matches and no row is marked.
  const currentRevisionIndex =
    currentBody === undefined
      ? 0
      : (revisions.data?.findIndex((revision) => revision.content === currentBody) ?? -1)

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
              revisions.data.map((revision, index) => {
                // Which row is actually on screen: the single newest revision whose content
                // matches the editor (see `currentRevisionIndex`). Older identical-content
                // revisions stay ordinary historical entries and keep their Restore action.
                const shownNow = index === currentRevisionIndex
                return (
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
                    {shownNow ? (
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
                )
              })
            )}
          </div>
        </ScrollArea>
      </SheetContent>
    </Sheet>
  )
}
