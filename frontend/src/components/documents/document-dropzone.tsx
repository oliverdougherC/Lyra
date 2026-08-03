'use client'

import { useCallback, useRef, useState } from 'react'
import { Upload } from 'lucide-react'

import { Progress } from '@/components/ui/progress'
import { cn } from '@/lib/utils'

export const ACCEPTED_EXTENSIONS = ['.pdf', '.txt', '.md'] as const
const ACCEPT_ATTR = ACCEPTED_EXTENSIONS.join(',')

export function hasAcceptedExtension(name: string): boolean {
  return ACCEPTED_EXTENSIONS.some((extension) => name.toLowerCase().endsWith(extension))
}

/**
 * Splits a drop into files Lyra can read and names it cannot, so the rejection message can
 * say what was refused instead of silently dropping the whole batch.
 */
export function partitionFiles(files: File[]): { accepted: File[]; rejected: string[] } {
  const accepted: File[] = []
  const rejected: string[] = []
  for (const file of files) {
    if (hasAcceptedExtension(file.name)) accepted.push(file)
    else rejected.push(file.name)
  }
  return { accepted, rejected }
}

type DocumentDropzoneProps = {
  onFiles: (files: File[]) => void
  /** Name of the file currently uploading, when one is in flight. */
  uploadingName?: string | null
  uploadedCount?: number
  queueLength?: number
  className?: string
}

export function DocumentDropzone({
  onFiles,
  uploadingName,
  uploadedCount = 0,
  queueLength = 0,
  className,
}: DocumentDropzoneProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragOver, setDragOver] = useState(false)
  const [rejected, setRejected] = useState<string[] | null>(null)

  const submit = useCallback(
    (files: File[]) => {
      const { accepted, rejected: refused } = partitionFiles(files)
      setRejected(refused.length > 0 ? refused : null)
      if (accepted.length > 0) onFiles(accepted)
    },
    [onFiles],
  )

  const uploading = Boolean(uploadingName)
  const total = uploadedCount + queueLength
  const percent = total > 0 ? Math.round((uploadedCount / total) * 100) : 0

  return (
    <div className={className}>
      <input
        ref={inputRef}
        type="file"
        multiple
        accept={ACCEPT_ATTR}
        className="sr-only"
        aria-label="Choose documents to upload"
        onChange={(event) => {
          submit(Array.from(event.target.files ?? []))
          event.target.value = ''
        }}
      />
      <button
        type="button"
        onClick={() => inputRef.current?.click()}
        onDragOver={(event) => {
          event.preventDefault()
          setDragOver(true)
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(event) => {
          event.preventDefault()
          setDragOver(false)
          submit(Array.from(event.dataTransfer.files))
        }}
        className={cn(
          'flex w-full items-center gap-3 rounded-md border border-dashed p-3 text-left text-sm transition-colors duration-150',
          'focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none',
          dragOver
            ? 'border-accent-primary bg-accent-surface'
            : rejected
              ? 'border-danger-text bg-danger-fill'
              : 'border-border-strong bg-card hover:bg-muted',
        )}
      >
        <span
          className={cn(
            'flex size-8 shrink-0 items-center justify-center rounded-md bg-muted',
            dragOver
              ? 'bg-accent-surface text-accent-primary'
              : rejected
                ? 'bg-danger-fill text-danger-text'
                : 'text-text-tertiary',
          )}
        >
          <Upload className="size-4" aria-hidden />
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium">Drop PDF, TXT, or MD</p>
          {uploading ? (
            <div className="mt-1.5 space-y-1.5">
              <p className="text-text-secondary truncate text-xs">Uploading {uploadingName}</p>
              <Progress value={percent} />
            </div>
          ) : rejected ? (
            <p className="text-danger-text mt-1 text-xs">
              {rejected.length === 1 ? `${rejected[0]} is not` : `${rejected.length} files are not`}{' '}
              a supported type. Lyra reads PDF, TXT, and MD.
            </p>
          ) : (
            <p className="text-text-secondary mt-1 text-xs">Choose files or drag them here.</p>
          )}
        </div>
      </button>
    </div>
  )
}
