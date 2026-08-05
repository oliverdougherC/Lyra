'use client'

import { useQueryClient } from '@tanstack/react-query'
import { FileText, PanelRightClose } from 'lucide-react'
import { motion, useReducedMotion } from 'motion/react'
import { useCallback, useEffect, useMemo, useRef, useState, type InputHTMLAttributes } from 'react'
import { toast } from 'sonner'

import { BatchLoader } from '@/components/documents/batch-loader'
import {
  ACCEPTED_EXTENSIONS,
  DocumentDropzone,
  filesFromDrop,
  partitionFiles,
} from '@/components/documents/document-dropzone'
import { DocumentRow } from '@/components/documents/document-row'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton } from '@/components/ui/skeleton'
import { ApiError } from '@/lib/api'
import { parseTimestamp } from '@/lib/format'
import {
  isTerminal,
  documentKeys,
  useDeleteDocument,
  useDocuments,
  useReingestDocument,
  useUploadDocument,
} from '@/lib/hooks/use-documents'
import { profileKeys } from '@/lib/hooks/use-profile'
import { cn } from '@/lib/utils'
import type { DocumentRead, DocumentState, DocumentStatus } from '@/types'

type DocumentsPaneProps = {
  classId: number
  className?: string
  selectedDocumentId: number | null
  onSelectDocument: (documentId: number | null) => void
  /** When set, the pane draws its own header with a close control (desktop column). */
  onClose?: () => void
}

