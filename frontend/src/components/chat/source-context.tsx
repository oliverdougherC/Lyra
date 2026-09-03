'use client'

import { useMemo, useState } from 'react'
import { FileSearch, X } from 'lucide-react'

import { Input } from '@/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'
import type { DocumentRead, DocumentState } from '@/types'

const ALL = 'all'

/** What a document that cannot be read yet cannot be used for, in one quiet phrase. */
const STATE_NOTES: Partial<Record<DocumentState, string>> = {
  pending: 'Waiting to be read',
  parsing: 'Being read',
  chunking: 'Being read',
  embedding: 'Being indexed',
  extracting: 'Being indexed',
  failed: 'Could not be read',
  unsupported: 'Format Lyra cannot read',
}

type SourceContextProps = {
  documents: DocumentRead[] | undefined
  /** Set while the list itself failed to load. */
  documentsError?: Error | null
  onRetryDocuments?: () => void
  selectedId: number | null
  /** `null` reads everything ready; an id reads only that document. */
  onSelect: (documentId: number | null) => void
}

/**
 * Choosing what Lyra reads for the next answer, kept at the composer rather than in a
 * column of the window.
 *
 * The two jobs that used to share a pane are separate: this control answers "which
 * material is this question about" - a name, a search, a selection - and says nothing
 * about managing files. Uploads, re-indexing, moving, and deleting live in the class's
 * Files tab. The default is everything ready, which is what most questions want; scoping
 * to one document is a deliberate, visible choice that carries a small chip in the
 * composer until the student clears it.
 */
export function SourceContext({
  documents,
  documentsError = null,
  onRetryDocuments,
  selectedId,
  onSelect,
}: SourceContextProps) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')

  const selected = documents?.find((document) => document.id === selectedId) ?? null

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase()
    const list = (documents ?? [])
      .filter((document) => (needle ? document.filename.toLowerCase().includes(needle) : true))
      .sort((a, b) => a.filename.localeCompare(b.filename))
    // Ready material is what the question can be about; the rest stay visible so a file
    // that just landed does not look lost, but they are not selectable until they can be.
    const ready = list.filter((document) => document.state === 'ready')
    const rest = list.filter((document) => document.state !== 'ready')
    return [...ready, ...rest]
  }, [documents, query])

  const readyCount = documents?.filter((document) => document.state === 'ready').length ?? 0

  const value = selectedId === null ? ALL : String(selectedId)

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next)
        if (!next) setQuery('')
      }}
    >
      <div className="flex items-center gap-1">
        <PopoverTrigger asChild>
          <button
            type="button"
            aria-label={
              selected
                ? `Lyra reads only ${selected.filename}. Choose what Lyra reads for this answer.`
                : 'Lyra reads all of this class\u2019s material. Choose what Lyra reads for this answer.'
            }
            className={cn(
              'flex h-6 max-w-[9rem] items-center gap-1.5 rounded-full px-2 text-xs',
              'text-text-secondary transition-colors hover:bg-muted hover:text-text-primary',
              'focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none',
              'sm:max-w-[12rem]',
            )}
          >
            <FileSearch aria-hidden className="size-3.5 shrink-0" />
            <span className="truncate">{selected ? selected.filename : 'All material'}</span>
          </button>
        </PopoverTrigger>
        {selected ? (
          <button
            type="button"
            aria-label={`Stop reading only ${selected.filename}; read all of this class's material`}
            onClick={() => onSelect(null)}
            className="text-text-tertiary hover:text-text-primary flex size-5 items-center justify-center rounded-full transition-colors focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
          >
            <X aria-hidden className="size-3" />
          </button>
        ) : null}
      </div>

      <PopoverContent align="start" sideOffset={8} className="w-72 max-w-[calc(100vw-1.5rem)] p-0">
        <p className="text-foreground border-b px-3 py-2 text-sm font-medium">
          What Lyra reads
        </p>

        {documentsError ? (
          <div className="flex flex-col gap-2 px-3 py-3 text-sm">
            <p className="text-danger-text">
              {documentsError.message || 'The file list could not be loaded.'}
            </p>
            {onRetryDocuments ? (
              <button
                type="button"
                onClick={() => {
                  onRetryDocuments()
                  setQuery('')
                }}
                className="text-left text-sm font-medium underline underline-offset-2"
              >
                Try again
              </button>
            ) : null}
          </div>
        ) : documents === undefined ? (
          <div className="flex flex-col gap-2 px-3 py-3" aria-busy="true">
            <Skeleton className="h-4 w-1/2" />
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-4 w-1/2" />
          </div>
        ) : (
          <>
            <div className="p-2 pb-0">
              <Input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search files"
                aria-label="Search this class's files"
                autoComplete="off"
                className="h-8 text-sm"
                autoFocus
              />
            </div>
            <ScrollArea className="max-h-72">
              {visible.length === 0 ? (
                <p className="text-text-tertiary px-3 py-3 text-sm">
                  {documents.length === 0
                    ? 'No files in this class yet. Add some in the Files tab.'
                    : `No files match "${query.trim()}".`}
                </p>
              ) : (
                <RadioGroup
                  value={value}
                  onValueChange={(next) => {
                    const id = next === ALL ? null : Number(next)
                    onSelect(id)
                    setOpen(false)
                  }}
                  aria-label="What Lyra reads for this answer"
                  className="p-2"
                >
                  <SourceRow
                    id={`${ALL}-${ALL}`}
                    label="All material"
                    checked={selectedId === null}
                    note={readyCount > 0 ? `${readyCount} ready` : null}
                    disabled={readyCount === 0}
                  />
                  {visible.map((document) => (
                    <SourceRow
                      key={document.id}
                      id={`${ALL}-${document.id}`}
                      label={document.filename}
                      checked={document.id === selectedId}
                      note={STATE_NOTES[document.state] ?? null}
                      disabled={document.state !== 'ready'}
                    />
                  ))}
                </RadioGroup>
              )}
            </ScrollArea>
            {readyCount === 0 && documents.length > 0 && !documentsError ? (
              <p className="text-text-tertiary border-t px-3 py-2 text-xs">
                Nothing is ready to read yet. Files Lyra has finished reading can be chosen
                here.
              </p>
            ) : null}
          </>
        )}
      </PopoverContent>
    </Popover>
  )
}

type SourceRowProps = {
  id: string
  label: string
  checked: boolean
  note: string | null
  disabled?: boolean
}

function SourceRow({ id, label, checked, note, disabled = false }: SourceRowProps) {
  return (
    <label
      htmlFor={id}
      className={cn(
        'flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm transition-colors',
        'hover:bg-muted',
        'focus-within:ring-2 focus-within:ring-ring/50',
        disabled && 'cursor-not-allowed text-text-tertiary hover:bg-transparent',
      )}
    >
      <RadioGroupItem id={id} value={id.split('-')[1]} disabled={disabled} />
      <span className={cn('min-w-0 flex-1 truncate', checked && 'font-medium')}>{label}</span>
      {note ? <span className="text-text-tertiary shrink-0 text-xs">{note}</span> : null}
    </label>
  )
}
