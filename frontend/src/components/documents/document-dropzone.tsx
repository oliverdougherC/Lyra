'use client'

import { useState } from 'react'
import { FolderOpen, Upload } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Progress } from '@/components/ui/progress'
import { cn } from '@/lib/utils'

export const ACCEPTED_EXTENSIONS = ['.pdf', '.txt', '.md'] as const

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
  rejectedFiles?: string[] | null
  /** Name of the file currently uploading, when one is in flight. */
  uploadingName?: string | null
  uploadedCount?: number
  queueLength?: number
  className?: string
  /** Hidden pickers owned by the parent, so a collapsed pane can still open them. */
  fileInputRef?: React.RefObject<HTMLInputElement | null>
  folderInputRef?: React.RefObject<HTMLInputElement | null>
}

export function DocumentDropzone({
  onFiles,
  rejectedFiles,
  uploadingName,
  uploadedCount = 0,
  queueLength = 0,
  className,
  fileInputRef,
  folderInputRef,
}: DocumentDropzoneProps) {
  const [dragOver, setDragOver] = useState(false)

  const uploading = Boolean(uploadingName)
  const total = uploadedCount + queueLength
  const percent = total > 0 ? Math.round((uploadedCount / total) * 100) : 0

  // Idle, this is one quiet row so the document list keeps the pane's height. It only
  // grows when it has something to say: a drag in progress, a rejection, or an upload.
  const expanded = dragOver || Boolean(rejectedFiles) || uploading

  return (
    <div className={className}>
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
          event.stopPropagation()
          setDragOver(false)
          void filesFromDrop(event.dataTransfer).then(({ files }) => onFiles(files))
        }}
        className={cn(
          'flex items-center gap-2 rounded-md border border-dashed px-2.5 py-2 text-sm transition-colors duration-150',
          'focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2',
          expanded && 'flex-col items-stretch gap-2.5 p-3',
          dragOver
            ? 'border-accent-primary bg-accent-surface'
            : rejectedFiles
              ? 'border-danger-text bg-danger-fill'
              : 'border-border-strong bg-card hover:bg-muted',
        )}
      >
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <Upload
            className={cn(
              'size-4 shrink-0',
              dragOver
                ? 'text-accent-primary'
                : rejectedFiles
                  ? 'text-danger-text'
                  : 'text-text-tertiary',
            )}
            aria-hidden
          />
          <div className="min-w-0 flex-1">
            <p className={cn('truncate text-xs', dragOver ? 'font-medium' : 'text-text-secondary')}>
              {dragOver ? 'Drop to upload' : 'Drop PDF, TXT, or MD here'}
            </p>
            {uploading ? (
              <div className="mt-1.5 space-y-1.5">
                <p className="text-text-secondary truncate text-xs">Uploading {uploadingName}</p>
                <Progress value={percent} />
              </div>
            ) : rejectedFiles ? (
              <p className="text-danger-text mt-1 text-xs">
                {rejectedFiles.length === 1
                  ? `${rejectedFiles[0]} is not`
                  : `${rejectedFiles.length} files are not`}{' '}
                a supported type. Lyra reads PDF, TXT, and MD.
              </p>
            ) : null}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <Button size="sm" variant="outline" onClick={() => fileInputRef?.current?.click()}>
            Choose files
          </Button>
          <Button
            size="icon"
            variant="ghost"
            className="size-8"
            onClick={() => folderInputRef?.current?.click()}
            title="Choose a folder, scanned recursively"
            aria-label="Choose a folder, scanned recursively"
          >
            <FolderOpen />
          </Button>
        </div>
      </div>
    </div>
  )
}
