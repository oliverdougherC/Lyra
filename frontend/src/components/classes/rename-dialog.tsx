'use client'

import { useId, useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Spinner } from '@/components/ui/spinner'
import { ApiError } from '@/lib/api'

type RenameDialogProps = {
  /** The thing being renamed, or null when the dialog is closed. */
  target: { id: number; name: string } | null
  title: string
  description: string
  label: string
  pending: boolean
  onOpenChange: (open: boolean) => void
  onRename: (id: number, name: string) => Promise<unknown>
}

/**
 * One name, typed and saved.
 *
 * Shared by conversations and solution sets because both are named by a guess Lyra made -
 * the first message, the problem set's filename - and correcting a guess is the same
 * interaction whichever of the two it was.
 */
export function RenameDialog({
  target,
  title,
  description,
  label,
  pending,
  onOpenChange,
  onRename,
}: RenameDialogProps) {
  const inputId = useId()
  const [value, setValue] = useState(target?.name ?? '')
  const [error, setError] = useState<string | null>(null)

  // Reset during render rather than in an effect, so reopening on a different target never
  // shows the previous one's name for a frame.
  const [targetId, setTargetId] = useState(target?.id ?? null)
  if (target && target.id !== targetId) {
    setTargetId(target.id)
    setValue(target.name)
    setError(null)
  }

  const trimmed = value.trim()
  const unchanged = trimmed === (target?.name ?? '').trim()

  async function onSubmit() {
    if (!target || !trimmed || unchanged) return
    try {
      await onRename(target.id, trimmed)
      onOpenChange(false)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not rename this.')
    }
  }

  return (
    <Dialog open={Boolean(target)} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>

        <div className="grid gap-2">
          <Label htmlFor={inputId}>{label}</Label>
          <Input
            id={inputId}
            value={value}
            autoFocus
            autoComplete="off"
            onChange={(event) => setValue(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault()
                void onSubmit()
              }
            }}
          />
          {error ? (
            <p className="text-danger-text text-sm" role="alert">
              {error}
            </p>
          ) : null}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button disabled={!trimmed || unchanged || pending} onClick={() => void onSubmit()}>
            {pending ? <Spinner /> : null}
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
