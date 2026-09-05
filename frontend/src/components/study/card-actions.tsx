'use client'

import { useEffect, useState } from 'react'
import { MoreVertical, Pencil, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Label } from '@/components/ui/label'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { ApiError } from '@/lib/api'
import { useDeleteCard, useUpdateCard } from '@/lib/hooks/use-study'
import type { CardContent, SessionCard } from '@/types'

/** Card quality actions never count as a recall rating. The session retains queue ownership. */
export function CardActions({
  deckId,
  current,
  disabled,
  onOpenChange,
  onUpdated,
  onRemoved,
}: {
  deckId: number
  current: SessionCard
  disabled: boolean
  onOpenChange: (open: boolean) => void
  onUpdated: (content: CardContent) => void
  onRemoved: () => void
}) {
  const update = useUpdateCard(deckId)
  const remove = useDeleteCard(deckId)
  const [menuOpen, setMenuOpen] = useState(false)
  const [mode, setMode] = useState<'edit' | 'remove' | null>(null)
  const [draft, setDraft] = useState(current.card)
  const [error, setError] = useState<string | null>(null)
  const pending = update.isPending || remove.isPending

  useEffect(() => {
    onOpenChange(menuOpen || mode !== null)
  }, [menuOpen, mode, onOpenChange])

  function open(next: 'edit' | 'remove' | null) {
    if (pending) return
    if (next === 'edit') setDraft(current.card)
    setError(null)
    setMode(next)
    onOpenChange(next !== null)
  }

  async function save() {
    if (pending || !draft.front.trim() || !draft.back.trim() || !draft.topic.trim()) return
    try {
      const saved = await update.mutateAsync({ partId: current.part_id, body: draft })
      onUpdated(saved.card)
      setMode(null)
      onOpenChange(false)
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? caught.message
          : 'Could not save this card. Your edits are still here.',
      )
    }
  }

  async function confirmRemove() {
    if (pending) return
    try {
      await remove.mutateAsync(current.part_id)
      onOpenChange(false)
      onRemoved()
    } catch (caught) {
      setError(
        caught instanceof ApiError ? caught.message : 'Could not remove this card. Try again.',
      )
    }
  }

  return (
    <>
      <DropdownMenu onOpenChange={setMenuOpen}>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="icon" aria-label="Card actions" disabled={disabled}>
            <MoreVertical />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onSelect={() => open('edit')}>
            <Pencil />
            Edit card
          </DropdownMenuItem>
          <DropdownMenuItem variant="destructive" onSelect={() => open('remove')}>
            <Trash2 />
            Remove card
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <Dialog
        open={mode === 'edit'}
        onOpenChange={(isOpen) => {
          if (!isOpen) open(null)
        }}
      >
        <DialogContent className="max-h-[85dvh] overflow-y-auto" showCloseButton={!pending}>
          <DialogHeader>
            <DialogTitle>Edit card</DialogTitle>
            <DialogDescription>
              Correct the question, answer or topic. Review history stays with this card.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-2">
            <Label htmlFor="card-front">Question</Label>
            <Textarea
              id="card-front"
              value={draft.front}
              disabled={pending}
              onChange={(event) => setDraft({ ...draft, front: event.target.value })}
              rows={4}
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="card-back">Answer</Label>
            <Textarea
              id="card-back"
              value={draft.back}
              disabled={pending}
              onChange={(event) => setDraft({ ...draft, back: event.target.value })}
              rows={4}
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="card-topic">Topic</Label>
            <Input
              id="card-topic"
              value={draft.topic}
              disabled={pending}
              onChange={(event) => setDraft({ ...draft, topic: event.target.value })}
            />
          </div>
          {error ? (
            <p role="alert" className="text-danger-text text-sm">
              {error}
            </p>
          ) : null}
          <DialogFooter>
            <Button variant="outline" disabled={pending} onClick={() => open(null)}>
              Cancel
            </Button>
            <Button
              disabled={pending || !draft.front.trim() || !draft.back.trim() || !draft.topic.trim()}
              onClick={() => void save()}
            >
              {pending ? 'Saving…' : 'Save card'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <AlertDialog
        open={mode === 'remove'}
        onOpenChange={(isOpen) => {
          if (!isOpen) open(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Remove this card?</AlertDialogTitle>
            <AlertDialogDescription>
              This removes the card and its review history from the deck. Other cards stay. This
              cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <p className="text-sm line-clamp-3">{current.card.front}</p>
          {error ? (
            <p role="alert" className="text-danger-text text-sm">
              {error}
            </p>
          ) : null}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={pending}>Cancel</AlertDialogCancel>
            <Button variant="destructive" disabled={pending} onClick={() => void confirmRemove()}>
              {pending ? 'Removing…' : 'Remove card'}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
