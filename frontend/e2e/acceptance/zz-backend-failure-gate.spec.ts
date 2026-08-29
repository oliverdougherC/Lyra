/**
 * PLA-292 acceptance gate: zero unexpected backend failures.
 *
 * This is the authoritative invariant for the whole lane. The harness wraps the REAL
 * production FastAPI app in an ASGI accounting layer that records every unexpected
 * backend failure -- an unhandled request exception, or an unexpected 5xx response --
 * with bounded privacy-safe metadata (method, route template, status/exception class).
 * Tests that intentionally exercise an expected 5xx consume that specific occurrence;
 * they never globally suppress a route or status class.
 *
 * Named `zz-` so it runs last (Playwright orders files alphabetically within a project):
 * by the time this gate runs, every other spec has finished, so any unconsumed failure
 * is one no test claimed as expected -- i.e. a hidden backend failure that would have
 * been invisible to Playwright assertions alone. The lane fails on it.
 */

import { test, expect } from '@playwright/test'
import { apiGet, apiPost } from './helpers'

test.describe('PLA-292 acceptance gate', () => {
  test('accounting layer is live: probe -> record -> inspect -> consume -> clear', async () => {
    const before = await (await apiGet('/_acceptance/backend-failures')).json()
    const baseTotal = before.total_recorded as number
    const baseUnconsumed = before.unconsumed_count as number

    const probeRes = await apiPost('/_acceptance/backend-failures/probe')
    expect(probeRes.status).toBe(500)

    const after = await (await apiGet('/_acceptance/backend-failures')).json()
    expect(after.total_recorded).toBe(baseTotal + 1)
    expect(after.unconsumed_count).toBe(baseUnconsumed + 1)

    const recorded = (after.unconsumed as Array<Record<string, unknown>>).find(
      (f) => f.exc_type === 'RuntimeError' && f.route === '/_acceptance/backend-failures/probe',
    )
    expect(recorded, 'probe failure should appear in the ledger with bounded metadata').toBeTruthy()
    expect(recorded!.kind).toBe('unhandled_exception')
    expect(recorded!.method).toBe('POST')
    expect(typeof recorded!.id).toBe('number')
    expect(typeof recorded!.at).toBe('number')

    const consumeRes = await apiPost('/_acceptance/backend-failures/consume', {
      failure_id: recorded!.id,
    })
    expect(consumeRes.ok).toBe(true)
    const consumeBody = await consumeRes.json()
    expect(consumeBody.ok).toBe(true)

    const final = await (await apiGet('/_acceptance/backend-failures')).json()
    expect(final.unconsumed_count).toBe(baseUnconsumed)
    expect(final.total_recorded).toBe(baseTotal + 1)
    expect(final.consumed).toBe(before.consumed + 1)
  })

  test('zero unexpected backend failures across the whole suite', async () => {
    const snap = await (await apiGet('/_acceptance/backend-failures')).json()

    if (snap.unconsumed_count > 0) {
      const lines = snap.unconsumed.slice(0, 10).map((f: Record<string, unknown>) => {
        return `  - [${f.method}] ${f.route} -> ${f.kind} status=${f.status ?? '-'} exc=${f.exc_type ?? '-'}`
      })
      const more = snap.unconsumed_count > 10 ? `  ... and ${snap.unconsumed_count - 10} more` : ''
      throw new Error(
        `Acceptance gate FAILED: ${snap.unconsumed_count} unexpected backend failure(s) were ` +
          `not consumed as expected by any test (total recorded this run: ${snap.total_recorded}, ` +
          `consumed as expected: ${snap.consumed}).\n` +
          `Unconsumed failures:\n${lines.join('\n')}${more}`,
      )
    }

    expect(
      snap.total_recorded,
      'accounting layer must have recorded at least the self-test probe',
    ).toBeGreaterThan(0)
  })
})
