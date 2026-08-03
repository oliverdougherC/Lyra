'use client'

import { useEffect, useRef } from 'react'
import {
  AlertCircle,
  Check,
  FileText,
  FileType,
  FileWarning,
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

type DocumentRowProps = {
  document: DocumentRead
  selected: boolean
  onSelect: (document: DocumentRead) => void
  onRetry: (documentId: number) => void
  onDelete: (document: DocumentRead) => void
  onStatus: (documentId: number, status: DocumentStatus) => void
}

export function DocumentRow({
  document,
  selected,
  onSelect,
  onRetry,
  onDelete,
  onStatus,
}: DocumentRowProps) {
  const polling = !isTerminal(document.state)
  const { data: status } = useDocumentStatus(document.id, polling)

  const state: DocumentState = status?.state ?? document.state
  const pagesDone = status?.pages_done ?? document.pages_done
  const pagesTotal = status?.pages_total ?? document.pages_total
  const pagesSkipped = status?.pages_skipped ?? document.pages_skipped
  const errorMessage = status?.error_message ?? document.error_message

  // Announce the transition once, not on every poll that still reports `ready`.
  const announced = useRef(isTerminal(document.state))
  useEffect(() => {
    if (announced.current || !isTerminal(state)) return
    announced.current = true
    if (state === 'ready') toast.success(`${document.filename} is ready.`)
  }, [state, document.filename])

  useEffect(() => {
    if (status && isTerminal(status.state)) onStatus(document.id, status)
  }, [document.id, onStatus, status])

  const busy = !isTerminal(state)
  const selectable = state === 'ready'

  return (
    <li
      className={cn(
        'rounded-md border bg-card p-3 transition-colors duration-150',
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
          className="flex min-h-10 min-w-0 flex-1 items-center gap-2 rounded-sm text-left focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none disabled:cursor-default"
        >
          <FileIcon filename={document.filename} />
          <span className="min-w-0 flex-1 truncate text-sm" title={document.filename}>
            {truncateMiddle(document.filename)}
          </span>
          <StateIndicator state={state} />
        </button>

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon" className="shrink-0">
              <MoreVertical />
              <span className="sr-only">Actions for {document.filename}</span>
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {state === 'failed' || state === 'ready' ? (
              <DropdownMenuItem onSelect={() => onRetry(document.id)}>
                <RotateCw />
                {state === 'failed' ? 'Retry' : 'Reindex'}
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
          <IngestionProgress state={state} pagesDone={pagesDone} pagesTotal={pagesTotal} />
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

      {!busy && state !== 'failed' ? (
        <p className="text-text-tertiary mt-1 pl-6 text-xs">{formatFileSize(document.byte_size)}</p>
      ) : null}
    </li>
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
