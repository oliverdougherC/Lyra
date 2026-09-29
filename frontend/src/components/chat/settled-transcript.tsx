'use client'

import type { ChatMessage } from '@/components/chat/message-bubble'

/**
 * The settled transcript's shared pieces (PLA-510).
 *
 * The pane re-renders on every composer keystroke and every live publication. The settled
 * rows must stop paying for that: their React elements are built once per settled-rows /
 * handoff change (a memoized element list the pane renders flat, so the tree structure —
 * and therefore each row's React identity — is exactly what a single flat list produced),
 * not once per keystroke. This module holds the handoff shape and the time-gap rule that
 * element list is built with.
 *
 * Row identity is unchanged: each row renders under the key its own handoff handed over
 * (first match wins, in settlement order), or its persisted ID when no handoff covers it.
 * Selections a reader held inside the live answer travel on the same handoff.
 */

/** A visible gap in time this wide opens with a wider row margin. */
export const TIME_GAP_MS = 60 * 60 * 1000

/**
 * The identity handoff for a settled turn: the persisted rows keep the keys their
 * optimistic twins streamed under, so the answer the reader has been watching is the
 * same React identity after the turn settles. Retired when the pane leaves the
 * conversation.
 */
export type SettledHandoff = {
  turnId: number
  sessionId: number
  /** The settled answer row keeps this key (the key its optimistic twin streamed under). */
  assistantId: number
  assistantKey: string
  /** Set when the turn's user row verified (adjacent, same text); otherwise the user
   *  row settles under its own persisted ID. */
  userId?: number
  userKey?: string
  /** A selection the reader held inside the live answer, as text offsets: the static
   *  renderer swaps the row's inner nodes, which resets a live selection even with the
   *  same outer node, so the handoff carries the range back. */
  selection?: { anchor: number; focus: number }
}

/**
 * Whether a visible gap in time separates `current` from the row before it. Takes
 * already-parsed timestamps: parsing each row's stamp on every render of every row is
 * the kind of repeated work the settled transcript should not pay again per keystroke —
 * the caller caches the parse per message.
 */
export function startsTimeGapBetween(
  current: ChatMessage,
  previous: ChatMessage | null,
  timestampOf: (message: ChatMessage) => number,
): boolean {
  if (previous === null) return true
  return timestampOf(current) - timestampOf(previous) > TIME_GAP_MS
}
