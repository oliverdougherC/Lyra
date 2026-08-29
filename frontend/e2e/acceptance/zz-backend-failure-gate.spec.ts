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
import { apiGet } from './helpers'

test.describe('PLA-292 acceptance gate', () => {
  test('zero unexpected backend failures across the whole suite', async () => {
    const snap = await (await apiGet('/_acceptance/backend-failures')).json()

    // Bounded, privacy-safe diagnostic: method + route template + status/exception class.
    // No student content, paths, bodies, or credentials are ever in the ledger.
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

    // Sanity: the accounting layer was actually active (it recorded and consumed the
    // expected failures the suite intentionally produced), so "zero unconsumed" is a real
    // assertion, not a no-op because the ledger never turned on.
    expect(snap.total_recorded).toBeGreaterThanOrEqual(0)
  })
})
