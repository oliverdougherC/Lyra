'use client'

import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'

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

type EditSession = { documentId: number; classId: number; expected: string | null }

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
  const [session, setSession] = useState<EditSession>(() => ({
    documentId: document.id,
    classId: document.class_id,
    expected: document.nickname ?? null,
  }))
  const [conflict, setConflict] = useState<{ current: DocumentRead | null } | null>(null)
  const active = useRef({ open, documentId: document.id })
  active.current = { open, documentId: document.id }
  const queryClient = useQueryClient()

  useEffect(() => {
    if (!open) return
    setSession({
      documentId: document.id,
      classId: document.class_id,
      expected: document.nickname ?? null,
    })
    setDraft(document.nickname ?? '')
    setError(null)
    setConflict(null)
    // A refetch of this document must not replace the draft or its CAS baseline.
  }, [open, document.id, document.class_id])

  async function save(value: string | null) {
    if (saving || conflict || session.documentId !== document.id) return
    setSaving(true)
    setError(null)
    try {
      await api.updateDocumentNickname(session.documentId, value, session.expected)
      await queryClient.invalidateQueries({ queryKey: documentKeys.list(session.classId) })
      if (active.current.open && active.current.documentId === session.documentId)
        onOpenChange(false)
    } catch (caught) {
      if (!active.current.open || active.current.documentId !== session.documentId) return
      setError(
        caught instanceof ApiError ? caught.message : 'Could not save this nickname. Try again.',
      )
      if (caught instanceof ApiError && caught.status === 409) {
        try {
          const current = await api.getDocument(session.documentId)
          if (active.current.open && active.current.documentId === session.documentId) {
            setConflict({ current })
            void queryClient.invalidateQueries({ queryKey: documentKeys.list(session.classId) })
          }
        } catch {
          if (active.current.open && active.current.documentId === session.documentId) {
            setError(
              'The nickname changed, but its current value could not be loaded. Cancel and reopen to review it.',
            )
            // Keep the old baseline; a blind retry cannot overwrite the newer value.
            setConflict({ current: null })
          }
        }
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
          {conflict?.current ? (
            <div className="text-sm" role="status">
              <p>
                Current name: {conflict.current.nickname?.trim() || conflict.current.filename}. Your
                draft is still here.
              </p>
              {conflict.current.id === session.documentId ? (
                <div className="mt-2 flex flex-wrap gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => {
                      setDraft(conflict.current?.nickname ?? '')
                      setSession({ ...session, expected: conflict.current?.nickname ?? null })
                      setConflict(null)
                      setError(null)
                    }}
                  >
                    Use current name
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => {
                      setSession({ ...session, expected: conflict.current?.nickname ?? null })
                      setConflict(null)
                      setError(null)
                    }}
                  >
                    Keep my draft
                  </Button>
                </div>
              ) : null}
            </div>
          ) : null}
          <DialogFooter>
            {session.expected ? (
              <Button
                type="button"
                variant="outline"
                disabled={saving || !!conflict || session.documentId !== document.id}
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
            <Button
              type="submit"
              disabled={saving || !!conflict || session.documentId !== document.id}
            >
              {saving ? 'Saving…' : 'Save'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
