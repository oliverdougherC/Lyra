'use client'

import { useState } from 'react'
import { FolderOpen, Upload } from 'lucide-react'

import { Progress } from '@/components/ui/progress'
import { cn } from '@/lib/utils'

export const ACCEPTED_EXTENSIONS = ['.pdf', '.txt', '.md'] as const

export function hasAcceptedExtension(name: string): boolean {
  return ACCEPTED_EXTENSIONS.some((extension) => name.toLowerCase().endsWith(extension))
}

/** Bookkeeping files Windows leaves in folders. The macOS ones are all dotfiles. */
const NOISE_FILENAMES = new Set(['thumbs.db', 'desktop.ini'])

/**
 * Whether a name is something the operating system left in the folder rather than
 * something the student chose to upload.
 *
 * Every folder on a Mac carries a `.DS_Store`, so every folder upload was answered with
 * "`.DS_Store` is not a supported type. Lyra reads PDF, TXT, and MD." - a correction for a
 * file the student never picked, sitting in danger colours over an upload that had in fact
 * worked. Noise is dropped without a word; the rejection message is for files they meant.
 */
export function isSystemNoise(name: string): boolean {
  // Every dotfile, which also catches AppleDouble sidecars (`._lecture.pdf`). Those end in
  // an accepted extension, so without this they were not merely reported, they were
  // uploaded: a few kilobytes of resource fork ingested as if it were the lecture.
  return name.startsWith('.') || NOISE_FILENAMES.has(name.toLowerCase())
}

/**
 * Splits a drop into files Lyra can read and names it cannot, so the rejection message can
 * say what was refused instead of silently dropping the whole batch. System noise is in
 * neither list: it is not uploaded and not mentioned.
 */
export function partitionFiles(files: File[]): { accepted: File[]; rejected: string[] } {
  const accepted: File[] = []
  const rejected: string[] = []
  for (const file of files) {
    if (isSystemNoise(file.name)) continue
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
 *
 * Every entry is claimed up front, before anything is awaited. A `DataTransferItemList` is
 * emptied the moment the drop handler yields to the event loop, so reading the list inside
 * the walk meant the second folder of a multi-folder drop came back null and its files were
 * dropped without a word: three week folders dragged in together uploaded only the first.
 *
 * Args:
 *   onFolderScan: Called synchronously, before the walk, when the drop contains a folder.
 *     Reading a term of notes off disk takes seconds, and the caller needs to be able to
 *     say so while it happens rather than after.
 */
export function filesFromDrop(
  dataTransfer: DataTransfer | null,
  onFolderScan?: () => void,
): Promise<{ files: File[]; folders: boolean }> {
  const items = dataTransfer?.items
  if (items && items.length > 0) {
    const entries = Array.from(items)
      .filter((item) => typeof item.webkitGetAsEntry === 'function')
      .map((item) => item.webkitGetAsEntry())
      .filter((entry): entry is FileSystemEntry => entry !== null)

    if (entries.length > 0) {
      const folders = entries.some((entry) => entry.isDirectory)
      if (folders) onFolderScan?.()
      return (async () => {
        const files: File[] = []
        for (const entry of entries) await collectEntry(entry, files)
        return { files, folders }
      })()
    }
  }
  return Promise.resolve({ files: Array.from(dataTransfer?.files ?? []), folders: false })
}

type DocumentDropzoneProps = {
  rejectedFiles?: string[] | null
  /** Name of the file currently uploading, when one is in flight. */
  uploadingName?: string | null
  /** True while a dropped folder is being walked, which happens before any upload starts. */
  scanning?: boolean
  uploadedCount?: number
  queueLength?: number
  className?: string
  /** Hidden pickers owned by the parent, so a collapsed pane can still open them. */
  fileInputRef?: React.RefObject<HTMLInputElement | null>
  folderInputRef?: React.RefObject<HTMLInputElement | null>
}

export function DocumentDropzone({
  rejectedFiles,
  uploadingName,
  scanning = false,
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
  const expanded = dragOver || Boolean(rejectedFiles) || uploading || scanning

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
        // The drop itself is handled by the pane, which wraps this well and the list above
        // it: one drop path rather than two, so a folder dropped on the rows and a folder
        // dropped on the well are read the same way and reported the same way.
        onDrop={() => setDragOver(false)}
        className={cn(
          'flex flex-col items-center justify-center gap-1 rounded-md border border-dashed px-3 py-3 text-center text-sm transition-colors duration-150',
          'focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2',
          'min-h-[var(--pane-control-row)]',
          dragOver
            ? 'border-accent-primary bg-accent-surface'
            : rejectedFiles
              ? 'border-danger-text bg-danger-fill'
              : 'border-border-strong bg-card hover:bg-muted',
        )}
      >
        {/* The whole well is the control. It used to be a label on the left with two
            buttons pinned to the right, which read as a toolbar with a caption rather than
            as somewhere to drop a file, and left the one thing it is for - the target -
            as the part you could not click. */}
        <button
          type="button"
          onClick={() => fileInputRef?.current?.click()}
          className="focus-visible:ring-ring flex w-full flex-col items-center justify-center gap-1.5 rounded-sm focus-visible:ring-2 focus-visible:outline-none"
        >
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
          {/* Says folders, because otherwise nothing does. The recursive walk has been here
              all along, and a well that offers "PDF, TXT, or MD" reads as a file-at-a-time
              control: the answer to "can I just give it my notes folder?" is yes. */}
          <span className={cn('text-xs', dragOver ? 'font-medium' : 'text-text-secondary')}>
            {dragOver ? 'Drop to upload' : 'Drop files or a folder here, or click to browse'}
          </span>
        </button>

        {/* Not a second button beside the first: a quiet line under the target, because
            picking a folder is the rarer half of the choice and the only other way to do it
            is to drag one in. */}
        {expanded ? null : (
          <button
            type="button"
            onClick={() => folderInputRef?.current?.click()}
            title="Every PDF, TXT, and MD inside it, at any depth"
            className="text-text-tertiary hover:text-text-secondary focus-visible:ring-ring rounded-sm text-[11px] underline underline-offset-2 transition-colors focus-visible:ring-2 focus-visible:outline-none"
          >
            <FolderOpen aria-hidden className="mr-1 inline size-3 align-[-1px]" />
            choose a folder
          </button>
        )}

        {scanning ? (
          <p className="text-text-secondary text-xs">
            Reading the folder, including everything inside it...
          </p>
        ) : uploading ? (
          <div className="w-full space-y-1.5">
            <p className="text-text-secondary truncate text-xs">Uploading {uploadingName}</p>
            <Progress value={percent} />
          </div>
        ) : rejectedFiles ? (
          <p className="text-danger-text text-xs">
            {rejectedFiles.length === 1
              ? `${rejectedFiles[0]} is not`
              : `${rejectedFiles.length} files are not`}{' '}
            a supported type. Lyra reads PDF, TXT, and MD.
          </p>
        ) : null}
      </div>
    </div>
  )
}
