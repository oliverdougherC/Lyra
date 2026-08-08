/**
 * The draft workspace's poll liveness, and the stage-detail contract it feeds.
 *
 * These two rules are what make a background run visible at all. The poll stops for good
 * the moment the artifact is not pending or generating, and nothing restarts it - so an
 * endpoint that queues a job while leaving the artifact `ready` produces a run the
 * interface never sees. That is exactly what made the Review button look dead: the review
 * ran, filed its findings, and the workspace had stopped listening after one request.
 */

import { describe, expect, it } from 'vitest'

import { draftPollInterval } from '@/lib/hooks/use-drafts'
import type { ArtifactState } from '@/types'

function query(state: ArtifactState | undefined, dataUpdateCount = 0) {
  return {
    state: {
      data: state === undefined ? undefined : { state },
      dataUpdateCount,
    },
  }
}

describe('draftPollInterval', () => {
  it('keeps polling while a job is queued or running', () => {
    // `pending` is the state a freshly queued pass or review is in before the shared
    // worker thread picks it up. Treating it as settled is what lost the review.
    expect(draftPollInterval(query('pending'))).toBeTypeOf('number')
    expect(draftPollInterval(query('generating'))).toBeTypeOf('number')
  })

  it('stops once the run settles, either way', () => {
    expect(draftPollInterval(query('ready'))).toBe(false)
    expect(draftPollInterval(query('failed'))).toBe(false)
  })

  it('backs off to a two-second ceiling', () => {
    expect(draftPollInterval(query('generating', 0))).toBe(500)
    expect(draftPollInterval(query('generating', 2))).toBe(1000)
    expect(draftPollInterval(query('generating', 99))).toBe(2000)
  })

  it('polls before the first answer arrives', () => {
    expect(draftPollInterval(query(undefined))).toBe(500)
  })
})

describe('the stage-detail contract', () => {
  // The workspace tells the two background residents apart by this prefix alone: a
  // review never writes the document, so the student keeps the pen while one runs; a
  // pass owns the document and the editor follows it. The server promises the prefix
  // from the first queued moment (`routes_drafts.QUEUED_REVIEW_DETAIL`).
  const isReview = (detail: string) => detail.startsWith('Reviewing')

  it('reads every review stage as a review', () => {
    expect(isReview('Reviewing (queued)')).toBe(true)
    expect(isReview('Reviewing the document')).toBe(true)
    expect(isReview('Reviewing structure')).toBe(true)
    expect(isReview('Reviewing prose: 1.1 Introduction')).toBe(true)
  })

  it('reads every pass stage as a pass', () => {
    expect(isReview('Queued')).toBe(false)
    expect(isReview('Reading the document')).toBe(false)
    expect(isReview('Structuring the document')).toBe(false)
    expect(isReview('Drafting 1.1 Introduction')).toBe(false)
  })
})
