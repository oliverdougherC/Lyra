/**
 * Harness stubs for PLA-313, PLA-315, and PLA-316 (concurrent branch).
 *
 * These tests verify that the acceptance harness can exercise the contracts
 * these tickets will prove, but they are GATED on the corresponding
 * production fixes landing.  Each test documents the expected contract and
 * skips with a clear reason until the fix is merged.
 *
 * When the production fixes land on main and this branch is rebased:
 *   1. Remove the test.skip() call
 *   2. Implement the actual assertion
 *   3. Verify the test passes against the fixed code
 */

import { test, expect } from '@playwright/test'
import {
  createClass,
  createSession,
  apiPost,
  clearTutorState,
  setTutorMode,
  BACKEND,
} from './helpers'

test.describe('Concurrent branch stubs (PLA-313/315/316)', () => {
  let classId: number

  test.beforeAll(async () => {
    const cls = await createClass('Acceptance: Concurrent Stubs')
    classId = cls.id
  })

  test.afterEach(async () => {
    await clearTutorState()
  })

  test('PLA-313: agent chat respects per-session turn claim', async () => {
    test.skip(true, 'Gated on PLA-313 production fix (concurrent branch)')

    // When PLA-313 lands, this test should:
    // 1. Start an agent chat turn that blocks (barrier mode)
    // 2. Attempt a second turn on the same agent session
    // 3. Verify the second turn is rejected with 409
    // 4. Release the barrier and verify the first turn completes
  })

  test('PLA-315: agent turn attempt lifecycle tracks retries', async () => {
    test.skip(true, 'Gated on PLA-315 production fix (concurrent branch)')

    // When PLA-315 lands, this test should:
    // 1. Start an agent chat turn that fails (error-before-stream)
    // 2. Verify the attempt is recorded with a settled marker
    // 3. Retry the turn
    // 4. Verify the retry creates a new attempt, not a duplicate question
    // 5. Verify the attempt count in the session metadata
  })

  test('PLA-316: inline writer window restricts concurrent edits', async () => {
    test.skip(true, 'Gated on PLA-316 production fix (concurrent branch)')

    // When PLA-316 lands, this test should:
    // 1. Start a writer run on a draft
    // 2. Attempt a concurrent writer run on the same draft
    // 3. Verify the concurrent run is rejected
    // 4. Wait for the first run to complete
    // 5. Verify the second run can then proceed
  })
})
