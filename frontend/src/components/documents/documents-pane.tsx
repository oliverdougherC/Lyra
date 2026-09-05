'use client'

import { useQueryClient } from '@tanstack/react-query'
import { FileText, FolderInput, PanelRightClose, Search, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState, type InputHTMLAttributes } from 'react'
import { useRouter } from '@/router/hooks'
import { toast } from 'sonner'

import { BatchLoader } from '@/components/documents/batch-loader'
import {
  ACCEPTED_EXTENSIONS,
  DocumentDropzone,
  filesFromDrop,
  partitionFiles,
} from '@/components/documents/document-dropzone'
import { DocumentRow } from '@/components/documents/document-row'
import { MoveDocumentDialog } from '@/components/documents/move-document-dialog'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
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
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { Input } from '@/components/ui/input'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton } from '@/components/ui/skeleton'
import { Spinner } from '@/components/ui/spinner'
import { ApiError } from '@/lib/api'
import { formatCount, parseTimestamp } from '@/lib/format'
import { documentStudyTitle } from '@/lib/handoff'
import {
  batchSummaryTitle,
  classifyBatch,
  isTerminal,
  documentKeys,
  useDeleteDocument,
  useDocuments,
  useRecognizeDocument,
  useReingestDocument,
  useUploadDocument,
} from '@/lib/hooks/use-documents'
import { profileKeys } from '@/lib/hooks/use-profile'
import { useCreateQuiz } from '@/lib/hooks/use-study'
import { cn } from '@/lib/utils'
import type { DocumentRead, DocumentState, DocumentStatus } from '@/types'

/**
 * What this list of documents is for.
 *
 * `ask` is the column beside the conversation, where picking a document narrows the next
 * question to it. `manage` is the class hub's Documents tab, where the same list is the
 * filing cabinet: several files at a time, moved between classes or thrown away.
 *
 * One component rather than two because everything underneath - the upload queue, the
 * per-file ingestion poll, the batch readout - is the same work, and the copy that was not
 * being looked at is the one that would rot.
 */
type DocumentsPaneVariant = 'ask' | 'manage'

type DocumentsPaneProps = {
  classId: number
  className?: string
  variant?: DocumentsPaneVariant
  /** Scoping the conversation. Unused, and unread, in the `manage` variant. */
  selectedDocumentId?: number | null
  onSelectDocument?: (documentId: number | null) => void
  /** When set, the pane draws its own header with a close control (desktop column). */
  onClose?: () => void
}

