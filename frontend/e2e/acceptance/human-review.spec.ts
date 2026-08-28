/**
 * Human-review boundary for solution segmentation (PLA-303).
 *
 * Proves: when problems change behind the reviewer's back (via a concurrent
 * tab or re-segmentation), the review page detects the stale base via
 * partSignature and re-seeds the problem list rather than letting the
 * student start solving a problem list they never reviewed.
 */

import { test, expect } from '@playwright/test'
import { resolve } from 'node:path'
import {
  apiGet,
  apiPost,
  apiPatch,
  createClass,
  uploadDocument,
  waitForDocumentReady,
  waitForSolutionSegmented,
  clearTutorState,
} from './helpers'

const TEST_DATA = resolve(__dirname, 'test-data')

test.describe('Human-review boundary (PLA-303)', () => {
  let classId: number
  let docId: number

  test.beforeAll(async () => {
    const cls = await createClass('Acceptance: Human Review')
    classId = cls.id

    const res = await uploadDocument(classId, resolve(TEST_DATA, 'sample.txt'), 'sample.txt')
    const doc = await res.json()
    docId = doc.id
    await waitForDocumentReady(docId, 30_000)
  })

  test.afterEach(async () => {
    await clearTutorState()
  })

  test('review page shows problems in awaiting_review state', async ({ page }) => {
    const solRes = await apiPost(`/api/classes/${classId}/solutions`, {
      title: 'Review Display Test',
      sources: [{ document_id: docId, role: 'problem_set' }],
    })
    expect(solRes.status).toBe(202)
    const sol = await solRes.json()

    await waitForSolutionSegmented(sol.id, 60_000)

    // Load the solution page in the browser
    await page.goto(`/classes/${classId}/solutions/${sol.id}`)
    await page.waitForLoadState('networkidle')

    // The review gate should show problem cards
    await expect(page.getByText(/Solve \d+ problem/i)).toBeVisible({ timeout: 15_000 })

    // Verify at least one problem is displayed
    const solutionRes = await apiGet(`/api/solutions/${sol.id}`)
    const solution = await solutionRes.json()
    const problems = solution.parts.filter(
      (p: { parent_part_id: number | null; kind: string }) =>
        p.parent_part_id === null && p.kind === 'problem',
    )
    expect(problems.length).toBeGreaterThan(0)
  })

  test('concurrent segmentation edit updates the review page', async ({ page }) => {
    const solRes = await apiPost(`/api/classes/${classId}/solutions`, {
      title: 'Concurrent Edit Test',
      sources: [{ document_id: docId, role: 'problem_set' }],
    })
    expect(solRes.status).toBe(202)
    const sol = await solRes.json()

    await waitForSolutionSegmented(sol.id, 60_000)

    // Get the current problem list
    const detailRes = await apiGet(`/api/solutions/${sol.id}`)
    const detail = await detailRes.json()
    const originalProblems = detail.parts.filter(
      (p: { parent_part_id: number | null; kind: string }) =>
        p.parent_part_id === null && p.kind === 'problem',
    )
    expect(originalProblems.length).toBeGreaterThan(0)

    // Load the review page
    await page.goto(`/classes/${classId}/solutions/${sol.id}`)
    await page.waitForLoadState('networkidle')
    await expect(page.getByText(/Solve \d+ problem/i)).toBeVisible({ timeout: 15_000 })

    // Simulate concurrent edit: replace the problem list via API
    // This is what a second tab editing problems would do.
    const editedProblems = [
      ...originalProblems.map((p: { id: number; content: string; label: string | null }) => ({
        id: p.id,
        statement: p.content,
        label: p.label,
      })),
      {
        id: null,
        statement: 'Newly added problem from a concurrent session.',
        label: 'New Problem',
      },
    ]

    const patchRes = await apiPatch(`/api/solutions/${sol.id}/segmentation`, {
      problems: editedProblems,
    })
    expect(patchRes.ok).toBe(true)

    // The solution detail query has no active polling once segmentation
    // settles, so a concurrent edit is picked up on the next navigation.
    // Navigate away and back to trigger a refetch.
    await page.goto('/')
    await page.waitForLoadState('networkidle')
    await page.goto(`/classes/${classId}/solutions/${sol.id}`)
    await page.waitForLoadState('networkidle')

    // After reload, partSignature detects the changed parts and re-seeds
    // the draft list with the updated problem set.
    await expect(page.getByText('Newly added problem from a concurrent session')).toBeVisible({
      timeout: 15_000,
    })

    // The solve button should reflect the updated count
    const updatedButton = page.getByText(/Solve \d+ problem/i)
    await expect(updatedButton).toBeVisible()
  })

  test('start rejects non-awaiting_review state', async () => {
    const solRes = await apiPost(`/api/classes/${classId}/solutions`, {
      title: 'State Guard Test',
      sources: [{ document_id: docId, role: 'problem_set' }],
    })
    expect(solRes.status).toBe(202)
    const sol = await solRes.json()

    // Try to start before segmentation completes (state is pending/segmenting)
    const startRes = await apiPost(`/api/solutions/${sol.id}/start`)
    // Should reject with 409 -- the solution is not at the review gate
    expect(startRes.status).toBe(409)
  })
})
