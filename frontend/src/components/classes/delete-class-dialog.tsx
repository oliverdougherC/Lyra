'use client'

import { useId, useState } from 'react'
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
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Spinner } from '@/components/ui/spinner'
import { ApiError } from '@/lib/api'
import { useDeleteClass } from '@/lib/hooks/use-classes'
import type { ClassRead } from '@/types'

type DeleteClassDialogProps = {
  klass: ClassRead | null
  onOpenChange: (open: boolean) => void
  /** Called once the class is gone, for callers standing on a page that no longer exists. */
  onDeleted?: () => void
}

export function DeleteClassDialog({ klass, onOpenChange, onDeleted }: DeleteClassDialogProps) {
  const inputId = useId()
  const [typed, setTyped] = useState('')
  const [error, setError] = useState<string | null>(null)
  const deleteClass = useDeleteClass()

  // Reset during render rather than in an effect, so reopening on a different class never
  // shows the previous class's typed confirmation for a frame.
  const [targetId, setTargetId] = useState(klass?.id ?? null)
  if (klass && klass.id !== targetId) {
    setTargetId(klass.id)
    setTyped('')
    setError(null)
  }

  const phrase = klass?.code?.trim() || klass?.name.trim() || ''
  const matches = typed.trim().toLowerCase() === phrase.toLowerCase()

  async function onConfirm() {
    if (!klass || !matches) return
    try {
      await deleteClass.mutateAsync(klass.id)
      toast.success(`${klass.name} deleted.`)
      onOpenChange(false)
      onDeleted?.()
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not delete this class.')
    }
  }

  return (
    <AlertDialog open={Boolean(klass)} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete {klass?.name}?</AlertDialogTitle>
          <AlertDialogDescription>
            This permanently removes its documents, their indexed text, every conversation, and the
            class profile. It cannot be undone.
          </AlertDialogDescription>
        </AlertDialogHeader>

        <div className="grid gap-2">
          <Label htmlFor={inputId}>
            Type <span className="font-mono font-semibold">{phrase}</span> to confirm
          </Label>
          <Input
            id={inputId}
            value={typed}
            autoComplete="off"
            onChange={(event) => setTyped(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && matches) {
                event.preventDefault()
                void onConfirm()
              }
            }}
          />
          {error ? (
            <p className="text-danger-text text-sm" role="alert">
              {error}
            </p>
          ) : null}
        </div>

        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <Button
            variant="destructive"
            disabled={!matches || deleteClass.isPending}
            onClick={onConfirm}
          >
            {deleteClass.isPending ? <Spinner /> : null}
            Delete class
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