export function DocumentsPane({
  classId,
  className,
  variant = 'ask',
  selectedDocumentId = null,
  onSelectDocument,
  onClose,
}: DocumentsPaneProps) {
  const managing = variant === 'manage'
  // Which files the next bulk action applies to. Ids rather than documents, so a list that
  // refetches mid-selection does not hold onto rows that have since changed state.
  const [checkedIds, setCheckedIds] = useState<number[]>([])
  const [moving, setMoving] = useState<DocumentRead[]>([])
  const [deleting, setDeleting] = useState<DocumentRead[]>([])
  const [deletingBusy, setDeletingBusy] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const queueRef = useRef<File[]>([])
  const drainingRef = useRef(false)
  // Hoisted to the pane root: the collapsed strip header's Upload button must be able to
  // open the pickers even when the dropzone (and its inputs) are not rendered.
  const fileInputRef = useRef<HTMLInputElement>(null)
  const folderInputRef = useRef<HTMLInputElement>(null)
  const [terminalStateById, setTerminalStateById] = useState(() => new Map<number, DocumentState>())
  const [uploading, setUploading] = useState<string | null>(null)
  // A dropped folder is walked before a single byte is uploaded, and a term of notes takes
  // long enough that silence reads as nothing having happened.
  const [scanning, setScanning] = useState(false)
  const [rejectedFiles, setRejectedFiles] = useState<string[] | null>(null)
  const [batch, setBatch] = useState<{
    total: number
    uploaded: number
    failed: number
    documentIds: number[]
    currentName: string | null
  }>({ total: 0, uploaded: 0, failed: 0, documentIds: [], currentName: null })
  const batchActive = batch.total > 0

  // The list polls itself while anything in it is mid-ingestion, so no interval is asked
  // for here. Tying it to the upload batch was the bug: the batch clears a couple of
  // seconds after the last byte is sent, and `extracting` - a model pass over the whole
  // document - runs long after that, so the readout sat on "Analyzing" until a reload.
  const router = useRouter()
  const { data, isPending, isError, error, refetch } = useDocuments(classId)
  const createQuiz = useCreateQuiz(classId)
  const uploadDocument = useUploadDocument(classId)
  const reingestDocument = useReingestDocument(classId)
  const recognizeDocument = useRecognizeDocument(classId)
  const deleteDocument = useDeleteDocument(classId)
  const queryClient = useQueryClient()
  // A 36-document class is the real class: a filter turns a scroll hunt into a glance
  // (ui-overhaul 2.6). Filters by filename, case-insensitive.
  const [filter, setFilter] = useState('')

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
  // Classified by what each terminal state means, through the same helper the rows use, so
  // an `unsupported` item is counted as needing attention rather than as a success. Upload
  // requests that never reached the server (`batch.failed`) never produced a document to
  // classify, so they are added to the attention total here.
  const outcome = classifyBatch(batchTerminalStates)
  const successfulDocumentCount = outcome.ready
  const batchAttentionCount = batch.failed + outcome.needsAttention
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
        ? batchSummaryTitle(batchAttentionCount)
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

  const onRecognize = useCallback(
    (documentId: number) => {
      recognizeDocument.mutate(documentId, {
        onError: (caught) =>
          toast.error(
            caught instanceof ApiError ? caught.message : 'Could not start reading that document.',
          ),
      })
    },
    [recognizeDocument],
  )

  const onDelete = useCallback((document: DocumentRead) => {
    setDeleteError(null)
    setDeleting([document])
  }, [])

  /**
   * One document into a practice quiz, named after the file, at the study defaults. The
   * generation screen it lands on shows the progress; the artifact is renameable there
   * and listed under Study like any other.
   */
  const onPractice = useCallback(
    (document: DocumentRead) => {
      createQuiz.mutate(
        { title: documentStudyTitle(document.filename), document_ids: [document.id] },
        {
          onSuccess: (artifact) => router.push(`/classes/${classId}/study/${artifact.id}`),
          onError: (caught) =>
            toast.error(
              caught instanceof ApiError
                ? caught.message
                : 'Could not make practice questions from that document.',
            ),
        },
      )
    },
    [classId, createQuiz, router],
  )

  const onRowSelect = useCallback(
    (document: DocumentRead) => {
      if (managing) {
        setCheckedIds((current) =>
          current.includes(document.id)
            ? current.filter((id) => id !== document.id)
            : [...current, document.id],
        )
        return
      }
      onSelectDocument?.(document.id === selectedDocumentId ? null : document.id)
    },
    [managing, onSelectDocument, selectedDocumentId],
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
      queryClient.setQueryData<DocumentRead[]>(documentKeys.list(classId), (current) => {
        const listed = current?.find((document) => document.id === documentId)
        // Same array back when the poll reported nothing new. Every row now reports each
        // poll rather than only its last one, and a class of thirty-six documents polls
        // roughly eighteen times a second between them: writing an identical list on each
        // of those would re-render every row and every count that reads the list, for no
        // change at all.
        if (!current || !listed || !hasProgressed(listed, status)) return current
        return current.map((document) =>
          document.id === documentId ? { ...document, ...status } : document,
        )
      })
      if (status.state === 'ready') {
        queryClient.invalidateQueries({ queryKey: profileKeys.forClass(classId) })
      }
    },
    [classId, queryClient],
  )

  const allDocuments = data
    ? [...data].sort(
        (a, b) => parseTimestamp(b.created_at).getTime() - parseTimestamp(a.created_at).getTime(),
      )
    : []
  const query = filter.trim().toLowerCase()
  const documents = query
    ? allDocuments.filter((document) => document.filename.toLowerCase().includes(query))
    : allDocuments
  const checked = allDocuments.filter((document) => checkedIds.includes(document.id))
  const hiddenCheckedCount = checked.filter((document) => !documents.includes(document)).length
  // The filter is worth offering once a list is long enough to hunt through, and only where
  // the list is the whole surface (manage), not the narrow chat column.
  const showFilter = managing && (allDocuments.length > 8 || filter.length > 0)

  async function onDeleteConfirmed() {
    if (deletingBusy) return
    setDeletingBusy(true)
    setDeleteError(null)
    const results = await Promise.allSettled(
      deleting.map((document) => deleteDocument.mutateAsync(document.id)),
    )
    const failed = deleting.filter((_, index) => results[index].status === 'rejected')
    const deletedIds = deleting
      .filter((_, index) => results[index].status === 'fulfilled')
      .map((document) => document.id)
    if (deletedIds.length > 0) {
      toast.success(`${formatCount(deletedIds.length, 'file')} deleted.`)
      setCheckedIds((current) => current.filter((id) => !deletedIds.includes(id)))
      if (selectedDocumentId !== null && deletedIds.includes(selectedDocumentId))
        onSelectDocument?.(null)
    }
    setDeleting(failed)
    if (failed.length > 0)
      setDeleteError(`${formatCount(failed.length, 'file')} could not be deleted. Try again.`)
    setDeletingBusy(false)
  }

  return (
    <div
      className={cn('flex h-full min-h-0 flex-col overflow-hidden', className)}
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => {
        event.preventDefault()
        // `filesFromDrop` claims the dropped entries synchronously, so it has to be called
        // here rather than after any await: the item list is gone once this handler yields.
        void filesFromDrop(event.dataTransfer, () => setScanning(true)).then(({ files }) => {
          setScanning(false)
          onFiles(files)
        })
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

      {managing && checked.length > 0 ? (
        // Present only once something is picked, rather than sitting there greyed out: a
        // permanently visible bar of dead controls says the list is mostly buttons, when
        // in fact it is mostly files. Rendered away rather than hidden, so its controls
        // are not left in the tab order describing an action that cannot be taken.
        <div className="flex shrink-0 flex-wrap items-center gap-2 border-b px-3 py-2">
          <span className="text-text-secondary text-sm tabular-nums">
            {formatCount(checked.length, 'file')} selected
            {hiddenCheckedCount > 0 ? ` · ${hiddenCheckedCount} hidden by filter` : ''}
          </span>
          <Button variant="outline" size="sm" className="h-8" onClick={() => setMoving(checked)}>
            <FolderInput aria-hidden className="size-3.5" />
            Move to class
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="text-danger-text hover:text-danger-text h-8"
            onClick={() => {
              setDeleteError(null)
              setDeleting(checked)
            }}
          >
            <Trash2 aria-hidden className="size-3.5" />
            Delete
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="ml-auto h-8"
            onClick={() => setCheckedIds([])}
          >
            Clear
          </Button>
        </div>
      ) : null}

      {showFilter ? (
        <div className="relative shrink-0 border-b px-3 py-2">
          <Search
            aria-hidden
            className="text-text-tertiary pointer-events-none absolute top-1/2 left-6 size-4 -translate-y-1/2"
          />
          <Input
            type="search"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            placeholder={`Filter ${formatCount(allDocuments.length, 'document')}`}
            aria-label="Filter documents by name"
            className="h-9 pl-9"
          />
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
          ) : allDocuments.length === 0 ? (
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
          ) : documents.length === 0 ? (
            // The list is non-empty but the filter matched nothing: say so plainly rather
            // than showing the same blank the truly-empty class shows.
            <div className="px-1 py-6 text-center">
              <p className="text-text-tertiary text-sm">
                No documents match &ldquo;{filter.trim()}&rdquo;.
              </p>
              <Button variant="outline" size="sm" className="mt-2" onClick={() => setFilter('')}>
                Clear filter
              </Button>
            </div>
          ) : (
            // Plain list items: the row's own arrival is enough, and a re-animated entrance on
            // every poll (a busy class polls several times a second) would flicker the list.
            // This is also what retires the motion/react dependency from this pane.
            <ul className="space-y-2">
              {documents.map((document) => (
                <li key={document.id}>
                  <DocumentRow
                    document={document}
                    mode={variant}
                    selected={
                      managing
                        ? checkedIds.includes(document.id)
                        : document.id === selectedDocumentId
                    }
                    onSelect={onRowSelect}
                    onRetry={onRetry}
                    onRecognize={onRecognize}
                    onDelete={onDelete}
                    onStatus={onStatus}
                    onMove={managing ? (picked) => setMoving([picked]) : undefined}
                    onPractice={onPractice}
                  />
                </li>
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
            needsAttention={batchAttentionCount}
            className="mb-3"
          />
        ) : null}
        <DocumentDropzone
          rejectedFiles={rejectedFiles}
          uploadingName={uploading}
          scanning={scanning}
          uploadedCount={batch.uploaded}
          queueLength={Math.max(batch.total - batch.uploaded, 0)}
          fileInputRef={fileInputRef}
          folderInputRef={folderInputRef}
        />
      </div>

      {managing ? (
        <MoveDocumentDialog
          documents={moving}
          classId={classId}
          onOpenChange={(open) => {
            if (!open) setMoving([])
          }}
          onMoved={() => setCheckedIds([])}
        />
      ) : null}
      <AlertDialog
        open={deleting.length > 0}
        onOpenChange={(open) => {
          if (!open && !deletingBusy) setDeleting([])
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {formatCount(deleting.length, 'file')}?</AlertDialogTitle>
            <AlertDialogDescription>
              This removes the files and everything Lyra indexed from them. Answers will stop citing
              them. It cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <ul className="max-h-48 overflow-y-auto text-sm">
            {deleting.map((document) => (
              <li key={document.id} className="break-words">
                {document.filename}
              </li>
            ))}
          </ul>
          {deleteError ? (
            <p role="alert" className="text-danger-text text-sm">
              {deleteError}
            </p>
          ) : null}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deletingBusy}>Cancel</AlertDialogCancel>
            <Button
              variant="destructive"
              disabled={deletingBusy}
              onClick={() => void onDeleteConfirmed()}
            >
              {deletingBusy ? <Spinner /> : null}Delete
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

/** Whether a polled status says anything the list does not already show. */
function hasProgressed(listed: DocumentRead, status: DocumentStatus): boolean {
  return (
    listed.state !== status.state ||
    listed.stage_detail !== status.stage_detail ||
    listed.pages_done !== status.pages_done ||
    listed.pages_total !== status.pages_total ||
    listed.pages_skipped !== status.pages_skipped ||
    listed.error_message !== status.error_message
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
