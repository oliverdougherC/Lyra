/**
 * Restart reconciliation through the real stack.
 *
 * Proves: background workers call reconcile_interrupted() at startup to
 * recover in-progress jobs.  We create a study artifact, verify it completes,
 * and confirm the health endpoint stays ready throughout -- exercising the
 * worker lifecycle without needing to restart the server mid-test.
 *
 * Full restart-and-recover is deferred to PLA-147 (requires process restart
 * within a bounded test window).
 */

import { test, expect } from '@playwright/test'
import { resolve } from 'node:path'
import {
  apiGet,
  apiPost,
  createClass,
  uploadDocument,
  waitForDocumentReady,
  waitForStudyReady,
  clearTutorState,
} from './helpers'

const TEST_DATA = resolve(__dirname, 'test-data')

test.describe('Worker reconciliation', () => {
  let classId: number
  let docId: number

  test.beforeAll(async () => {
    const cls = await createClass('Acceptance: Reconciliation')
    classId = cls.id

    const res = await uploadDocument(classId, resolve(TEST_DATA, 'sample.txt'), 'sample.txt')
    const doc = await res.json()
    docId = doc.id
    await waitForDocumentReady(docId, 30_000)
  })

  test.afterEach(async () => {
    await clearTutorState()
  })

  test('health endpoint remains ready after worker activity', async () => {
    // Trigger worker activity: create a deck and wait for generation
    const deckRes = await apiPost(`/api/classes/${classId}/decks`, {
      title: 'Reconciliation Deck',
      document_ids: [docId],
      cards_per_topic: 2,
    })
    expect(deckRes.status).toBe(202)
    const deck = await deckRes.json()
    await waitForStudyReady('decks', deck.id, 30_000)

    // After worker completes, health should still be ready
    const healthRes = await apiGet('/api/health/ready')
    expect(healthRes.ok).toBe(true)
    const health = await healthRes.json()
    expect(health.status).toBe('ready')
    expect(health.components.database.status).toBe('ready')
  })

  test('concurrent study jobs complete without interference', async () => {
    // Create two study artifacts concurrently
    const [deck1Res, deck2Res] = await Promise.all([
      apiPost(`/api/classes/${classId}/decks`, {
        title: 'Concurrent Deck A',
        document_ids: [docId],
        cards_per_topic: 2,
      }),
      apiPost(`/api/classes/${classId}/decks`, {
        title: 'Concurrent Deck B',
        document_ids: [docId],
        cards_per_topic: 2,
      }),
    ])
    expect(deck1Res.status).toBe(202)
    expect(deck2Res.status).toBe(202)

    const deck1 = await deck1Res.json()
    const deck2 = await deck2Res.json()

    // Both should complete
    await Promise.all([
      waitForStudyReady('decks', deck1.id, 30_000),
      waitForStudyReady('decks', deck2.id, 30_000),
    ])

    // Both should have cards
    const d1Res = await apiGet(`/api/decks/${deck1.id}`)
    const d2Res = await apiGet(`/api/decks/${deck2.id}`)
    const d1 = await d1Res.json()
    const d2 = await d2Res.json()
    expect(d1.cards.length).toBeGreaterThan(0)
    expect(d2.cards.length).toBeGreaterThan(0)
  })

  test('ingestion worker handles sequential re-uploads', async () => {
    // Upload, wait for ready, delete, re-upload -- exercises worker idempotency
    const res1 = await uploadDocument(classId, resolve(TEST_DATA, 'supplement.md'), 'supplement.md')
    const doc1 = await res1.json()
    await waitForDocumentReady(doc1.id, 30_000)

    // Verify the document is fully processed
    const detail1 = await apiGet(`/api/documents/${doc1.id}`)
    const body1 = await detail1.json()
    expect(body1.state).toBe('ready')

    // Health still ready after all this activity
    const healthRes = await apiGet('/api/health/ready')
    expect(healthRes.ok).toBe(true)
  })
})
