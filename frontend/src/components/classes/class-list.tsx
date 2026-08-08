'use client'

import { useMemo, useRef, useState } from 'react'
import { Archive, GraduationCap, Plus } from 'lucide-react'

import { ClassCard, ClassCardSkeleton, NewClassCard } from '@/components/classes/class-card'
import { ClassFormDialog } from '@/components/classes/class-form-dialog'
import { DeleteClassDialog } from '@/components/classes/delete-class-dialog'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from '@/components/ui/empty'
import { ApiError } from '@/lib/api'
import { useAppShortcuts } from '@/lib/hooks/use-app-shortcuts'
import { useClasses, useUpdateClass } from '@/lib/hooks/use-classes'
import { formatCount, parseTimestamp } from '@/lib/format'
import type { ClassRead } from '@/types'

const SKELETON_COUNT = 4
/** The ledger: one class per line, hairlines between, like a contents page. */
const LEDGER = 'flex flex-col divide-y divide-border/70 border-y border-border/70'

export function ClassList() {
  const { data, isPending, isError, error, refetch, isFetching } = useClasses()
  const updateClass = useUpdateClass()
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<ClassRead | null>(null)
  const [deleting, setDeleting] = useState<ClassRead | null>(null)
  const [focusClassId, setFocusClassId] = useState<number | null>(null)
  const formTriggerRef = useRef<HTMLButtonElement>(null)
  const createdRef = useRef(false)

  const shortcuts = useMemo(
    () => [
      {
        key: 'n',
        run: () => {
          // Always the create form: the dialog doubles as Rename, and a stale editing
          // target would turn the shortcut into an edit of whichever class was last
          // renamed.
          setEditing(null)
          setFormOpen(true)
        },
      },
    ],
    [],
  )
  useAppShortcuts(shortcuts)

  function openCreate(trigger: HTMLButtonElement) {
    formTriggerRef.current = trigger
    setEditing(null)
    setFormOpen(true)
  }

  function openRename(klass: ClassRead) {
    setEditing(klass)
    setFormOpen(true)
  }

  function onFormOpenChange(open: boolean) {
    setFormOpen(open)
    if (!open && !createdRef.current && editing === null) {
      requestAnimationFrame(() => formTriggerRef.current?.focus())
    }
    if (!open) createdRef.current = false
  }

  const classes = data
    ? [...data].sort(
        (a, b) =>
          parseTimestamp(b.last_active_at).getTime() - parseTimestamp(a.last_active_at).getTime(),
      )
    : []
  const activeClasses = classes.filter((item) => !item.archived)
  const archivedCount = classes.length - activeClasses.length

  return (
    // A reading measure, not a dashboard: the index is a centered column with room to
    // breathe, because eleven classes is a page of a book, not a wall of tiles.
    <div className="mx-auto w-full max-w-3xl space-y-8">
      {/* One action, one home (ui-overhaul 2.1): New class lives only as the ledger's final
          line and the keyboard shortcut. The header carries the title, not a second button
          for the verb the list already ends with. */}
      <div className="pt-2 md:pt-6">
        <h1 className="font-display text-3xl leading-tight md:text-4xl">Classes</h1>
        <p className="text-text-secondary mt-1.5 text-sm">
          Everything Lyra knows is organized by class.
        </p>
      </div>

      {archivedCount > 0 ? (
        <p className="text-text-tertiary -mt-2 text-xs">
          {formatCount(archivedCount, 'class')} archived. Open the sidebar&apos;s Archived section
          to restore one.
        </p>
      ) : null}

      {isPending ? (
        <div className={LEDGER} aria-busy="true" aria-label="Loading classes">
          {Array.from({ length: SKELETON_COUNT }, (_, index) => (
            <ClassCardSkeleton key={index} />
          ))}
        </div>
      ) : isError ? (
        <Alert variant="destructive">
          <AlertTitle>Could not load your classes</AlertTitle>
          <AlertDescription className="text-danger-text">
            <p>
              {error instanceof ApiError
                ? error.message
                : "Could not load your classes. Check that Lyra's local server is running, then try again."}
            </p>
            <Button
              variant="outline"
              size="sm"
              className="mt-2"
              disabled={isFetching}
              onClick={() => void refetch()}
            >
              Retry
            </Button>
          </AlertDescription>
        </Alert>
      ) : activeClasses.length === 0 ? (
        archivedCount > 0 ? (
          <Empty>
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <Archive className="text-text-tertiary size-8" />
              </EmptyMedia>
              <EmptyTitle>All classes are archived</EmptyTitle>
              <EmptyDescription>
                Restore a class from the sidebar&apos;s Archived section to see it here.
              </EmptyDescription>
            </EmptyHeader>
            <EmptyContent>
              <Button size="lg" onClick={(event) => openCreate(event.currentTarget)}>
                <Plus />
                New class
              </Button>
            </EmptyContent>
          </Empty>
        ) : (
          <Empty>
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <GraduationCap className="text-text-tertiary size-8" />
              </EmptyMedia>
              <EmptyTitle>No classes yet</EmptyTitle>
              <EmptyDescription>
                Create a class to start uploading your course materials.
              </EmptyDescription>
            </EmptyHeader>
            <EmptyContent>
              <Button size="lg" onClick={(event) => openCreate(event.currentTarget)}>
                <Plus />
                New class
              </Button>
            </EmptyContent>
          </Empty>
        )
      ) : (
        <div className={LEDGER}>
          {activeClasses.map((klass, index) => (
            <ClassCard
              key={klass.id}
              klass={klass}
              index={index}
              autoFocus={klass.id === focusClassId}
              onRename={openRename}
              onDelete={setDeleting}
              onArchive={(picked) =>
                updateClass.mutate({ classId: picked.id, body: { archived: true } })
              }
            />
          ))}
          <NewClassCard onClick={openCreate} />
        </div>
      )}

      <ClassFormDialog
        open={formOpen}
        onOpenChange={onFormOpenChange}
        klass={editing}
        onCreated={(created) => {
          createdRef.current = true
          setFocusClassId(created.id)
        }}
      />
      <DeleteClassDialog klass={deleting} onOpenChange={() => setDeleting(null)} />
    </div>
  )
}
