'use client'

import { useCallback, useEffect, useRef, useState } from 'react'

import { documentRowId, parseDocumentAttentionAnchor } from '@/lib/attention'
import { needsAttention } from '@/lib/hooks/use-documents'
import { useNavigationVersion, useRouteAnchor, useRouter } from '@/router/hooks'
import type { DocumentRead } from '@/types'

/** How long the arrival emphasis lingers on the revealed row before it settles. */
const HIGHLIGHT_MS = 2600

/**
 * The bounded reveal state one arrival produces. The list polls while anything in it is
 * mid-ingestion, so a reveal must happen exactly once per navigation, not once per poll:
 * the remembered keys keep a busy class from re-focusing a row the student has already
 * seen, which would read as the app yanking their attention every two seconds.
 */
type DocumentAttention = {
  /** A document attention anchor is active on this route. */
  active: boolean
  /** Every list item that needs attention, in list order. */
  attention: DocumentRead[]
  /** The row the anchor stands on right now: the anchored one, else the first attention item. */
  target: DocumentRead | null
  /** 1-based position of `target` among the attention items. */
  position: number | null
  /** The row currently wearing the arrival emphasis, if the highlight has not expired. */
  highlightedId: number | null
  /** The live-region sentence for the current arrival, or null. */
  announcement: string | null
  /** The navigation that produced the current arrival, for re-announcing it. */
  navigationVersion: number
  /** Step to the previous/next attention item (wrapping), landing on its row. */
  step: (direction: -1 | 1) => void
  /** End the visit: the anchor leaves the URL and the list returns to itself. */
  dismiss: () => void
}

/**
 * The "needs attention" half of the shared navigation contract, on the document list.
 *
 * Arriving with a `document-N` anchor, the pane stands on the exact row: it clears a
 * filter that would hide the row (restoring it when the visit ends), scrolls the row into
 * view, moves focus to it, and announces the arrival. Multi-item visits step through the
 * affected documents in list order, one history entry at a time, so Back walks them back.
 * A row that resolved or was deleted in the meantime is handled rather than hunted for:
 * the visit lands on the next item that still needs attention, or says plainly that the
 * document is gone.
 *
 * `filter` and `setFilter` are the pane's own search state: the reveal borrows a clearing
 * of it for the duration of the visit and gives it back on the way out, which is what
 * "the list's state cannot hide the target, and return restores it" means in practice.
 */
