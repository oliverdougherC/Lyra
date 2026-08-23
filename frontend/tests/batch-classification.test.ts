import { describe, expect, it } from 'vitest'

import { batchSummaryTitle, classifyBatch, needsAttention } from '@/lib/hooks/use-documents'
import type { DocumentState } from '@/types'

/**
 * The batch summary must count every terminal state by what it means. The reported bug was
 * that `unsupported` - a file Lyra cannot use - was tallied as a success, so a batch
 * carrying one could finish with the all-success title and check icon. These tests pin the
 * classification to the same `needsAttention()` line the document rows use, so the pane and
 * the rows cannot drift back apart.
 */
describe('classifyBatch', () => {
  it('counts an all-ready batch as entirely successful', () => {
    expect(classifyBatch(['ready', 'ready', 'ready'])).toEqual({
      ready: 3,
      needsAttention: 0,
      settled: 3,
    })
  })

  it('separates a processing failure from the successes', () => {
    expect(classifyBatch(['ready', 'failed'])).toEqual({
      ready: 1,
      needsAttention: 1,
      settled: 2,
    })
  })

  it('treats an unsupported item as needing attention, not as a success', () => {
    // The core of PLA-293: `unsupported` is terminal but unusable, so it must not inflate
    // the success count the way it used to.
    expect(classifyBatch(['ready', 'unsupported'])).toEqual({
      ready: 1,
      needsAttention: 1,
      settled: 2,
    })
  })

  it('counts an all-unsupported batch as all needing attention', () => {
    expect(classifyBatch(['unsupported', 'unsupported'])).toEqual({
      ready: 0,
      needsAttention: 2,
      settled: 2,
    })
  })

  it('ignores documents that have not settled yet', () => {
    const inFlight: DocumentState[] = ['pending', 'parsing', 'chunking', 'embedding', 'extracting']
    expect(classifyBatch([...inFlight, 'ready'])).toEqual({
      ready: 1,
      needsAttention: 0,
      settled: 1,
    })
  })

  it('agrees with needsAttention on every terminal state', () => {
    // The guarantee that keeps the summary from disagreeing with the rows.
    for (const state of ['ready', 'failed', 'unsupported'] as const) {
      const outcome = classifyBatch([state])
      expect(outcome.needsAttention).toBe(needsAttention(state) ? 1 : 0)
    }
  })
})

describe('batchSummaryTitle', () => {
  it('claims all-success only when nothing needs attention', () => {
    expect(batchSummaryTitle(0)).toBe('All documents processed')
  })

  it('never shows the all-success copy once one item needs attention', () => {
    // Covers a request/upload failure combined with a terminal ingestion failure: the pane
    // sums both into the attention total, and any positive total drops the success copy.
    expect(batchSummaryTitle(1)).toBe('1 item needs attention')
    expect(batchSummaryTitle(3)).toBe('3 items need attention')
    for (const attention of [1, 2, 5, 12]) {
      expect(batchSummaryTitle(attention)).not.toBe('All documents processed')
    }
  })
})
