'use client'

import { useState } from 'react'
import { MessageSquare, MoreVertical, Pencil, Plus, SquareCheckBig, Trash2 } from 'lucide-react'
import Link from '@/router/link'
import { toast } from 'sonner'

import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { RenameDialog } from '@/components/classes/rename-dialog'
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
import { formatRelativeTime, formatSessionFallbackTitle } from '@/lib/format'
import { useDeleteSession, useRenameSession, useSessions } from '@/lib/hooks/use-chat'
import type { SessionRead } from '@/types'

/**
 * Every conversation in a class, and the three things you can do to one.
 *
 * The sidebar lists the last five and folds the rest away, which is right for a rail and
 * wrong as the only place the history exists: a term's chats were reachable but not
 * manageable, with no way to rename a bad auto-title or delete a conversation at all.
 */
export function ClassChatsPanel({
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
  const { data: sessions, isPending, isError, isFetching, refetch } = useSessions(classId)
  const renameSession = useRenameSession(classId)
  const deleteSession = useDeleteSession(classId)
  const [renaming, setRenaming] = useState<SessionRead | null>(null)
  const [deleting, setDeleting] = useState<SessionRead | null>(null)

  async function onConfirmDelete() {
    if (!deleting) return
    try {
      await deleteSession.mutateAsync(deleting.id)
      toast.success('Conversation deleted.')
      setDeleting(null)
    } catch (caught) {
      toast.error(
        caught instanceof ApiError ? caught.message : 'Could not delete that conversation.',
      )
    }
  }

  if (isPending) {
    return (
      <div className="flex flex-col gap-2" aria-busy="true" aria-label="Loading conversations">
        {[0, 1, 2].map((row) => (
          <Skeleton key={row} className="h-14 w-full rounded-md" />
        ))}
      </div>
    )
  }

  const list = sessions ?? []
  const matches = list.filter((session) =>
    (session.title || formatSessionFallbackTitle(session.created_at))
      .toLocaleLowerCase()
      .includes(query.trim().toLocaleLowerCase()),
  )

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-3">
        <p className="text-text-secondary text-sm">Everything you have asked Lyra in this class.</p>
        {list.length > 0 ? (
          <Button asChild size="sm">
            <Link href={`/classes/${classId}/chat?session=new`}>
              <Plus className="size-4" />
              New chat
            </Link>
          </Button>
        ) : null}
      </div>

      {isError && !managedRecovery ? (
        <div role="alert" className="rounded-md border p-4 text-sm">
          <p>Could not load conversations.</p>
          <Button
            variant="outline"
            size="sm"
            className="mt-2"
            disabled={isFetching}
            onClick={() => void refetch()}
          >
            {isFetching ? 'Retrying…' : 'Retry conversations'}
          </Button>
        </div>
      ) : null}
      {list.length === 0 && isError ? null : list.length === 0 ? (
        <Empty className="py-12">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <MessageSquare className="text-text-tertiary size-8" />
            </EmptyMedia>
            <EmptyTitle>No conversations yet</EmptyTitle>
            <EmptyDescription>
              Ask a question about your course material and it will be saved here.
            </EmptyDescription>
          </EmptyHeader>
          <Button asChild className="mt-2">
            <Link href={`/classes/${classId}/chat?session=new`}>Start a chat</Link>
          </Button>
        </Empty>
      ) : matches.length === 0 ? (
        <p role="status">No work matches this search.</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {matches.slice(0, limit).map((session) => (
            <li key={session.id}>
              <div className="group border-border bg-card hover:border-border-strong focus-within:border-border-strong relative flex items-center gap-2 rounded-md border transition-colors">
                <Link
                  href={`/classes/${classId}/chat?session=${session.id}`}
                  className="focus-visible:ring-ring flex min-w-0 flex-1 flex-col gap-1 rounded-md px-4 py-3 focus-visible:ring-2 focus-visible:outline-none"
                >
                  <span className="text-text-primary break-words font-medium">
                    {session.title || formatSessionFallbackTitle(session.created_at)}
                  </span>
                  <span className="text-text-tertiary flex flex-wrap items-center gap-2 text-xs">
                    {formatRelativeTime(session.created_at)}
                    {/* A conversation opened from a solution step keeps that step pinned
                        into every turn, which is worth saying: it reads differently from
                        an ordinary chat and answers as though the step is on screen. */}
                    {session.artifact_part_id !== null ? (
                      <span className="text-text-tertiary inline-flex items-center gap-1">
                        <SquareCheckBig aria-hidden className="size-3" />
                        About a solution step
                      </span>
                    ) : null}
                  </span>
                </Link>

                <div className="shrink-0 pr-2">
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={`Actions for ${session.title || 'this conversation'}`}
                        className="size-8"
                      >
                        <MoreVertical />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      <DropdownMenuItem onSelect={() => setRenaming(session)}>
                        <Pencil />
                        Rename
                      </DropdownMenuItem>
                      <DropdownMenuItem variant="destructive" onSelect={() => setDeleting(session)}>
                        <Trash2 />
                        Delete
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}

      <RenameDialog
        target={
          renaming
            ? {
                id: renaming.id,
                name: renaming.title || formatSessionFallbackTitle(renaming.created_at),
              }
            : null
        }
        title="Rename conversation"
        description="Lyra named this after its first message. Call it whatever you look for it by."
        label="Name"
        pending={renameSession.isPending}
        onOpenChange={(open) => {
          if (!open) setRenaming(null)
        }}
        onRename={(sessionId, title) => renameSession.mutateAsync({ sessionId, title })}
      />

      <AlertDialog
        open={deleting !== null}
        onOpenChange={(open) => {
          if (!open) setDeleting(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete this conversation?</AlertDialogTitle>
            <AlertDialogDescription>
              Every message in it goes with it. Your documents and the class profile are not
              affected. This cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <Button
              variant="destructive"
              disabled={deleteSession.isPending}
              onClick={() => void onConfirmDelete()}
            >
              {deleteSession.isPending ? <Spinner /> : null}
              Delete
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