export function DocumentsPane({
  classId,
  className,
  selectedDocumentId,
  onSelectDocument,
  onClose,
}: DocumentsPaneProps) {
  const queueRef = useRef<File[]>([])
  const drainingRef = useRef(false)
  // Hoisted to the pane root: the collapsed strip header's Upload button must be able to
  // open the pickers even when the dropzone (and its inputs) are not rendered.
  const fileInputRef = useRef<HTMLInputElement>(null)
  const folderInputRef = useRef<HTMLInputElement>(null)
  const [terminalStateById, setTerminalStateById] = useState(() => new Map<number, DocumentState>())
  const [uploading, setUploading] = useState<string | null>(null)
  const [rejectedFiles, setRejectedFiles] = useState<string[] | null>(null)
  const [batch, setBatch] = useState<{
    total: number
    uploaded: number
    failed: number
    documentIds: number[]
    currentName: string | null
  }>({ total: 0, uploaded: 0, failed: 0, documentIds: [], currentName: null })
  const batchActive = batch.total > 0

  const { data, isPending, isError, error, refetch } = useDocuments(classId, {
    // While a batch is draining or ingesting, keep the list fresh so the batch loader
    // reports real stage verbs (Reading, Splitting, ...) and terminal counts.
    refetchInterval: batchActive ? 1500 : false,
  })
  const uploadDocument = useUploadDocument(classId)
  const reingestDocument = useReingestDocument(classId)
  const deleteDocument = useDeleteDocument(classId)
  const queryClient = useQueryClient()
  const reduceMotion = useReducedMotion()

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
        setBatch((current) => ({ ...current, currentName: next.name }))
        try {
          const created = await uploadDocument.mutateAsync(next)
          setBatch((current) => ({
            ...current,
            documentIds: [...current.documentIds, created.id],
          }))
        } catch (caught) {
          setBatch((current) => ({ ...current, failed: current.failed + 1 }))
          toast.error(
            caught instanceof ApiError ? caught.message : `Could not upload ${next.name}.`,
          )
        }
        queueRef.current = queueRef.current.slice(1)
        setBatch((current) => ({ ...current, uploaded: current.uploaded + 1 }))
      }
    } finally {
      drainingRef.current = false
      setUploading(null)
      setBatch((current) => ({ ...current, currentName: null }))
    }
  }, [uploadDocument])

  const onFiles = (files: File[]) => {
    const { accepted, rejected } = partitionFiles(files)
    setRejectedFiles(rejected.length > 0 ? rejected : null)
    if (accepted.length === 0) return
    queueRef.current = [...queueRef.current, ...accepted]
    setBatch((current) => ({ ...current, total: current.total + accepted.length }))
    void drain()
  }

  const documentsById = useMemo(
    () => new Map(data?.map((document) => [document.id, document]) ?? []),
    [data],
  )
  const batchTerminalStates = batch.documentIds
    .map((id) => terminalStateById.get(id) ?? documentsById.get(id)?.state)
    .filter((state): state is DocumentState => state !== undefined && isTerminal(state))
  const terminalCount = batchTerminalStates.length
  const failedDocumentCount = batchTerminalStates.filter((state) => state === 'failed').length
  const successfulDocumentCount = terminalCount - failedDocumentCount
  const batchFailureCount = batch.failed + failedDocumentCount
  const batchFinished =
    batchActive && batch.uploaded >= batch.total && terminalCount >= batch.total - batch.failed
  // Once every uploaded document has finished ingesting, let the finished summary linger
  // briefly, then clear the batch so the next drop starts from a clean counter.
  useEffect(() => {
    if (!batchFinished) return
    const timer = window.setTimeout(() => {
      setTerminalStateById(new Map())
      setBatch({ total: 0, uploaded: 0, failed: 0, documentIds: [], currentName: null })
    }, 2000)
    return () => window.clearTimeout(timer)
  }, [batchFinished])

  const batchDocument = batch.documentIds
    .map((id) => documentsById.get(id))
    .find(
      (document) =>
        document !== undefined && !isTerminal(terminalStateById.get(document.id) ?? document.state),
    )
  const batchTitle = uploading
    ? `Uploading ${batch.currentName ?? ''}`
    : batchDocument
      ? `${STATE_ACTIONS[batchDocument.state]} ${batchDocument.filename}`
      : batchFinished
        ? batchFailureCount === 0
          ? 'All documents processed'
          : batchFailureCount === 1
            ? '1 item failed'
            : `${batchFailureCount} items failed`
        : 'Preparing documents'

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
      if (isTerminal(status.state)) {
        setTerminalStateById((current) => {
          if (current.get(documentId) === status.state) return current
          const next = new Map(current)
          next.set(documentId, status.state)
          return next
        })
      }
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
      className={cn('flex h-full min-h-0 flex-col overflow-hidden', className)}
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => {
        event.preventDefault()
        void filesFromDrop(event.dataTransfer).then(({ files }) => onFiles(files))
      }}
    >
      <input
        ref={fileInputRef}
        type="file"
        id="document-upload"
        name="documents"
        multiple
        accept={ACCEPTED_EXTENSIONS.join(',')}
        className="sr-only"
        aria-label="Choose documents to upload"
        onChange={(event) => {
          onFiles(Array.from(event.target.files ?? []))
          event.target.value = ''
        }}
      />
      <input
        ref={folderInputRef}
        {...({ webkitdirectory: '' } as InputHTMLAttributes<HTMLInputElement> & {
          webkitdirectory?: string
        })}
        type="file"
        id="folder-upload"
        name="folder"
        multiple
        accept={ACCEPTED_EXTENSIONS.join(',')}
        className="sr-only"
        aria-label="Choose a folder of documents to upload"
        onChange={(event) => {
          onFiles(Array.from(event.target.files ?? []))
          event.target.value = ''
        }}
      />
      {/* Header height is matched to the tutor pane's rather than derived from this row's
          controls, so the rule under both columns is one line. */}
      {onClose ? (
        <div className="flex h-9 shrink-0 items-center gap-2 border-b px-3 lg:h-10">
          <h2 className="text-xs font-medium tracking-[0.14em] uppercase">Documents</h2>
          {documents.length > 0 ? (
            <span className="text-text-tertiary text-xs tabular-nums">{documents.length}</span>
          ) : null}
          <Button
            variant="ghost"
            size="icon"
            className="ml-auto size-8"
            onClick={onClose}
            aria-label="Hide the documents panel"
          >
            <PanelRightClose />
          </Button>
        </div>
      ) : null}

      <ScrollArea id="documents-pane-body" className="min-h-0 flex-1 overflow-hidden">
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
              {documents.map((document, index) => (
                <motion.li
                  key={document.id}
                  layout
                  initial={reduceMotion ? { opacity: 0 } : { opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{
                    duration: 0.25,
                    delay: Math.min(index, 5) * 0.05,
                    ease: [0.25, 0.1, 0.3, 1],
                  }}
                >
                  <DocumentRow
                    document={document}
                    selected={document.id === selectedDocumentId}
                    onSelect={(picked) =>
                      onSelectDocument(picked.id === selectedDocumentId ? null : picked.id)
                    }
                    onRetry={onRetry}
                    onDelete={onDelete}
                    onStatus={onStatus}
                  />
                </motion.li>
              ))}
            </ul>
          )}
        </div>
      </ScrollArea>

      {/* Vertical padding matches the composer's bar; horizontal stays at the list's inset so
          the well still lines up with the rows above it. */}
      <div className="shrink-0 border-t bg-background px-3 py-4">
        {batchActive ? (
          <BatchLoader
            title={batchTitle}
            detail={batchDocument?.stage_detail}
            processed={successfulDocumentCount}
            total={batch.total}
            complete={batchFinished}
            failed={batchFailureCount}
            className="mb-3"
          />
        ) : null}
        <DocumentDropzone
          onFiles={onFiles}
          rejectedFiles={rejectedFiles}
          uploadingName={uploading}
          uploadedCount={batch.uploaded}
          queueLength={Math.max(batch.total - batch.uploaded, 0)}
          fileInputRef={fileInputRef}
          folderInputRef={folderInputRef}
        />
      </div>
    </div>
  )
}

const STATE_ACTIONS: Record<DocumentState, string> = {
  pending: 'Queued',
  parsing: 'Reading',
  chunking: 'Splitting',
  embedding: 'Indexing',
  extracting: 'Analyzing',
  ready: 'Ready',
  failed: 'Failed',
  unsupported: 'Unsupported',
}
