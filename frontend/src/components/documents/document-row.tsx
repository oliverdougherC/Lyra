'use client'

import { useEffect, useRef, useState } from 'react'
import Link from 'next/link'
import {
  AlertCircle,
  Check,
  ChevronRight,
  FileText,
  FileType,
  FileWarning,
  FolderInput,
  ListChecks,
  MessageSquare,
  MoreVertical,
  RotateCw,
  ScanText,
  Trash2,
} from 'lucide-react'
import { toast } from 'sonner'

import { StatusWord } from '@/components/ex-libris'
import { IngestionProgress } from '@/components/documents/ingestion-progress'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { formatCount, formatFileSize, truncateMiddle } from '@/lib/format'
import { chatHandoffUrl } from '@/lib/handoff'
import { isTerminal, useDocumentOutline, useDocumentStatus } from '@/lib/hooks/use-documents'
import { useSettings } from '@/lib/hooks/use-settings'
import { cn } from '@/lib/utils'
import type { DocumentRead, DocumentState, DocumentStatus } from '@/types'

/** How the backend joins a section path. Titles, not numbers. */
const PATH_SEPARATOR = ' / '

const SCANNED_TITLE = 'Needs text recognition'

/**
 * The copy this replaced promised that scans would be readable "in a future update" and
 * that the document would "process automatically then". Both halves are now wrong, and the
 * second is the one that cost a student something: recognition is opt-in, so a document
 * left waiting for it to happen by itself waits forever.
 */
const SCANNED_BODY =
  'This looks like a scanned document, so there was no text to read when it was uploaded. Lyra can read it now.'

/**
 * The same shape as the `unchecked` verdict's hover card, for the same reason: a feature
 * that is unavailable says so plainly and points at the thing that would make it available.
 * It never renders as a failure of the document.
 */
const NO_VISION_BODY =
  'This looks like a scanned document. Reading it needs a model that can see images, and the one in Settings cannot.'

function skippedBody(pages: number): string {
  return pages === 1
    ? 'One page of this document had no text to find. Lyra can read it as an image.'
    : `${pages} pages of this document had no text to find. Lyra can read them as images.`
}

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
  onRecognize: (documentId: number) => void
  onDelete: (document: DocumentRead) => void
  onStatus: (documentId: number, status: DocumentStatus) => void
  mode?: DocumentRowMode
  /** Supplied where refiling is on offer, which is the class hub rather than the rail. */
  onMove?: (document: DocumentRead) => void
  /** Make a practice quiz from this one document. Supplied where the pane can navigate. */
  onPractice?: (document: DocumentRead) => void
}

