/**
 * PLA-317 real-stack revalidation under the backend-failure accounting gate.
 *
 * The merged PLA-317 fix hardens the SQLite WAL sidecar (lyra.db / -shm / -wal)
 * so an ordinary per-request connection can no longer hit FileExistsError on the
 * -shm sidecar and then FileNotFoundError, which surfaced as a hidden backend 500.
 *
 * This spec exercises enough overlapping ordinary API + browser activity that each
 * request opens its own SQLite connection (the production `connect()` path runs the
 * WAL sidecar hardening on every open). If the merged fix is absent from this stack,
 * the race re-appears as an unexpected 5xx / unhandled filesystem exception and the
 * global failure accounting records it -- which the final gate then fails on. We do
 * NOT reimplement PLA-317 here; we only drive real concurrency at the real backend.
 */

import { test, expect } from '@playwright/test'
import { apiGet, apiPost, createClass, createSession, navigateToClass } from './helpers'

test.describe('PLA-317 concurrent WAL revalidation', () => {
  test('overlapping per-request API + browser activity produces zero unexpected backend failures', async ({
    page,
  }) => {
    const cls = await createClass('WAL Stress Class')
    const session = await createSession(cls.id)

    // Seed a little durable state so the concurrent reads have real rows to touch.
    const sendRes = await apiPost(`/api/sessions/${session.id}/chat`, {
      content: 'What is the first law of thermodynamics?',
      mode: 'guide',
    })
    expect(sendRes.status).toBe(200)
    // Drain the SSE stream so the turn settles before we start the burst.
    const reader = sendRes.body?.getReader()
    if (reader) {
      while (!(await reader.read()).done) {
        /* drain */
      }
    }

    // Snapshot the ledger so this test only accounts for failures it caused.
    const before = await (await apiGet('/_acceptance/backend-failures')).json()

    // Fire a burst of overlapping ordinary requests. Each opens its own SQLite
    // connection, so this is exactly the concurrency the PLA-317 sidecar hardening
    // must survive. Mix reads and writes: reads are the common per-request path,
    // writes force real WAL commits to interleave with the sidecar preparation.
    const N = 60
    const tasks: Promise<unknown>[] = []
    for (let i = 0; i < N; i++) {
      if (i % 3 === 0) {
        // Concurrent session creation (write path, each a fresh connection).
        tasks.push(apiPost(`/api/classes/${cls.id}/sessions`, {}))
      } else if (i % 3 === 1) {
        // Concurrent message read of the seeded session.
        tasks.push(apiGet(`/api/sessions/${session.id}/messages`))
      } else {
        // Concurrent settings read (another per-request connection).
        tasks.push(apiGet('/api/settings'))
      }
    }

    // Overlap a real browser poll against the same data while the API burst runs.
    const browserPoll = (async () => {
      await navigateToClass(page, cls.id)
      // Force a couple of refetches so the browser is issuing ordinary GETs in
      // parallel with the Node-side burst.
      for (let i = 0; i < 3; i++) {
        await page.reload({ waitUntil: 'domcontentloaded' })
      }
    })()

    const results = await Promise.allSettled([...tasks, browserPoll])

    // Every ordinary request must have succeeded. A rejected promise here is a
    // client-side failure; an unexpected 5xx would also be caught by the ledger.
    const apiResults = results.slice(0, tasks.length)
    const failures = apiResults.filter((r) => r.status === 'rejected')
    expect(failures).toEqual([])

    // The browser poll must have completed without throwing.
    expect(results[tasks.length].status).toBe('fulfilled')

    // The authoritative assertion: the global failure accounting recorded ZERO new
    // unexpected backend failures (5xx or unhandled exception) during the burst. We use
    // the total_recorded delta so this test only counts failures it caused, and print a
    // bounded privacy-safe diagnostic if any slipped through.
    const after = await (await apiGet('/_acceptance/backend-failures')).json()
    const recordedDelta = after.total_recorded - before.total_recorded
    expect(
      recordedDelta,
      `PLA-317: ${recordedDelta} unexpected backend failure(s) during concurrent WAL activity; ` +
        `unconsumed ledger: ${JSON.stringify(after.unconsumed.slice(0, 5))}`,
    ).toBe(0)
  })
})
