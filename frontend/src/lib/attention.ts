/**
 * The shared "needs attention" destination contract.
 *
 * Any surface that tells the user something needs attention must carry enough target
 * context to resolve the exact destination: a stable id inside a structured anchor,
 * never a display-text guess. The router's reserved `lyra-anchor` query parameter carries
 * the anchor through the URL, history entries, and reloads; the receiving surface reveals
 * the exact item (scroll, focus, announce) instead of dropping the user at the top of a
 * list they have to hunt through.
 *
 * This module owns the *shape* of those anchors and the row ids they point at. Every
 * surface that lists documents names its rows with the same vocabulary, so a destination
 * built here resolves to exactly one row without re-deriving naming rules.
 */

import { ROUTE_ANCHOR_QUERY_KEY } from '@/router/hooks'

/** The anchor prefix for attention destinations on the document list. */
export const DOCUMENT_ATTENTION_ANCHOR_PREFIX = 'document-'

/**
 * The stable id of one document's row. Row ids and attention anchors share the same
 * vocabulary on purpose: an anchor is a row id, and `getElementById` is the resolution.
 */
export function documentRowId(documentId: number): string {
  return `${DOCUMENT_ATTENTION_ANCHOR_PREFIX}${documentId}`
}

/**
 * The document id inside an attention anchor, or null for any other anchor (a source
 * jump, a settings disclosure, ...) and for ids that do not name a real document.
 */
export function parseDocumentAttentionAnchor(anchor: string | null | undefined): number | null {
  if (!anchor?.startsWith(DOCUMENT_ATTENTION_ANCHOR_PREFIX)) return null
  const id = Number(anchor.slice(DOCUMENT_ATTENTION_ANCHOR_PREFIX.length))
  return Number.isSafeInteger(id) && id > 0 ? id : null
}

/**
 * The deep link into a class's Files tab aimed at one document: the `tab` parameter puts
 * the list on screen, the anchor tells it which row to reveal. The id travels in the URL,
 * so the destination survives Back/Forward, reloads, and a sent link.
 */
export function documentAttentionHref(classId: number, documentId: number): string {
  const params = new URLSearchParams()
  params.set('tab', 'files')
  params.set(ROUTE_ANCHOR_QUERY_KEY, documentRowId(documentId))
  return `/classes/${classId}?${params.toString()}`
}