export function DocumentRow({
  document,
  selected,
  onSelect,
  onRetry,
  onRecognize,
  onDelete,
  onStatus,
  mode = 'ask',
  onMove,
  onPractice,
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
  const pagesDone = live?.pages_done ?? document.pages_done
  const pagesSkipped = live?.pages_skipped ?? document.pages_skipped
  const pagesFailed = live?.pages_failed ?? document.pages_failed
  const stageDetail = live?.stage_detail ?? document.stage_detail
  const requested = live?.recognize ?? document.recognize
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
            {/* The file's own next actions, ahead of its management: while a student is
                looking at course material, "do something with this" is the likelier reason
                to open the menu than "refile this" (contextual handoffs, not more tabs). */}
            {state === 'ready' ? (
              <>
                <DropdownMenuItem asChild>
                  <Link href={chatHandoffUrl(document.class_id, { documentId: document.id })}>
                    <MessageSquare />
                    Ask about this
                  </Link>
                </DropdownMenuItem>
                {onPractice ? (
                  <DropdownMenuItem onSelect={() => onPractice(document)}>
                    <ListChecks />
                    Make practice questions
                  </DropdownMenuItem>
                ) : null}
              </>
            ) : null}
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
          <IngestionProgress
            state={state}
            pagesTotal={pagesTotal}
            pagesDone={pagesDone}
            stageDetail={stageDetail}
          />
        </div>
      ) : null}

      {state === 'unsupported' ? (
        <div className="mt-2 pl-6">
          <ScannedPopover
            trigger="No readable text. What does that mean?"
            // Once it has been asked for and the document is still here, the backend has
            // put the reason it could not run in `error_message`, and that reason is the
            // one thing the student can act on.
            body={requested && errorMessage ? errorMessage : SCANNED_BODY}
            actionLabel={requested ? 'Try again' : 'Read this document'}
            onAction={() => onRecognize(document.id)}
          />
        </div>
      ) : null}

      {/* Two lines that can both be true of one document, and are different facts. This
          one is pages that had no text to find; the notice below is pages recognition
          tried and could not transcribe. */}
      {state === 'ready' && pagesSkipped > 0 ? (
        <div className="mt-2 pl-6">
          <ScannedPopover
            trigger={`${pagesSkipped} ${pagesSkipped === 1 ? 'page' : 'pages'} skipped, no readable text`}
            // Same rule as the unsupported branch above: once reading was asked for, the
            // backend puts the reason it could not run in `error_message`, and a mixed
            // document that landed ready on its text pages alone must say why the rest
            // were not attempted rather than pretending nobody asked.
            body={requested && errorMessage ? errorMessage : skippedBody(pagesSkipped)}
            actionLabel={
              requested && errorMessage
                ? 'Try again'
                : pagesSkipped === 1
                  ? 'Read that page'
                  : 'Read those pages'
            }
            onAction={() => onRecognize(document.id)}
          />
        </div>
      ) : null}

      {state === 'ready' && pagesFailed > 0 ? (
        <div className="mt-2 pl-6">
          <PageFailureNotice count={pagesFailed} onRetry={() => onRecognize(document.id)} />
        </div>
      ) : null}

      {state === 'ready' ? (
        <div className="mt-2 pl-6">
          <DocumentOutline documentId={document.id} />
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
  // Status is a word, never a bare icon (design system section 10). Ready is the nominal
  // state, so it prints quietly rather than in a color; only the exceptions take one.
  if (state === 'ready') {
    return (
      <StatusWord tone="nominal" icon={<Check />}>
        Ready
      </StatusWord>
    )
  }
  if (state === 'unsupported') {
    return (
      <StatusWord tone="info" icon={<FileWarning />}>
        no text
      </StatusWord>
    )
  }
  if (state === 'failed') {
    return (
      <StatusWord tone="warn" icon={<AlertCircle />}>
        failed
      </StatusWord>
    )
  }
  if (state === 'pending') {
    return (
      <StatusWord
        tone="nominal"
        icon={<span className="bg-text-tertiary block size-2 rounded-full" />}
      >
        queued
      </StatusWord>
    )
  }
  return null
}

/**
 * Why a document has no text, and the offer to read it anyway.
 *
 * Nothing is transcribed on the student's behalf. Recognition is minutes of model time per
 * document and, against a configured remote endpoint, it sends page images of their own
 * material somewhere, so it happens when it is asked for and not before. That is why this
 * is an action in a popover rather than something the document did while nobody was
 * looking.
 */
function ScannedPopover({
  trigger,
  body,
  actionLabel,
  onAction,
}: {
  trigger: string
  body: string
  actionLabel: string
  onAction: () => void
}) {
  const { data: settings } = useSettings()
  // Only an explicit no withholds the action. Null means nobody has asked this endpoint
  // yet, and refusing to offer on an unknown would hide the feature from every student who
  // has not visited Settings.
  const blind = settings?.vision_supported === false

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
        <p className="text-text-secondary mt-1">{blind ? NO_VISION_BODY : body}</p>
        {blind ? (
          <p className="mt-3">
            <Link href="/settings" className="text-accent-primary underline underline-offset-2">
              Check your endpoint settings
            </Link>
          </p>
        ) : (
          <Button className="mt-3 w-full" onClick={onAction}>
            <ScanText />
            {actionLabel}
          </Button>
        )}
      </PopoverContent>
    </Popover>
  )
}

