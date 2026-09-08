'use client'

import { useCallback, useEffect, useRef, useState } from 'react'

import { documentRowId, parseDocumentAttentionAnchor } from '@/lib/attention'
import { needsAttention } from '@/lib/hooks/use-documents'
import { useNavigationVersion, useRouteAnchor, useRouter } from '@/router/hooks'
import type { DocumentRead } from '@/types'

/** How long the arrival emphasis lingers on the revealed row before it settles. */
const HIGHLIGHT_MS = 2600

/**
 * The bounded reveal state one navigation produces. The list polls while anything in it is
 * mid-ingestion, so the arrival must happen exactly once per navigation: the single current
 * navigation record keeps a busy class from re-focusing a row the student has already seen,
 * which would read as the app yanking their attention every two seconds.
 */
type RevealRecord = {
  /** The navigation (and anchor) this record belongs to; a new one replaces it whole. */
  navKey: string
  /** The target this record has been made consistent with. */
  targetId: number | null
  /** Keyboard focus has been moved to the row once under this navigation. */
  focused: boolean
}

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
  /**
   * The filter the pane should show and apply right now: '' while this visit borrows a
   * clearing of the student's filter (the original is untouched - the pane's own state and
   * its session-storage copy still hold it), otherwise null and the pane's filter applies.
   */
  filterOverride: '' | null
  /** Step to the previous/next attention item (wrapping), landing on its row. */
  step: (direction: -1 | 1) => void
  /** End the visit: the anchor leaves the URL and the list returns to itself. */
  dismiss: () => void
}

/**
 * The "needs attention" half of the shared navigation contract, on the document list.
 *
 * Arriving with a `document-N` anchor, the pane stands on the exact row: it temporarily
 * hides a filter that would hide the row (the pane's own filter state - and its session
 * storage - is never written, so the original survives pane unmount, tab navigation,
 * Back/Forward, and reload), scrolls the row into view, moves focus to it, and announces
 * the arrival. Multi-item visits step through the affected documents in list order, one
 * history entry at a time, so Back walks them back. A row that resolves or is deleted while
 * the student stands on the list is handled rather than hunted for: the strip, the visible
 * target, and the live region follow the next live item without moving keyboard focus, and
 * an explicit step (or a new navigation) stands on the next live target with the full reveal.
 */
export function useDocumentAttention(
  documents: DocumentRead[],
  loaded: boolean,
  filter: string,
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
  const [filterOverride, setFilterOverride] = useState<'' | null>(null)
  const recordRef = useRef<RevealRecord | null>(null)
  // The pane's filter when this visit borrowed its clearing, so a value the student types
  // mid-visit can be recognised as their intent and takes over from the override.
  const filterBaselineRef = useRef('')
  const highlightTimerRef = useRef<number | null>(null)

  const armHighlight = useCallback(() => {
    if (highlightTimerRef.current !== null) window.clearTimeout(highlightTimerRef.current)
    highlightTimerRef.current = window.setTimeout(() => setHighlightedId(null), HIGHLIGHT_MS)
  }, [])

  useEffect(() => {
    const navKey = `${navigationVersion}:${anchorDocumentId}`
    if (!active || !loaded) {
      // The visit ended (the anchor cleared, another route owns it, or a tab change
      // rewrote the URL): drop the view state. The filter needs no restoration - this hook
      // never wrote to it, so whatever is in the pane's state is what the student left.
      recordRef.current = null
      setFilterOverride(null)
      setHighlightedId(null)
      setAnnouncement(null)
      return
    }

    if (target === null) {
      // Nothing live to stand on.
      recordRef.current = null
      setFilterOverride(null)
      setHighlightedId(null)
      if (anchored !== null) {
        // The anchored document is here but no longer needs attention, and nothing else
        // does either: the work was done while we were standing on it. End the visit
        // quietly (a replace, not a push) rather than leaving a spent anchor in the URL.
        router.replaceAnchor(null)
        setAnnouncement(null)
      } else {
        // A deleted anchor with nothing left to show is said once, the same way a missing
        // source anchor is, and left in the URL: the history entry still names the place.
        setAnnouncement('That document is no longer in this class.')
      }
      return
    }

    if (filter !== filterBaselineRef.current) {
      // The student typed during the visit (or the pane's filter otherwise changed under
      // us): that is now their intent, and it takes over from the temporary clearing.
      setFilterOverride(null)
    }

    if (recordRef.current?.navKey !== navKey) {
      // A new navigation: the arrival (or a Back/Forward re-arrival). One full reveal for
      // this navigation, and one record that bounds everything it produces.
      recordRef.current = { navKey, targetId: target.id, focused: false }
      // Search state must not be allowed to hide an attention target: show an unfiltered
      // list for the visit without ever writing to the pane's own (persisted) filter.
      const query = filter.trim().toLowerCase()
      filterBaselineRef.current = filter
      setFilterOverride(query && !target.filename.toLowerCase().includes(query) ? '' : null)
      setHighlightedId(target.id)
      setAnnouncement(
        attention.length === 1
          ? `Jumped to ${target.filename}. It needs attention.`
          : `Jumped to ${target.filename}. ${attention.length} documents need attention.`,
      )
      armHighlight()
      return
    }

    if (recordRef.current.targetId !== target.id) {
      // The list changed under an unchanged navigation (a poll resolved or deleted the row
      // we stand on): keep the strip, the visible target, and the live region consistent
      // with the new live target. Deliberately no focus move and no scroll - a background
      // refresh must not yank the keyboard from the student; an explicit step or a new
      // navigation performs the full reveal.
      recordRef.current = { ...recordRef.current, targetId: target.id }
      setHighlightedId(target.id)
      setAnnouncement(
        attention.length === 1
          ? `Now standing on ${target.filename}. It needs attention.`
          : `Now standing on ${target.filename}. ${attention.length} documents need attention.`,
      )
      armHighlight()
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
    router,
    armHighlight,
  ])

  // Focus and scroll as a second stage: the row may not be in the DOM yet when the
  // arrival lands (the list still loading, or the filter clearing this very tick), so
  // this effect retries as the list renders until the row exists. One focus per
  // navigation - the record's `focused` flag is what keeps a poll, or a target swap within
  // the same navigation, from re-stealing the keyboard.
  useEffect(() => {
    const record = recordRef.current
    if (!active || record === null || record.focused || record.targetId === null) return
    const row = document.getElementById(documentRowId(record.targetId))
    if (!row) return
    record.focused = true
    // Focusable exactly for programmatic arrival (tabIndex -1 on every row); focus moves
    // there, the list scrolls the row to the middle of the pane, and ordinary tab order
    // is untouched.
    row.focus({ preventScroll: true })
    row.scrollIntoView({ block: 'center' })
  }, [active, target, documents, navigationVersion])

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
    filterOverride,
    step,
    dismiss,
  }
}
