import { parseTimestamp } from '@/lib/format'
import type { DocumentRead } from '@/types'

export type DocumentSort = 'date-added' | 'alphabetical' | 'kind'

const names = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' })
const KIND_ORDER = ['Image', 'Markdown', 'PDF', 'Text', 'Other'] as const
export type DocumentKind = (typeof KIND_ORDER)[number]

export function effectiveDocumentName(
  document: Pick<DocumentRead, 'filename' | 'nickname'>,
): string {
  return document.nickname?.trim() || document.filename
}

export function matchesDocumentName(
  document: Pick<DocumentRead, 'filename' | 'nickname'>,
  query: string,
): boolean {
  const needle = query.trim().toLocaleLowerCase()
  return (
    !needle ||
    effectiveDocumentName(document).toLocaleLowerCase().includes(needle) ||
    document.filename.toLocaleLowerCase().includes(needle)
  )
}

/** Identify equal display names without changing the document ID used for selection. */
export function documentNameNote(
  document: DocumentRead,
  documents: readonly DocumentRead[],
): string | null {
  const name = effectiveDocumentName(document)
  const peers = documents.filter((item) => names.compare(effectiveDocumentName(item), name) === 0)
  if (peers.length < 2) return null
  const original = `Original: ${document.filename}`
  if (
    !peers.some(
      (item) => item.id !== document.id && names.compare(item.filename, document.filename) === 0,
    )
  )
    return original
  const added = parseTimestamp(document.created_at).getTime()
  return `${original}${Number.isFinite(added) ? ` · Added ${new Date(added).toLocaleString()}` : ''} · Document #${document.id}`
}

export function documentKind(document: Pick<DocumentRead, 'filename' | 'mime'>): DocumentKind {
  const mime = (document.mime ?? '').toLowerCase()
  const suffix = document.filename.split('.').pop()?.toLowerCase()
  if (mime === 'application/pdf') return 'PDF'
  if (mime.startsWith('image/')) return 'Image'
  if (mime === 'text/markdown') return 'Markdown'
  if (mime === 'text/plain') return 'Text'
  if (suffix === 'pdf') return 'PDF'
  if (['png', 'jpg', 'jpeg'].includes(suffix ?? '')) return 'Image'
  if (suffix === 'md') return 'Markdown'
  if (suffix === 'txt') return 'Text'
  return 'Other'
}

function createdAt(document: DocumentRead): number {
  if (typeof document.created_at !== 'string' || !document.created_at)
    return Number.NEGATIVE_INFINITY
  const time = parseTimestamp(document.created_at).getTime()
  return Number.isFinite(time) ? time : Number.NEGATIVE_INFINITY
}

/** Presentation order only; document IDs remain the identity for actions and deep links. */
export function sortDocuments(
  documents: readonly DocumentRead[],
  sort: DocumentSort,
): DocumentRead[] {
  return [...documents].sort((left, right) => {
    if (sort === 'date-added') return createdAt(right) - createdAt(left) || left.id - right.id
    if (sort === 'kind') {
      const group = KIND_ORDER.indexOf(documentKind(left)) - KIND_ORDER.indexOf(documentKind(right))
      if (group) return group
    }
    return (
      names.compare(effectiveDocumentName(left), effectiveDocumentName(right)) || left.id - right.id
    )
  })
}
