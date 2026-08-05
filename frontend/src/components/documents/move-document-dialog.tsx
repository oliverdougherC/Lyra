'use client'

import { useState } from 'react'
import { toast } from 'sonner'

import { CourseMark } from '@/components/classes/course-mark'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Spinner } from '@/components/ui/spinner'
import { ApiError } from '@/lib/api'
import { formatCount } from '@/lib/format'
import { useClasses } from '@/lib/hooks/use-classes'
import { useMoveDocument } from '@/lib/hooks/use-documents'
import { cn } from '@/lib/utils'
import type { DocumentRead } from '@/types'

type MoveDocumentDialogProps = {
  /** The files to refile, or an empty list when the dialog is closed. */
  documents: DocumentRead[]
  classId: number
  onOpenChange: (open: boolean) => void
  onMoved?: () => void
}

/**
 * Where a misfiled document belongs.
 *
 * Retrieval is partitioned by class, so a file in the wrong workspace is invisible where
 * it is needed and quietly answering questions where it is not. Until this existed the
 * only fix was to delete it and upload it again, which throws the file away to correct
 * its label.
 */
export function MoveDocumentDialog({
  documents,
  classId,
  onOpenChange,
  onMoved,
}: MoveDocumentDialogProps) {
  const { data: classes } = useClasses()
  const moveDocument = useMoveDocument(classId)
  const [target, setTarget] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  const open = documents.length > 0
  // Reset during render rather than in an effect, so a second move never opens on the
  // destination chosen for the first one.
  const [openedFor, setOpenedFor] = useState<string | null>(null)
  const signature = documents.map((document) => document.id).join(',')
  if (open && signature !== openedFor) {
    setOpenedFor(signature)
    setTarget(null)
    setError(null)
  }

  // Archived classes are offered too: a class put away at the end of a term is exactly
  // where last term's notes belong, and refusing to file them there would be strange.
  const destinations = (classes ?? []).filter((item) => item.id !== classId)

  async function onConfirm() {
    if (target === null) return
    const destination = destinations.find((item) => item.id === target)
    const results = await Promise.allSettled(
      documents.map((document) =>
        moveDocument.mutateAsync({ documentId: document.id, targetClassId: target }),
      ),
    )
    const failed = results.filter((result) => result.status === 'rejected')
    if (failed.length === results.length) {
      const first = failed[0] as PromiseRejectedResult
      setError(
        first.reason instanceof ApiError ? first.reason.message : 'Could not move these files.',
      )
      return
    }
    const movedCount = results.length - failed.length
    toast.success(
      `${formatCount(movedCount, 'file')} moved to ${destination?.name ?? 'the other class'}.`,
      {
        // Said plainly rather than left as a surprise: the file arrives unindexed and is
        // not searchable in its new class until it has been read again.
        description: 'Lyra is reading them again so they can be searched there.',
      },
    )
    // A partial failure still leaves something to say about the rest, and the toast above
    // already reported what did move.
    if (failed.length > 0) {
      const first = failed[0] as PromiseRejectedResult
      toast.error(
        first.reason instanceof ApiError
          ? first.reason.message
          : `${formatCount(failed.length, 'file')} could not be moved.`,
      )
    }
    onMoved?.()
    onOpenChange(false)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>
            {documents.length === 1
              ? `Move ${documents[0].filename}`
              : `Move ${formatCount(documents.length, 'file')}`}
          </DialogTitle>
          <DialogDescription>
            The file keeps its place in Lyra, and is indexed again under the class you choose.
          </DialogDescription>
        </DialogHeader>

        {destinations.length === 0 ? (
          <p className="text-text-secondary text-sm">
            There is nowhere to move this to yet. Create another class first.
          </p>
        ) : (
          <ScrollArea className="max-h-64">
            <ul className="flex flex-col gap-1 pr-2">
              {destinations.map((item) => (
                <li key={item.id}>
                  <button
                    type="button"
                    aria-pressed={target === item.id}
                    onClick={() => setTarget(item.id)}
                    className={cn(
                      'focus-visible:ring-ring flex w-full items-center gap-3 rounded-md border px-3 py-2 text-left transition-colors duration-150 focus-visible:ring-2 focus-visible:outline-none',
                      target === item.id
                        ? 'border-accent-primary bg-accent-surface/50'
                        : 'border-border hover:bg-muted',
                    )}
                  >
                    <CourseMark klass={item} size="sm" />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium">{item.name}</span>
                      {item.code || item.archived ? (
                        <span className="text-text-tertiary block truncate text-xs">
                          {[item.code, item.archived ? 'Archived' : null]
                            .filter(Boolean)
                            .join(' · ')}
                        </span>
                      ) : null}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </ScrollArea>
        )}

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
            disabled={target === null || moveDocument.isPending}
            onClick={() => void onConfirm()}
          >
            {moveDocument.isPending ? <Spinner /> : null}
            Move
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
