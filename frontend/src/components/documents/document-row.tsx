'use client'

import { useEffect, useRef } from 'react'
import {
  AlertCircle,
  Check,
  FileText,
  FileType,
  FileWarning,
  FolderInput,
  MoreVertical,
  RotateCw,
  Trash2,
} from 'lucide-react'
import { toast } from 'sonner'

import { IngestionProgress } from '@/components/documents/ingestion-progress'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { formatFileSize, truncateMiddle } from '@/lib/format'
import { isTerminal, useDocumentStatus } from '@/lib/hooks/use-documents'
import { cn } from '@/lib/utils'
import type { DocumentRead, DocumentState, DocumentStatus } from '@/types'

const SCANNED_TITLE = 'Needs text recognition'
const SCANNED_BODY =
  'This looks like a scanned document, so there is no text to read yet. Your file is saved. Lyra will be able to read scans in a future update, and this document will process automatically then.'

function FileIcon({ filename }: { filename: string }) {
  const Icon = filename.toLowerCase().endsWith('.pdf') ? FileType : FileText
  return <Icon className="text-text-tertiary size-4 shrink-0" aria-hidden />
}

/**
 * What clicking the row does, which is the only thing that differs between the two places
 * this row is listed.
 *
 * `ask` is the workspace beside the conversation: picking a document narrows the next
 * question to it. `manage` is the class hub's file list: picking a document marks it for
 * an action, and every document can be picked, including the ones that failed to index -
 * a file that could not be read is exactly the one a student wants to move or throw away.
 */
type DocumentRowMode = 'ask' | 'manage'

type DocumentRowProps = {
  document: DocumentRead
  selected: boolean
  onSelect: (document: DocumentRead) => void
  onRetry: (documentId: number) => void
  onDelete: (document: DocumentRead) => void
  onStatus: (documentId: number, status: DocumentStatus) => void
  mode?: DocumentRowMode
  /** Supplied where refiling is on offer, which is the class hub rather than the rail. */
  onMove?: (document: DocumentRead) => void
}

