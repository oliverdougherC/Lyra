/**
 * PLA-292 CI Gate proof — DELIBERATELY FAILING acceptance assertion.
 *
 * This spec exists only on the temporary gate-proof branch. It proves that a real
 * acceptance failure makes the required `Full-stack acceptance` job fail and, through
 * it, the aggregate required `CI Gate`. DO NOT MERGE.
 */

import { test, expect } from '@playwright/test'

test('PLA-292 gate proof: this assertion fails on purpose', () => {
  expect(1, 'intentional PLA-292 CI Gate proof failure — DO NOT MERGE').toBe(2)
})