/**
 * Pages recognition tried and could not read, as a quiet caption rather than an alarm.
 *
 * The document is `ready`, not `failed`, and that is the whole point. Thirty-nine good
 * pages and one bad one is a document that works, and styling it as a failure would tell
 * the student to throw away something that is mostly fine.
 */
function PageFailureNotice({ count, onRetry }: { count: number; onRetry: () => void }) {
  return (
    <p className="text-text-tertiary flex flex-wrap items-center gap-2 text-xs">
      <span>{count === 1 ? '1 page' : `${count} pages`} could not be read</span>
      <button
        type="button"
        onClick={onRetry}
        className="text-accent-primary rounded-sm underline underline-offset-2 focus-visible:ring-ring/50 focus-visible:ring-[3px] focus-visible:outline-none"
      >
        {count === 1 ? 'Try that page' : 'Try those pages'}
      </button>
    </p>
  )
}

/**
 * The section hierarchy Lyra indexed this document under, closed by default.
 *
 * It exists because of pillar 3. `section_path` decides which chunks answer a question, and
 * a student whose 600-page book was read as one flat blob otherwise has no way to find that
 * out except by noticing that the answers got worse.
 */
function DocumentOutline({ documentId }: { documentId: number }) {
  const [open, setOpen] = useState(false)
  // Only once it is opened. A closed disclosure is the default on every row of the list,
  // and this is a group-by over every chunk of what may be a book.
  const { data, isPending } = useDocumentOutline(documentId, open)

  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger className="text-text-tertiary hover:text-text-secondary focus-visible:ring-ring/50 flex items-center gap-1 rounded-sm text-xs transition-colors focus-visible:ring-[3px] focus-visible:outline-none">
        <ChevronRight
          className={cn('size-3 transition-transform', open && 'rotate-90')}
          aria-hidden
        />
        Outline
      </CollapsibleTrigger>
      <CollapsibleContent className="pt-1.5">
        {isPending ? (
          <p className="text-text-tertiary text-xs">Reading the index...</p>
        ) : !data || data.sections.length === 0 ? (
          // Said plainly rather than left blank. "No structure" and "this did not load" look
          // identical as an empty list, and only one of them is worth acting on.
          <p className="text-text-tertiary text-xs">
            No sections found. This document is indexed as{' '}
            {formatCount(data?.chunk_count ?? 0, 'passage')} with no hierarchy, so questions about
            it are answered by meaning alone.
          </p>
        ) : (
          <>
            <ul className="max-h-56 space-y-0.5 overflow-y-auto">
              {data.sections.map((section) => (
                <li
                  key={section.path}
                  // Indented by depth, so the shape of the book is visible at a glance
                  // rather than having to be read out of the paths.
                  style={{ paddingLeft: `${(section.depth - 1) * 0.75}rem` }}
                  className="flex items-baseline gap-1.5 text-xs"
                >
                  {section.number ? (
                    <span className="text-text-tertiary shrink-0 tabular-nums">
                      {section.number}
                    </span>
                  ) : null}
                  <span
                    className={cn(
                      'min-w-0 truncate',
                      section.depth === 1
                        ? 'text-text-secondary font-medium'
                        : 'text-text-tertiary',
                    )}
                    title={section.path}
                  >
                    {section.path.split(PATH_SEPARATOR).at(-1)}
                  </span>
                  {section.first_page !== null ? (
                    <span className="text-text-tertiary ml-auto shrink-0 tabular-nums">
                      p{section.first_page}
                    </span>
                  ) : null}
                </li>
              ))}
            </ul>
            {data.sectioned_count < data.chunk_count ? (
              <p className="text-text-tertiary mt-1.5 text-[11px]">
                {formatCount(data.chunk_count - data.sectioned_count, 'passage')} outside any
                section.
              </p>
            ) : null}
          </>
        )}
      </CollapsibleContent>
    </Collapsible>
  )
}
