'use client'

import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

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
import { ApiError, api } from '@/lib/api'
import { documentKeys } from '@/lib/hooks/use-documents'
import type { DocumentRead } from '@/types'

export function DocumentNicknameDialog({
  document,
  open,
  onOpenChange,
}: {
  document: DocumentRead
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [draft, setDraft] = useState(document.nickname ?? '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const queryClient = useQueryClient()

  useEffect(() => {
    if (!open) return
    setDraft(document.nickname ?? '')
    setError(null)
    // Deliberately only on opening: a refetch cannot replace an unsaved edit.
  }, [open])

  async function save(value: string | null) {
    if (saving) return
    setSaving(true)
    setError(null)
    try {
      await api.updateDocumentNickname(document.id, value, document.nickname ?? null)
      await queryClient.invalidateQueries({ queryKey: documentKeys.list(document.class_id) })
      onOpenChange(false)
    } catch (caught) {
      setError(
        caught instanceof ApiError ? caught.message : 'Could not save this nickname. Try again.',
      )
      if (caught instanceof ApiError && caught.status === 409) {
        void queryClient.invalidateQueries({ queryKey: documentKeys.list(document.class_id) })
      }
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !saving && onOpenChange(next)}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Edit nickname</DialogTitle>
          <DialogDescription>Original file: {document.filename}</DialogDescription>
        </DialogHeader>
        <form
          onSubmit={(event) => {
            event.preventDefault()
            void save(draft)
          }}
          className="space-y-3"
        >
          <Input
            aria-label="Document nickname"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            maxLength={120}
            autoFocus
            disabled={saving}
          />
          {error ? (
            <p role="alert" className="text-danger-text text-sm">
              {error}
            </p>
          ) : null}
          <DialogFooter>
            {document.nickname ? (
              <Button
                type="button"
                variant="outline"
                disabled={saving}
                onClick={() => void save(null)}
              >
                Use original name
              </Button>
            ) : null}
            <Button
              type="button"
              variant="ghost"
              disabled={saving}
              onClick={() => onOpenChange(false)}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={saving}>
              {saving ? 'Saving…' : 'Save'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
