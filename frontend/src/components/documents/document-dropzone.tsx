'use client'

import { useCallback, useRef, useState } from 'react'
import { FolderOpen, Upload } from 'lucide-react'

import { Button } from '@/components/ui/button'
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

/** Reads one file entry, resolving null when the file cannot be read. */
function readFileEntry(entry: FileSystemFileEntry): Promise<File | null> {
  return new Promise((resolve) => {
    entry.file(resolve, () => resolve(null))
  })
}

/** Reads every entry in a directory, looping because the API returns entries in batches. */
function readAllEntries(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
  const readBatch = (): Promise<FileSystemEntry[]> =>
    new Promise((resolve) => {
      reader.readEntries(resolve, () => resolve([]))
    })
  const collect = async (): Promise<FileSystemEntry[]> => {
    const entries: FileSystemEntry[] = []
    for (;;) {
      const batch = await readBatch()
      if (batch.length === 0) return entries
      entries.push(...batch)
    }
  }
  return collect()
}

/** Walks a dropped file system entry recursively and collects every readable file. */
async function collectEntry(entry: FileSystemEntry, files: File[]): Promise<void> {
  if (entry.isFile) {
    const file = await readFileEntry(entry as FileSystemFileEntry)
    if (file) files.push(file)
  } else if (entry.isDirectory) {
    const reader = (entry as FileSystemDirectoryEntry).createReader()
    const children = await readAllEntries(reader)
    for (const child of children) await collectEntry(child, files)
  }
}

/**
 * Turns a drop into flat `File[]`, recursing through dropped folders. Falls back to
 * `dataTransfer.files` where the entry API is unavailable (non-Chromium browsers).
 */
export function filesFromDrop(
  dataTransfer: DataTransfer | null,
): Promise<{ files: File[]; folders: boolean }> {
  const items = dataTransfer?.items
  if (items && items.length > 0) {
    const withEntries = Array.from(items).filter(
      (item) => typeof item.webkitGetAsEntry === 'function',
    )
    if (withEntries.length > 0) {
      return (async () => {
        const files: File[] = []
        let folders = false
        for (const item of withEntries) {
          const entry = item.webkitGetAsEntry()
          if (entry?.isDirectory) folders = true
          if (entry) await collectEntry(entry, files)
        }
        return { files, folders }
      })()
    }
  }
  return Promise.resolve({ files: Array.from(dataTransfer?.files ?? []), folders: false })
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
  const fileInputRef = useRef<HTMLInputElement>(null)
  const folderInputRef = useRef<HTMLInputElement>(null)
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
        ref={fileInputRef}
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
      <input
        ref={(node) => {
          folderInputRef.current = node
          if (node && !node.hasAttribute('webkitdirectory')) {
            node.setAttribute('webkitdirectory', '')
          }
        }}
        type="file"
        multiple
        className="sr-only"
        aria-label="Choose a folder of documents to upload"
        onChange={(event) => {
          submit(Array.from(event.target.files ?? []))
          event.target.value = ''
        }}
      />
      <div
        role="group"
        aria-label="Upload documents"
        onDragOver={(event) => {
          event.preventDefault()
          setDragOver(true)
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(event) => {
          event.preventDefault()
          setDragOver(false)
          void filesFromDrop(event.dataTransfer).then(({ files }) => submit(files))
        }}
        className={cn(
          'flex flex-col gap-2 rounded-md border border-dashed p-3 text-sm transition-colors duration-150',
          'focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2',
          dragOver
            ? 'border-accent-primary bg-accent-surface'
            : rejected
              ? 'border-danger-text bg-danger-fill'
              : 'border-border-strong bg-card hover:bg-muted',
        )}
      >
        <div className="flex items-center gap-3">
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
                {rejected.length === 1
                  ? `${rejected[0]} is not`
                  : `${rejected.length} files are not`}{' '}
                a supported type. Lyra reads PDF, TXT, and MD.
              </p>
            ) : (
              <p className="text-text-secondary mt-1 text-xs">
                Folders are scanned recursively. Choose files or drag them here.
              </p>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button size="sm" onClick={() => folderInputRef.current?.click()}>
            <FolderOpen />
            Choose folder
          </Button>
          <Button size="sm" variant="outline" onClick={() => fileInputRef.current?.click()}>
            <Upload />
            Choose files
          </Button>
        </div>
      </div>
    </div>
  )
}
