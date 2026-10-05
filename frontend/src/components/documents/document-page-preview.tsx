'use client'

import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { api, documentPagePath, loadProtectedAssetSource } from '@/lib/api'
import { effectiveDocumentName } from '@/lib/document-display'
import { useDocuments } from '@/lib/hooks/use-documents'

type Preview = { kind: 'image'; url: string } | { kind: 'text'; text: string; truncated: boolean }

/** The exact page named by a chat source event, loaded through the authenticated API. */
export function DocumentPagePreview({
  classId,
  documentId,
  page,
  onClose,
}: {
  classId: number
  documentId: number
  page: number
  onClose: () => void
}) {
  const documents = useDocuments(classId)
  const document = documents.data?.find((item) => item.id === documentId)
  const [preview, setPreview] = useState<Preview | null>(null)
  const [error, setError] = useState(false)
  const [retry, setRetry] = useState(0)

  useEffect(() => {
    if (!document) return
    const controller = new AbortController()
    let release: (() => void) | undefined
    setPreview(null)
    setError(false)
    void (async () => {
      try {
        if (document.mime === 'application/pdf' || document.mime.startsWith('image/')) {
          const asset = await loadProtectedAssetSource(
            documentPagePath(documentId, page),
            controller.signal,
          )
          if (controller.signal.aborted) {
            asset.release?.()
            return
          }
          release = asset.release
          setPreview({ kind: 'image', url: asset.url })
        } else if (page === 1) {
          const text = await api.getDocumentText(documentId, controller.signal)
          if (!controller.signal.aborted)
            setPreview({ kind: 'text', text: text.text, truncated: text.truncated })
        } else {
          setError(true)
        }
      } catch {
        if (!controller.signal.aborted) setError(true)
      }
    })()
    return () => {
      controller.abort()
      release?.()
    }
  }, [document, documentId, page, retry])

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="flex max-h-[90dvh] max-w-4xl flex-col overflow-hidden">
        <DialogHeader>
          <DialogTitle>
            {document ? effectiveDocumentName(document) : 'Source document'} · page {page}
          </DialogTitle>
          <DialogDescription>Exact source page used in this conversation.</DialogDescription>
        </DialogHeader>
        <div className="min-h-0 overflow-auto">
          {documents.isPending ? <p role="status">Loading source…</p> : null}
          {documents.isError ? (
            <div role="alert" className="space-y-2">
              <p>Could not load the source. Try again.</p>
              <Button
                variant="outline"
                size="sm"
                disabled={documents.isFetching}
                onClick={() => void documents.refetch()}
              >
                Retry source
              </Button>
            </div>
          ) : documents.data && !document ? (
            <p role="alert">This source is no longer in this class.</p>
          ) : null}
          {document && !preview && !error ? <p role="status">Opening page…</p> : null}
          {error ? (
            <div role="alert" className="space-y-2">
              <p>This page could not be opened. The source may have changed.</p>
              <Button variant="outline" size="sm" onClick={() => setRetry((value) => value + 1)}>
                Retry page
              </Button>
            </div>
          ) : null}
          {!error && document && preview?.kind === 'image' ? (
            <img
              src={preview.url}
              alt={`${document ? effectiveDocumentName(document) : 'Source'}, page ${page}`}
              className="mx-auto h-auto max-w-full"
              onError={() => setError(true)}
            />
          ) : !error && document && preview?.kind === 'text' ? (
            <div>
              <pre className="font-sans text-sm whitespace-pre-wrap">{preview.text}</pre>
              {preview.truncated ? (
                <p className="text-text-tertiary mt-3 text-xs">
                  Only the first part of this page is shown here.
                </p>
              ) : null}
            </div>
          ) : null}
        </div>
      </DialogContent>
    </Dialog>
  )
}
