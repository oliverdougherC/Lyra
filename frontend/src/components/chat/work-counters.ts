'use client'

/**
 * Lightweight always-on work counters for the chat pane's hot paths.
 *
 * The cost reports (PLA-501's streaming-cost suite and the chat work report) need to
 * attribute per-commit work to the code that does it, and a counter that the production
 * components update is the only way to read that off the real components in a jsdom
 * playback. Each increment is one property write, so the standing cost is negligible;
 * the precedent is `reveal.revealWork`, which the cost gate has read all along.
 *
 * The counters are per-process (the test worker), not per message or per pane: a number
 * is only meaningful for the window it was reset for, so the reports reset before each
 * phase they measure. They are diagnostics, not a contract: production code must never
 * branch on their values.
 */
export const chatWork = {
  /** Every `MessageRow` render (settled rows included). */
  rowRenders: 0,
  /** `MessageRow` renders of the live streaming row. */
  liveRowRenders: 0,
  /** Handoff lookups for row keys / selection restore (one per row per transcript pass). */
  handoffLookups: 0,
  /**
   * Settled rows the transcript pass iterated over. A memoized transcript that bails out
   * on a parent re-render contributes zero; a non-zero delta during composer typing or a
   * live publication is O(history) parent work still being paid (PLA-510).
   */
  transcriptIterations: 0,
  /** Containment steps the pre-index linear handoff scan performed (legacy cost). */
  handoffScanSteps: 0,
  /** Whole-document Markdown normalizations actually run (a cache recompute, not a render). */
  markdownNormalizations: 0,
  /** Characters handed to the normalizer across those runs. */
  normalizedChars: 0,
  /**
   * Whole-document re-parses of the rendered answer: every React render of the memoized
   * document subtree (react-markdown has no internal cache, so a render IS a full
   * remark/rehype pass plus element-tree reconciliation). In a healthy schedule this
   * tracks markdownNormalizations; a gap between the two is re-parse work the
   * normalization cache did not account for.
   */
  markdownRenders: 0,
  /** Reveal nodes the cascade pass queried off the DOM in a commit. */
  revealNodeVisits: 0,
  /** Answer-text publications delivered to the rows (first word and coalesced frames). */
  answerPublications: 0,
  /** Reasoning-text publications delivered to the rows. */
  reasoningPublications: 0,
  /** Stream-follow scroll operations that actually ran (coalesced or not). */
  followScrolls: 0,
}

export function resetChatWork(): void {
  chatWork.rowRenders = 0
  chatWork.liveRowRenders = 0
  chatWork.handoffLookups = 0
  chatWork.transcriptIterations = 0
  chatWork.handoffScanSteps = 0
  chatWork.markdownNormalizations = 0
  chatWork.normalizedChars = 0
  chatWork.markdownRenders = 0
  chatWork.revealNodeVisits = 0
  chatWork.answerPublications = 0
  chatWork.reasoningPublications = 0
  chatWork.followScrolls = 0
}
