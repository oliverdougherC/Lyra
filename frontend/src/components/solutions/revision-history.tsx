'use client'

import { toast } from 'sonner'

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
import { ApiError } from '@/lib/api'
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
}

/**
 * Every stored version of one part, newest first.
 *
 * Restoring writes a new revision rather than rewinding the list, so what was there in
 * between stays readable and restoring is itself undoable.
 */
export function RevisionHistory({ artifactId, part, onClose }: RevisionHistoryProps) {
  const revisions = usePartRevisions(artifactId, part?.id ?? null)
  const restore = useRestoreRevision(artifactId)

  const handleRestore = (revision: number) => {
    if (!part) return
    restore.mutate(
      { partId: part.id, revision },
      {
        onSuccess: () => {
          toast.success('Restored that version.')
          onClose()
        },
        onError: (error) =>
          toast.error(
            error instanceof ApiError ? error.message : 'Could not restore that version.',
          ),
      },
    )
  }

  return (
    <Sheet open={part !== null} onOpenChange={(open) => (open ? null : onClose())}>
      <SheetContent side="right" className="w-full gap-0 p-0 sm:max-w-[480px]">
        <SheetHeader className="border-border border-b">
          <SheetTitle>History</SheetTitle>
          <SheetDescription>
            {part?.label ?? 'This part'} of your solution set, newest version first.
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
                    <p className="text-text-secondary text-xs italic">{revision.note}</p>
                  ) : null}
                  <p className="text-text-secondary line-clamp-6 text-sm whitespace-pre-wrap">
                    {revision.content}
                  </p>
                  {index === 0 ? (
                    <span className="text-text-tertiary text-xs">This is what is shown now.</span>
                  ) : (
                    <Button
                      variant="outline"
                      size="sm"
                      className="self-start"
                      onClick={() => handleRestore(revision.revision)}
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
