import { describe, expect, it, vi } from 'vitest'

import {
  filesFromDrop,
  hasAcceptedExtension,
  isSystemNoise,
  partitionFiles,
} from '@/components/documents/document-dropzone'

/**
 * A stand-in for the drag-and-drop entry API.
 *
 * The important part is the neutering: a real `DataTransferItemList` is emptied as soon as
 * the drop handler yields to the event loop, so `webkitGetAsEntry` returns null from the
 * first `await` onwards. Anything that reads the list lazily loses whatever it had not got
 * to yet, which is exactly the failure this mock exists to catch.
 */
function dropOf(tree: Record<string, string[] | null>): DataTransfer {
  let neutered = false

  const fileEntry = (name: string) => ({
    isFile: true,
    isDirectory: false,
    name,
    file: (resolve: (file: File) => void) => resolve(new File(['x'], name)),
  })

  const directoryEntry = (name: string, children: string[]) => ({
    isFile: false,
    isDirectory: true,
    name,
    createReader: () => {
      let served = false
      return {
        // Batched, the way the real reader is: one call with the entries, then an empty
        // call to say there are no more.
        readEntries: (resolve: (entries: unknown[]) => void) => {
          resolve(served ? [] : children.map(fileEntry))
          served = true
        },
      }
    },
  })

  const items = Object.entries(tree).map(([name, children]) => ({
    webkitGetAsEntry: () =>
      neutered ? null : children === null ? fileEntry(name) : directoryEntry(name, children),
  }))

  // Whatever has not been read by the time the handler yields is gone.
  queueMicrotask(() => {
    neutered = true
  })

  return { items, files: [] } as unknown as DataTransfer
}

describe('hasAcceptedExtension', () => {
  it.each([
    ['notes.pdf', true],
    ['NOTES.PDF', true],
    ['syllabus.md', true],
    ['scratch.txt', true],
    ['slides.pptx', false],
    ['.DS_Store', false],
  ])('reads %s as supported=%s', (name, expected) => {
    expect(hasAcceptedExtension(name)).toBe(expected)
  })
})

describe('isSystemNoise', () => {
  it.each([
    ['.DS_Store', true],
    // An AppleDouble sidecar. It ends in an accepted extension, so before this it was not
    // merely reported but uploaded and ingested as though it were the lecture.
    ['._lecture.pdf', true],
    ['.localized', true],
    ['Thumbs.db', true],
    ['thumbs.db', true],
    ['desktop.ini', true],
    ['lecture.pdf', false],
    ['notes.docx', false],
  ])('reads %s as noise=%s', (name, expected) => {
    expect(isSystemNoise(name)).toBe(expected)
  })
})

describe('partitionFiles', () => {
  it('names what it refused rather than dropping the batch silently', () => {
    const { accepted, rejected } = partitionFiles([
      new File([''], 'hw1.pdf'),
      new File([''], 'notes.docx'),
    ])

    expect(accepted.map((file) => file.name)).toEqual(['hw1.pdf'])
    expect(rejected).toEqual(['notes.docx'])
  })

  it('ignores what the operating system left in the folder, without a word', () => {
    // The complaint: every folder on a Mac carries a .DS_Store, so every folder upload
    // ended in a red banner correcting the student about a file they never chose.
    const { accepted, rejected } = partitionFiles([
      new File([''], 'hw1.pdf'),
      new File([''], '.DS_Store'),
      new File([''], '._hw1.pdf'),
      new File([''], 'Thumbs.db'),
    ])

    expect(accepted.map((file) => file.name)).toEqual(['hw1.pdf'])
    expect(rejected).toEqual([])
  })

  it('still names a real file of the wrong type dropped alongside noise', () => {
    const { accepted, rejected } = partitionFiles([
      new File([''], '.DS_Store'),
      new File([''], 'slides.pptx'),
    ])

    expect(accepted).toEqual([])
    expect(rejected).toEqual(['slides.pptx'])
  })
})

describe('filesFromDrop', () => {
  it('walks a dropped folder to its leaves', async () => {
    const { files, folders } = await filesFromDrop(dropOf({ Week1: ['a.pdf', 'b.pdf'] }))

    expect(files.map((file) => file.name)).toEqual(['a.pdf', 'b.pdf'])
    expect(folders).toBe(true)
  })

  it('keeps every folder in a multi-folder drop', async () => {
    // The regression: reading the second folder's entry after awaiting the first returned
    // null, so dragging three week folders in at once uploaded only the first.
    const { files } = await filesFromDrop(
      dropOf({ Week1: ['a.pdf'], Week2: ['b.pdf'], Week3: ['c.pdf'] }),
    )

    expect(files.map((file) => file.name)).toEqual(['a.pdf', 'b.pdf', 'c.pdf'])
  })

  it('takes folders and loose files dropped together', async () => {
    const { files, folders } = await filesFromDrop(
      dropOf({ 'Week1': ['a.pdf'], 'syllabus.pdf': null }),
    )

    expect(files.map((file) => file.name)).toEqual(['a.pdf', 'syllabus.pdf'])
    expect(folders).toBe(true)
  })

  it('says a folder is coming before it starts reading it', async () => {
    // The walk takes seconds on a term's worth of notes, and the well has to be able to
    // say so while it happens rather than sitting idle and then filling up at the end.
    const onFolderScan = vi.fn()

    const pending = filesFromDrop(dropOf({ Week1: ['a.pdf'] }), onFolderScan)

    // Synchronously, in the handler: after the await it would be too late to be useful.
    expect(onFolderScan).toHaveBeenCalledTimes(1)
    await pending
  })

  it('does not announce a scan for a drop of loose files', async () => {
    const onFolderScan = vi.fn()

    await filesFromDrop(dropOf({ 'a.pdf': null }), onFolderScan)

    expect(onFolderScan).not.toHaveBeenCalled()
  })

  it('falls back to the plain file list where the entry API is missing', async () => {
    const plain = { items: [], files: [new File([''], 'a.pdf')] } as unknown as DataTransfer

    const { files, folders } = await filesFromDrop(plain)

    expect(files.map((file) => file.name)).toEqual(['a.pdf'])
    expect(folders).toBe(false)
  })
})