export function useDocumentAttention(
  documents: DocumentRead[],
  loaded: boolean,
  filter: string,
  setFilter: (value: string) => void,
): DocumentAttention {
  const router = useRouter()
  const routeAnchor = useRouteAnchor()
  const navigationVersion = useNavigationVersion()
  const anchorDocumentId = parseDocumentAttentionAnchor(routeAnchor)
  const active = anchorDocumentId !== null

  const attention = documents.filter((document) => needsAttention(document.state))
  const anchored = active
    ? (documents.find((document) => document.id === anchorDocumentId) ?? null)
    : null
  // The row to stand on. The anchored one when it still needs attention; otherwise the
  // first attention item - the anchored one may have been deleted (land on the rest) or
  // have just resolved (the others still need the student).
  const target =
    anchored !== null && needsAttention(anchored.state) ? anchored : (attention[0] ?? null)
  const position = target ? attention.findIndex((document) => document.id === target.id) + 1 : null

  const [highlightedId, setHighlightedId] = useState<number | null>(null)
  const [announcement, setAnnouncement] = useState<string | null>(null)
  const revealedRef = useRef(new Set<string>())
  const focusedRef = useRef(new Set<string>())
  const highlightTimerRef = useRef<number | null>(null)
  const filterBackupRef = useRef<string | null>(null)

  useEffect(() => {
    if (!active || !loaded) {
      // The visit ended (the anchor cleared, another route owns it, or a tab change
      // rewrote the URL): hand the filter back if this visit borrowed the clearing. A
      // filter the student typed themselves is kept, because that is now their intent.
      if (filterBackupRef.current !== null) {
        if (filter === '') setFilter(filterBackupRef.current)
        filterBackupRef.current = null
      }
      if (highlightTimerRef.current !== null) {
        window.clearTimeout(highlightTimerRef.current)
        highlightTimerRef.current = null
      }
      setHighlightedId(null)
      setAnnouncement(null)
      return
    }

    if (target === null) {
      // The anchored document is here but no longer needs attention, and nothing else
      // does either: the work was done while we were standing on it. End the visit
      // quietly (a replace, not a push) rather than leaving a spent anchor in the URL.
      if (anchored !== null) {
        router.replaceAnchor(null)
      } else {
        // A deleted anchor with nothing left to show is said once, the same way a missing
        // source anchor is, and left in the URL: the history entry still names the place.
        setAnnouncement('That document is no longer in this class.')
      }
      setHighlightedId(null)
      return
    }

    const revealKey = `${navigationVersion}:${anchorDocumentId}`
    if (!revealedRef.current.has(revealKey)) {
      revealedRef.current.add(revealKey)
      // Bound the set: one entry per navigation, and a session's worth of steps is small.
      if (revealedRef.current.size > 32) {
        revealedRef.current = new Set([...revealedRef.current].slice(-16))
      }
      // Search state must not be allowed to hide an attention target.
      const query = filter.trim().toLowerCase()
      if (query && !target.filename.toLowerCase().includes(query)) {
        filterBackupRef.current = filter
        setFilter('')
      }
      setHighlightedId(target.id)
      setAnnouncement(
        attention.length === 1
          ? `Jumped to ${target.filename}. It needs attention.`
          : `Jumped to ${target.filename}. ${attention.length} documents need attention.`,
      )
      if (highlightTimerRef.current !== null) window.clearTimeout(highlightTimerRef.current)
      highlightTimerRef.current = window.setTimeout(() => setHighlightedId(null), HIGHLIGHT_MS)
    }
  }, [
    active,
    loaded,
    target,
    anchored,
    attention,
    navigationVersion,
    anchorDocumentId,
    filter,
    setFilter,
    router,
  ])

  // Focus and scroll as a second stage: the row may not be in the DOM yet when the
  // arrival lands (the list still loading, or the filter clearing this very tick), so
  // this effect retries as the list renders until the row exists. Once it stands on a
  // row, the same row is never stood on twice for one navigation - a poll re-rendering
  // the list must not re-steal focus.
  useEffect(() => {
    if (highlightedId === null || !active || anchorDocumentId === null) return
    const key = `${navigationVersion}:${anchorDocumentId}:${highlightedId}`
    if (focusedRef.current.has(key)) return
    const row = document.getElementById(documentRowId(highlightedId))
    if (!row) return
    focusedRef.current.add(key)
    // Focusable exactly for programmatic arrival (tabIndex -1 on every row); focus moves
    // there, the list scrolls the row to the middle of the pane, and ordinary tab order
    // is untouched.
    row.focus({ preventScroll: true })
    row.scrollIntoView({ block: 'center' })
  }, [highlightedId, active, anchorDocumentId, navigationVersion, documents])

  // Give the highlight its time to settle, then let the row return to the list's colours.
  useEffect(() => {
    return () => {
      if (highlightTimerRef.current !== null) window.clearTimeout(highlightTimerRef.current)
    }
  }, [])

  const step = useCallback(
    (direction: -1 | 1) => {
      if (attention.length < 2) return
      const current = target ? attention.findIndex((document) => document.id === target.id) : 0
      const nextIndex = (current + direction + attention.length) % attention.length
      const next = attention[nextIndex]
      const nextAnchor = documentRowId(next.id)
      if (routeAnchor === nextAnchor) router.replaceAnchor(nextAnchor)
      else router.pushAnchor(nextAnchor)
    },
    [attention, target, routeAnchor, router],
  )

  const dismiss = useCallback(() => {
    router.replaceAnchor(null)
  }, [router])

  return {
    active,
    attention,
    target,
    position,
    highlightedId,
    announcement,
    navigationVersion,
    step,
    dismiss,
  }
}
