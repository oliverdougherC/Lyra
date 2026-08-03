'use client'

import { useQueryClient } from '@tanstack/react-query'
import { FileText } from 'lucide-react'
import { useCallback, useRef, useState } from 'react'
import { toast } from 'sonner'

import { DocumentDropzone } from '@/components/documents/document-dropzone'
import { DocumentRow } from '@/components/documents/document-row'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton } from '@/components/ui/skeleton'
import { ApiError } from '@/lib/api'
import { parseTimestamp } from '@/lib/format'
import {
  documentKeys,
  useDeleteDocument,
  useDocuments,
  useReingestDocument,
  useUploadDocument,
} from '@/lib/hooks/use-documents'
import { profileKeys } from '@/lib/hooks/use-profile'
import { cn } from '@/lib/utils'
import type { DocumentRead, DocumentStatus } from '@/types'

type DocumentsPaneProps = {
  classId: number
  className?: string
  selectedDocumentId: number | null
  onSelectDocument: (documentId: number | null) => void
}

export function DocumentsPane({
  classId,
  className,
  selectedDocumentId,
  onSelectDocument,
}: DocumentsPaneProps) {
  const { data, isPending, isError, error, refetch } = useDocuments(classId)
  const uploadDocument = useUploadDocument(classId)
  const reingestDocument = useReingestDocument(classId)
  const deleteDocument = useDeleteDocument(classId)
  const queryClient = useQueryClient()

  const queueRef = useRef<File[]>([])
  const drainingRef = useRef(false)
  const [uploading, setUploading] = useState<string | null>(null)
  const [batch, setBatch] = useState({ done: 0, total: 0 })

  // Uploads run one at a time so the progress readout names a single file, and so a large
  // multi-file drop does not open a dozen request bodies at once. The queue lives in a ref
  // because the drain loop is driven by the drop event, not by a render.
  const drain = useCallback(async () => {
    if (drainingRef.current) return
    drainingRef.current = true
    try {
      while (queueRef.current.length > 0) {
        const next = queueRef.current[0]
        setUploading(next.name)
        try {
          await uploadDocument.mutateAsync(next)
        } catch (caught) {
          toast.error(
            caught instanceof ApiError ? caught.message : `Could not upload ${next.name}.`,
          )
        }
        queueRef.current = queueRef.current.slice(1)
        setBatch((current) => ({ ...current, done: current.done + 1 }))
      }
    } finally {
      drainingRef.current = false
      setUploading(null)
      setBatch({ done: 0, total: 0 })
    }
  }, [uploadDocument])

  const onFiles = useCallback(
    (files: File[]) => {
      queueRef.current = [...queueRef.current, ...files]
      setBatch((current) => ({ ...current, total: current.total + files.length }))
      void drain()
    },
    [drain],
  )

  const onRetry = useCallback(
    (documentId: number) => {
      reingestDocument.mutate(documentId, {
        onError: (caught) =>
          toast.error(
            caught instanceof ApiError ? caught.message : 'Could not reindex that document.',
          ),
      })
    },
    [reingestDocument],
  )

  const onDelete = useCallback(
    (document: DocumentRead) => {
      if (selectedDocumentId === document.id) onSelectDocument(null)
      deleteDocument.mutate(document.id, {
        onSuccess: () => toast.success(`${document.filename} deleted.`),
        onError: (caught) =>
          toast.error(
            caught instanceof ApiError ? caught.message : 'Could not delete that document.',
          ),
      })
    },
    [deleteDocument, onSelectDocument, selectedDocumentId],
  )

  const onStatus = useCallback(
    (documentId: number, status: DocumentStatus) => {
      queryClient.setQueryData<DocumentRead[]>(documentKeys.list(classId), (current) =>
        current?.map((document) =>
          document.id === documentId ? { ...document, ...status } : document,
        ),
      )
      if (status.state === 'ready') {
        queryClient.invalidateQueries({ queryKey: profileKeys.forClass(classId) })
      }
    },
    [classId, queryClient],
  )

  const documents = data
    ? [...data].sort(
        (a, b) => parseTimestamp(b.created_at).getTime() - parseTimestamp(a.created_at).getTime(),
      )
    : []

  return (
    <div
      className={cn('flex h-full flex-col', className)}
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => {
        event.preventDefault()
        onFiles(Array.from(event.dataTransfer.files))
      }}
    >
      <div className="flex items-center justify-between border-b px-4 py-3">
        <h2 className="text-xs font-medium tracking-[0.14em] uppercase">Documents</h2>
        {documents.length > 0 ? (
          <span className="text-text-tertiary text-xs tabular-nums">{documents.length}</span>
        ) : null}
      </div>

      <ScrollArea className="min-h-0 flex-1">
        <div className="p-3">
          {isPending ? (
            <div className="space-y-2" aria-busy="true" aria-label="Loading documents">
              {[0, 1, 2].map((index) => (
                <Skeleton key={index} className="h-14 w-full" />
              ))}
            </div>
          ) : isError ? (
            <Alert variant="destructive">
              <AlertTitle>Could not load documents</AlertTitle>
              <AlertDescription className="text-danger-text">
                <p>
                  {error instanceof ApiError
                    ? error.message
                    : 'Could not load documents. Try again.'}
                </p>
                <Button variant="outline" size="sm" className="mt-2" onClick={() => void refetch()}>
                  Retry
                </Button>
              </AlertDescription>
            </Alert>
          ) : documents.length === 0 ? (
            <Empty className="py-8">
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <FileText className="text-text-tertiary size-8" />
                </EmptyMedia>
                <EmptyTitle>No documents yet</EmptyTitle>
                <EmptyDescription>
                  Add a syllabus, lecture notes, or a problem set and Lyra will index it.
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          ) : (
            <ul className="space-y-2">
              {documents.map((document) => (
                <DocumentRow
                  key={document.id}
                  document={document}
                  selected={document.id === selectedDocumentId}
                  onSelect={(picked) =>
                    onSelectDocument(picked.id === selectedDocumentId ? null : picked.id)
                  }
                  onRetry={onRetry}
                  onDelete={onDelete}
                  onStatus={onStatus}
                />
              ))}
            </ul>
          )}
        </div>
      </ScrollArea>

      <div className="border-t bg-card p-3">
        <DocumentDropzone
          onFiles={onFiles}
          uploadingName={uploading}
          uploadedCount={batch.done}
          queueLength={Math.max(batch.total - batch.done, 0)}
        />
      </div>
    </div>
  )
}