export function DocumentRow({
  document,
  selected,
  onSelect,
  onRetry,
  onDelete,
  onStatus,
  mode = 'ask',
  onMove,
}: DocumentRowProps) {
  const polling = !isTerminal(document.state)
  const { data: status } = useDocumentStatus(document.id, polling)

  // The row's own poll is finer grained than the list, but only while it is running. It is
  // switched off the moment the list reports the document has settled, and a disabled query
  // keeps its last answer: preferring that answer meant a row went on saying "Analyzing"
  // about a document the server had already finished, for as long as the page stayed open.
  // Reloading appeared to fix it because it threw that frozen answer away.
  const live = polling ? status : undefined

  const state: DocumentState = live?.state ?? document.state
  const pagesTotal = live?.pages_total ?? document.pages_total
  const pagesSkipped = live?.pages_skipped ?? document.pages_skipped
  const errorMessage = live?.error_message ?? document.error_message

  // Announce the transition once, not on every poll that still reports `ready`.
  const announced = useRef(isTerminal(document.state))
  useEffect(() => {
    if (announced.current || !isTerminal(state)) return
    announced.current = true
    if (state === 'ready') toast.success(`${document.filename} is ready.`)
  }, [state, document.filename])

  // Every poll, not only the last one. The row reads its own stage straight off this query,
  // but everything else on screen - the batch readout's stage verb, the class hub's counts,
  // the picker on the solver's setup screen - reads the list, and reporting only the
  // terminal state left all of them showing the stage the document was on when it was
  // uploaded until something else happened to refresh it.
  useEffect(() => {
    if (status) onStatus(document.id, status)
  }, [document.id, onStatus, status])

  const busy = !isTerminal(state)
  const managing = mode === 'manage'
  const selectable = managing || state === 'ready'
  const selectLabel = managing
    ? `${selected ? 'Deselect' : 'Select'} ${document.filename}`
    : selectable
      ? `${selected ? 'Ask about every document instead of' : 'Ask only about'} ${document.filename}`
      : document.filename

  return (
    <div
      className={cn(
        'rounded-md border bg-card px-2.5 py-1.5 transition-colors duration-150',
        busy && 'opacity-70',
        selected ? 'border-accent-primary bg-accent-surface/50' : 'border-border hover:bg-muted',
      )}
    >
      <div className="flex items-start gap-2">
        <button
          type="button"
          disabled={!selectable}
          onClick={() => onSelect(document)}
          aria-pressed={selected}
          aria-label={selectLabel}
          className="flex min-h-9 min-w-0 flex-1 items-center gap-2 rounded-sm text-left focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none disabled:cursor-default"
        >
          {managing ? (
            // A box rather than a highlight: the hub's list is where several files are
            // picked at once, and a selection that only shows as a tint gives no count and
            // no obvious way to let one go.
            <span
              aria-hidden
              className={cn(
                'flex size-4 shrink-0 items-center justify-center rounded-[4px] border transition-colors duration-150',
                selected
                  ? 'border-accent-primary bg-accent-primary text-accent-primary-foreground'
                  : 'border-border-strong',
              )}
            >
              {selected ? <Check className="size-3" /> : null}
            </span>
          ) : null}
          <FileIcon filename={document.filename} />
          <span className="min-w-0 flex-1 truncate text-sm" title={document.filename}>
            {truncateMiddle(document.filename)}
          </span>
          {!busy && state !== 'failed' ? (
            <span className="text-text-tertiary shrink-0 text-xs tabular-nums">
              {formatFileSize(document.byte_size)}
            </span>
          ) : null}
          <StateIndicator state={state} />
        </button>

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="size-8 shrink-0"
              aria-label={`Actions for ${document.filename}`}
            >
              <MoreVertical />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {state === 'failed' || state === 'ready' ? (
              <DropdownMenuItem onSelect={() => onRetry(document.id)}>
                <RotateCw />
                {state === 'failed' ? 'Retry' : 'Reindex'}
              </DropdownMenuItem>
            ) : null}
            {onMove ? (
              <DropdownMenuItem onSelect={() => onMove(document)}>
                <FolderInput />
                Move to another class
              </DropdownMenuItem>
            ) : null}
            <DropdownMenuItem variant="destructive" onSelect={() => onDelete(document)}>
              <Trash2 />
              Delete
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      {busy ? (
        <div className="mt-2 pl-6">
          <IngestionProgress state={state} pagesTotal={pagesTotal} />
        </div>
      ) : null}

      {state === 'unsupported' ? (
        <div className="mt-2 pl-6">
          <ScannedPopover trigger="No readable text. What does that mean?" />
        </div>
      ) : null}

      {state === 'ready' && pagesSkipped > 0 ? (
        <div className="mt-2 pl-6">
          <ScannedPopover
            trigger={`${pagesSkipped} ${pagesSkipped === 1 ? 'page' : 'pages'} skipped, no readable text`}
          />
        </div>
      ) : null}

      {state === 'failed' ? (
        <div className="mt-2 flex items-center gap-2 pl-6">
          <p className="text-danger-text text-xs">
            {errorMessage ?? 'Lyra could not finish reading this document. Retry it.'}
          </p>
          <Button variant="outline" onClick={() => onRetry(document.id)}>
            Retry
          </Button>
        </div>
      ) : null}
    </div>
  )
}

function StateIndicator({ state }: { state: DocumentState }) {
  if (state === 'ready') {
    return (
      <span className="text-success-text flex items-center gap-1 text-xs">
        <Check className="size-4" aria-hidden />
        <span className="sr-only">Ready</span>
      </span>
    )
  }
  if (state === 'unsupported') {
    return (
      <span className="text-info-text flex items-center gap-1 text-xs">
        <FileWarning className="size-4" aria-hidden />
        no text
      </span>
    )
  }
  if (state === 'failed') {
    return (
      <span className="text-danger-text flex items-center gap-1 text-xs">
        <AlertCircle className="size-4" aria-hidden />
        failed
      </span>
    )
  }
  if (state === 'pending') {
    return (
      <span className="text-text-tertiary flex items-center gap-1 text-xs">
        <span className="bg-text-tertiary size-2 rounded-full" aria-hidden />
        queued
      </span>
    )
  }
  return null
}

function ScannedPopover({ trigger }: { trigger: string }) {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="text-info-text rounded-sm text-xs underline underline-offset-2 focus-visible:ring-ring/50 focus-visible:ring-[3px] focus-visible:outline-none"
        >
          {trigger}
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-80 text-sm">
        <p className="font-medium">{SCANNED_TITLE}</p>
        <p className="text-text-secondary mt-1">{SCANNED_BODY}</p>
      </PopoverContent>
    </Popover>
  )
}
