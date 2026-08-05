'use client'

import { useState } from 'react'
import { Plus, SquareCheckBig } from 'lucide-react'
import Link from 'next/link'
import { toast } from 'sonner'

import { RenameDialog } from '@/components/classes/rename-dialog'
import { SolutionRow } from '@/components/solutions/solution-row'
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
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { Skeleton } from '@/components/ui/skeleton'
import { Spinner } from '@/components/ui/spinner'
import { ApiError } from '@/lib/api'
import { useDeleteSolution, useRenameSolution, useSolutions } from '@/lib/hooks/use-solutions'
import type { SolutionRead } from '@/types'

/** Every solution set in a class, with the actions the index page has never had. */
export function ClassSolutionsPanel({ classId }: { classId: number }) {
  const solutions = useSolutions(classId)
  const renameSolution = useRenameSolution(classId)
  const deleteSolution = useDeleteSolution(classId)
  const [renaming, setRenaming] = useState<SolutionRead | null>(null)
  const [deleting, setDeleting] = useState<SolutionRead | null>(null)

  async function onConfirmDelete() {
    if (!deleting) return
    try {
      await deleteSolution.mutateAsync(deleting.id)
      toast.success(`${deleting.title} deleted.`)
      setDeleting(null)
    } catch (caught) {
      toast.error(
        caught instanceof ApiError ? caught.message : 'Could not delete that solution set.',
      )
    }
  }

  if (solutions.isPending) {
    return (
      <div className="flex flex-col gap-2" aria-busy="true" aria-label="Loading solution sets">
        {[0, 1, 2].map((row) => (
          <Skeleton key={row} className="h-16 w-full rounded-md" />
        ))}
      </div>
    )
  }

  if (solutions.isError) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load your solution sets</AlertTitle>
        <AlertDescription>
          <p>
            {solutions.error instanceof ApiError
              ? solutions.error.message
              : 'Something went wrong.'}
          </p>
          <Button
            variant="outline"
            size="sm"
            className="mt-3"
            onClick={() => void solutions.refetch()}
          >
            Retry
          </Button>
        </AlertDescription>
      </Alert>
    )
  }

  const list = solutions.data

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-3">
        <p className="text-text-secondary text-sm">
          Every problem set you have handed to Lyra in this class.
        </p>
        {list.length > 0 ? (
          <Button asChild size="sm">
            <Link href={`/classes/${classId}/solutions/new`}>
              <Plus className="size-4" />
              New solution set
            </Link>
          </Button>
        ) : null}
      </div>

      {list.length === 0 ? (
        <Empty className="py-12">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <SquareCheckBig className="text-text-tertiary size-8" />
            </EmptyMedia>
            <EmptyTitle>No solution sets yet</EmptyTitle>
            <EmptyDescription>
              Hand Lyra a homework PDF and it works through every problem, checking the mathematics
              as it goes.
            </EmptyDescription>
          </EmptyHeader>
          <Button asChild className="mt-2">
            <Link href={`/classes/${classId}/solutions/new`}>Solve a problem set</Link>
          </Button>
        </Empty>
      ) : (
        <ul className="flex flex-col gap-2">
          {list.map((solution) => (
            <li key={solution.id}>
              <SolutionRow
                classId={classId}
                solution={solution}
                onRename={setRenaming}
                onDelete={setDeleting}
              />
            </li>
          ))}
        </ul>
      )}

      <RenameDialog
        target={renaming ? { id: renaming.id, name: renaming.title } : null}
        title="Rename solution set"
        description="It was named after the problem set's filename, which is rarely what the work is."
        label="Name"
        pending={renameSolution.isPending}
        onOpenChange={(open) => {
          if (!open) setRenaming(null)
        }}
        onRename={(artifactId, title) => renameSolution.mutateAsync({ artifactId, title })}
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
              Every solution in it goes, along with their working, their checks, and their edit
              history. The documents it was built from stay. This cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <Button
              variant="destructive"
              disabled={deleteSolution.isPending}
              onClick={() => void onConfirmDelete()}
            >
              {deleteSolution.isPending ? <Spinner /> : null}
              Delete
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
