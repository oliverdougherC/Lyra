'use client'

import { useQueryClient } from '@tanstack/react-query'
import {
  ChevronLeft,
  ChevronRight,
  FileText,
  FolderInput,
  PanelRightClose,
  Search,
  Trash2,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState, type InputHTMLAttributes } from 'react'
import { useRouter } from '@/router/hooks'
import { toast } from 'sonner'

import { useDocumentAttention } from '@/components/documents/attention-reveal'
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
import { formatCount } from '@/lib/format'
import { documentStudyTitle } from '@/lib/handoff'
import {
  clearUploadedReceipts,
  documentsInListOrder,
  isTerminal,
  documentKeys,
  useDeleteDocument,
  useDocuments,
  useRecognizeDocument,
  useReingestDocument,
  useUploadQueue,
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

export function DocumentsPane(props: DocumentsPaneProps) {
  // A class change must not carry filters, bulk selections or upload UI into another class.
  return <ClassDocumentsPane key={props.classId} {...props} />
}

function ClassDocumentsPane({
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
  // Hoisted to the pane root: the collapsed strip header's Upload button must be able to
  // open the pickers even when the dropzone (and its inputs) are not rendered.
  const fileInputRef = useRef<HTMLInputElement>(null)
  const folderInputRef = useRef<HTMLInputElement>(null)
  // A dropped folder is walked before a single byte is uploaded, and a term of notes takes
  // long enough that silence reads as nothing having happened.
  const [scanning, setScanning] = useState(false)
  const [rejectedFiles, setRejectedFiles] = useState<string[] | null>(null)

  // The list polls itself while anything in it is mid-ingestion, so no interval is asked
  // for here. Tying it to the upload batch was the bug: the batch clears a couple of
  // seconds after the last byte is sent, and `extracting` - a model pass over the whole
  // document - runs long after that, so the readout sat on "Analyzing" until a reload.
  const router = useRouter()
  const { data, isPending, isError, error, refetch } = useDocuments(classId)
  const createQuiz = useCreateQuiz(classId)
  const uploads = useUploadQueue(classId)
  const reingestDocument = useReingestDocument(classId)
  const recognizeDocument = useRecognizeDocument(classId)
  const deleteDocument = useDeleteDocument(classId)
  const queryClient = useQueryClient()
  // A 36-document class is the real class: a filter turns a scroll hunt into a glance
  // (ui-overhaul 2.6). Filters by filename, case-insensitive.
  const filterKey = `lyra:class:${classId}:files-query`
  const [filter, setFilter] = useState(() => {
    try {
      return sessionStorage.getItem(filterKey) ?? ''
    } catch {
      return ''
    }
  })
  useEffect(() => {
    try {
      sessionStorage.setItem(filterKey, filter)
    } catch {
      /* Filtering still works in memory. */
    }
  }, [filterKey, filter])

  const onFiles = (files: File[]) => {
    const { accepted, rejected } = partitionFiles(files)
    setRejectedFiles(rejected.length > 0 ? rejected : null)
    if (accepted.length > 0) uploads.enqueue(accepted)
  }

  const documentsById = useMemo(
    () => new Map(data?.map((document) => [document.id, document]) ?? []),
    [data],
  )
  const uploading = uploads.attempts.find((attempt) => attempt.state === 'uploading')?.label ?? null
  const failedUploads = uploads.attempts.filter(
    (attempt) => attempt.state === 'failed' || attempt.state === 'uncertain',
  )
  const pendingUploads = uploads.attempts.filter(
    (attempt) => attempt.state === 'uploading' || attempt.state === 'queued',
  )
  const uploadedCount = uploads.attempts.filter((attempt) => attempt.state === 'uploaded').length
  const batchDocument = uploads.attempts
    .filter((attempt) => attempt.documentId !== null)
    .map((attempt) => documentsById.get(attempt.documentId!))
    .find((document) => document && !isTerminal(document.state))
  const uploadedDocuments = uploads.attempts
    .filter((attempt) => attempt.state === 'uploaded')
    .map((attempt) =>
      attempt.documentId === null ? undefined : documentsById.get(attempt.documentId),
    )
  const batchActive =
    pendingUploads.length > 0 ||
    Boolean(batchDocument) ||
    uploadedDocuments.some((document) => !document)
  const batchFinished =
    uploads.attempts.length > 0 && !batchActive && uploadedDocuments.every(Boolean)
  const batchAttentionCount =
    failedUploads.length +
    uploadedDocuments.filter(
      (document) =>
        document &&
        (document.state === 'failed' ||
          document.state === 'unsupported' ||
          document.coverage_complete === false),
    ).length

  useEffect(() => {
    if (!batchFinished || uploadedCount === 0) return
    const timer = window.setTimeout(() => clearUploadedReceipts(classId), 2000)
    return () => window.clearTimeout(timer)
  }, [batchFinished, classId, uploadedCount])

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

  const allDocuments = data ? documentsInListOrder(data) : []
  // The "needs attention" half of the shared navigation contract: a `lyra-anchor=document-N`
  // arrival stands on the exact row (focus, scroll, announce, transient emphasis) and steps
  // through the rest of the affected documents. A reveal may temporarily hide a filter that
  // would hide the target - but it only borrows a clearing, never writing the pane's own
  // (persisted) filter, so the student's original survives unmount, tab changes, Back,
  // Forward, and reload.
  const attention = useDocumentAttention(allDocuments, data !== undefined, filter)
  // '' while the reveal borrows the clearing; otherwise the student's own filter applies.
  const effectiveFilter = attention.filterOverride ?? filter
  const query = effectiveFilter.trim().toLowerCase()
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
        void filesFromDrop(event.dataTransfer, () => setScanning(true)).then(
          ({ files, errors }) => {
            setScanning(false)
            uploads.reportScanErrors(errors)
            onFiles(files)
          },
        )
      }}
    >
      {/* The arrival announcement, kept mounted so a screen reader is listening when it
          fires; the key re-arms the region on each navigation so Back re-announces. */}
      <p key={`attention-${attention.navigationVersion}`} className="sr-only" aria-live="polite">
        {attention.announcement ?? ''}
      </p>
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
            value={effectiveFilter}
            // Typing (or clearing) is the student's intent: it lands in the pane's own
            // filter, and the reveal's temporary clearing steps aside for it.
            onChange={(event) => setFilter(event.target.value)}
            placeholder={`Filter ${formatCount(allDocuments.length, 'document')}`}
            aria-label="Filter documents by name"
            className="h-9 pl-9"
          />
        </div>
      ) : null}

      {attention.active && attention.attention.length >= 2 && attention.target ? (
        // A multi-item visit, one line tall: where the student is and how to walk the rest.
        // It exists only while an attention anchor is in the URL - Dismiss, Back, or the
        // items resolving all retire it, so the list never grows a permanent fixture. A
        // single affected item gets its row's emphasis and the announcement instead.
        <div
          role="group"
          aria-label={`Documents that need attention, ${attention.position} of ${attention.attention.length}`}
          className="border-border bg-danger-fill/40 flex shrink-0 items-center gap-2 border-b px-3 py-2"
        >
          <span className="text-danger-text shrink-0 text-xs font-medium tabular-nums">
            {attention.attention.length} need attention
          </span>
          <span className="text-text-tertiary min-w-0 truncate text-xs">
            {attention.position} of {attention.attention.length} · {attention.target.filename}
          </span>
          <span className="ml-auto flex shrink-0 items-center">
            <Button
              variant="ghost"
              size="icon"
              className="size-7"
              onClick={() => attention.step(-1)}
              aria-label="Previous document that needs attention"
            >
              <ChevronLeft aria-hidden className="size-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className="size-7"
              onClick={() => attention.step(1)}
              aria-label="Next document that needs attention"
            >
              <ChevronRight aria-hidden className="size-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className="size-7"
              onClick={attention.dismiss}
              aria-label="Dismiss documents that need attention"
            >
              <X aria-hidden className="size-4" />
            </Button>
          </span>
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
                No documents match &ldquo;{effectiveFilter.trim()}&rdquo;.
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
                    highlighted={document.id === attention.highlightedId}
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
            title={
              uploading
                ? `Uploading ${uploading}`
                : batchDocument
                  ? `${STATE_ACTIONS[batchDocument.state]} ${batchDocument.filename}`
                  : 'Preparing documents'
            }
            detail={batchDocument?.stage_detail}
            processed={uploadedCount}
            total={uploads.attempts.length}
            complete={false}
            needsAttention={failedUploads.length}
            className="mb-3"
          />
        ) : null}
        {batchFinished ? (
          <p className="text-text-secondary mb-2 text-sm">
            {batchAttentionCount === 0
              ? 'All documents processed'
              : `${batchAttentionCount} ${batchAttentionCount === 1 ? 'item needs' : 'items need'} attention`}
          </p>
        ) : null}
        {uploads.scanErrors.length > 0 ? (
          <div role="alert" className="text-danger-text mb-2 text-xs">
            <p>Folder scan was incomplete. Check these entries and choose them again:</p>
            <ul>
              {uploads.scanErrors.map((error, index) => (
                <li key={`${index}-${error}`}>{error}</li>
              ))}
            </ul>
            <Button variant="ghost" size="sm" onClick={uploads.clearScanErrors}>
              Dismiss
            </Button>
          </div>
        ) : null}
        {failedUploads.length > 0 ? (
          <div role="alert" className="mb-2 space-y-1 text-xs">
            {failedUploads.map((attempt) => (
              <div key={attempt.id} className="flex items-center gap-2">
                <span className="min-w-0 flex-1 break-words">
                  {attempt.label}:{' '}
                  {attempt.state === 'uncertain'
                    ? 'Upload may have completed; retry will check it.'
                    : attempt.error}
                </span>
                <Button variant="outline" size="sm" onClick={() => uploads.retry(attempt.id)}>
                  Retry
                </Button>
                <Button variant="ghost" size="sm" onClick={() => uploads.dismiss(attempt.id)}>
                  Dismiss
                </Button>
              </div>
            ))}
          </div>
        ) : null}
        <DocumentDropzone
          rejectedFiles={rejectedFiles}
          uploadingName={uploading}
          scanning={scanning}
          uploadedCount={uploadedCount}
          queueLength={pendingUploads.length}
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
    listed.pages_failed !== status.pages_failed ||
    listed.coverage_complete !== status.coverage_complete ||
    listed.refresh_state !== status.refresh_state ||
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
