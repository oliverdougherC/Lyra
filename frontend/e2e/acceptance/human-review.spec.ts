/**
 * Workspace hunk confirmation boundary through the real browser (PLA-303).
 *
 * Proves: workspace change proposals display hunks in the contextual work surface,
 * the student can accept or reject through the browser UI, accepted hunks
 * land on disk, and stale hunks (file changed after proposal) are rejected
 * rather than silently applied.
 *
 * Uses the real workspace attach, change-proposal, and confirmation APIs
 * through the contextual work surface at /classes/{classId}/chat?session={sessionId}.
 */

import { test, expect } from '@playwright/test'
import { realpathSync } from 'node:fs'
import { mkdtemp, writeFile, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  apiGet,
  apiPost,
  apiPatch,
  createClass,
  createSession,
  clearTutorState,
  BACKEND,
} from './helpers'

test.describe('Workspace hunk confirmation boundary (PLA-303)', () => {
  let classId: number
  let workspaceDir: string

  test.beforeAll(async () => {
    const cls = await createClass('Acceptance: Workspace Review')
    classId = cls.id

    workspaceDir = realpathSync(await mkdtemp(join(tmpdir(), 'lyra-ws-review-')))
    await writeFile(join(workspaceDir, 'greet.py'), 'print("hello world")\n')

    // Attach workspace and enable change proposals
    const attachRes = await fetch(`${BACKEND}/api/classes/${classId}/workspace`, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        'X-Lyra-Client': 'acceptance-test',
      },
      body: JSON.stringify({ root_path: workspaceDir }),
    })
    expect(attachRes.status).toBe(201)

    await apiPatch(`/api/classes/${classId}/workspace/grants`, {
      read_enabled: true,
      change_proposals_enabled: true,
    })
  })

  test.afterEach(async () => {
    await clearTutorState()
  })

  test('browser: accept workspace change hunks and verify on disk', async ({ page }) => {
    const session = await createSession(classId)

    // Read current file state
    const readRes = await apiGet(
      `/api/classes/${classId}/sessions/${session.id}/workspace/read?path=greet.py`,
    )
    const fileData = await readRes.json()

    // Create a change proposal via API
    const changeRes = await apiPost(
      `/api/classes/${classId}/sessions/${session.id}/workspace/changes`,
      {
        relative_path: 'greet.py',
        observed_base_hash: fileData.sha256,
        proposed_content: 'print("hello acceptance")\n',
        rationale: 'Update greeting message',
      },
    )
    expect(changeRes.status).toBe(201)

    // Navigate to the agent chat page with the session in the URL
    await page.goto(`/classes/${classId}/chat?session=${session.id}`)
    await page.waitForLoadState('networkidle')

    // The contextual work surface renders the change card directly - there is no panel to open.

    // The workspace change review card should be visible
    const changeCard = page.locator('[aria-label="Workspace change for greet.py"]')
    await expect(changeCard).toBeVisible({ timeout: 15_000 })

    // Verify the diff is displayed with hunk content
    await expect(changeCard.getByText('greet.py')).toBeVisible()
    await expect(changeCard.getByText('Update greeting message')).toBeVisible()

    // Click "Accept remaining" to accept all hunks through the browser UI
    const acceptBtn = changeCard.getByRole('button', { name: /Accept remaining/i })
    await expect(acceptBtn).toBeVisible({ timeout: 5_000 })
    await acceptBtn.click()

    // Wait for the change to be applied: the result settles OUT of the live band (which
    // carries only outstanding work) into the collapsed Details audit, where the terminal
    // state stays findable.
    await expect(changeCard).toBeHidden({ timeout: 10_000 })
    await page
      .locator('[aria-label="Agent work"]')
      .getByRole('button', { name: /Details/i })
      .click()
    await expect(page.getByText('Applied', { exact: true })).toBeVisible()

    // Verify the file on disk was modified
    const diskContent = await readFile(join(workspaceDir, 'greet.py'), 'utf-8')
    expect(diskContent).toContain('hello acceptance')
  })

  test('browser: reject workspace change through UI leaves file unchanged', async ({ page }) => {
    // Reset file
    await writeFile(join(workspaceDir, 'greet.py'), 'print("hello acceptance")\n')

    const session = await createSession(classId)
    const readRes = await apiGet(
      `/api/classes/${classId}/sessions/${session.id}/workspace/read?path=greet.py`,
    )
    const fileData = await readRes.json()

    await apiPost(`/api/classes/${classId}/sessions/${session.id}/workspace/changes`, {
      relative_path: 'greet.py',
      observed_base_hash: fileData.sha256,
      proposed_content: 'print("rejected change")\n',
      rationale: 'This should be rejected',
    })

    await page.goto(`/classes/${classId}/chat?session=${session.id}`)
    await page.waitForLoadState('networkidle')

    const changeCard = page.locator('[aria-label="Workspace change for greet.py"]')
    await expect(changeCard).toBeVisible({ timeout: 15_000 })

    // Click "Reject proposal"
    const rejectBtn = changeCard.getByRole('button', { name: /Reject proposal/i })
    await expect(rejectBtn).toBeVisible({ timeout: 5_000 })
    await rejectBtn.click()

    // The rejection settles out of the live band: the card leaves the top work band, and
    // its terminal state stays findable in the collapsed Details audit.
    await expect(changeCard).toBeHidden({ timeout: 10_000 })
    await page
      .locator('[aria-label="Agent work"]')
      .getByRole('button', { name: /Details/i })
      .click()
    await expect(page.locator('[data-slot="badge"]', { hasText: 'Rejected' })).toBeVisible({
      timeout: 10_000,
    })

    // File on disk should be unchanged
    const diskContent = await readFile(join(workspaceDir, 'greet.py'), 'utf-8')
    expect(diskContent).toBe('print("hello acceptance")\n')
  })

  test('PLA-303: stale hunk is rejected when file changes between display and acceptance', async ({
    page,
  }) => {
    // Reset file to known state
    await writeFile(join(workspaceDir, 'greet.py'), 'print("fresh content")\n')

    const session = await createSession(classId)
    const readRes = await apiGet(
      `/api/classes/${classId}/sessions/${session.id}/workspace/read?path=greet.py`,
    )
    const fileData = await readRes.json()

    // Create a change proposal based on the current file state
    const changeRes = await apiPost(
      `/api/classes/${classId}/sessions/${session.id}/workspace/changes`,
      {
        relative_path: 'greet.py',
        observed_base_hash: fileData.sha256,
        proposed_content: 'print("stale proposal")\n',
        rationale: 'This will go stale',
      },
    )
    expect(changeRes.status).toBe(201)

    // Navigate to the review page so the hunks are displayed
    await page.goto(`/classes/${classId}/chat?session=${session.id}`)
    await page.waitForLoadState('networkidle')

    const changeCard = page.locator('[aria-label="Workspace change for greet.py"]')
    await expect(changeCard).toBeVisible({ timeout: 15_000 })

    // MUTATE the file on disk while the student is reviewing
    await writeFile(join(workspaceDir, 'greet.py'), 'print("externally modified")\n')

    // Try to accept -- the backend should detect the stale base hash
    const acceptBtn = changeCard.getByRole('button', { name: /Accept remaining/i })
    await expect(acceptBtn).toBeVisible({ timeout: 5_000 })
    await acceptBtn.click()

    // Staleness remains visible on the proposal after the transient toast disappears.
    const staleBadge = changeCard.locator('[data-slot="badge"]', {
      hasText: 'Stale',
    })
    await expect(staleBadge).toBeVisible({ timeout: 10_000 })
    await expect(
      changeCard.getByText('print("externally modified")', { exact: true }),
    ).toBeVisible()
    await expect(changeCard.getByText('print("stale proposal")', { exact: true })).toBeVisible()
    await expect(changeCard.getByRole('button', { name: /Accept remaining/i })).toHaveCount(0)

    // The stale proposal must NOT have been applied to disk
    const diskContent = await readFile(join(workspaceDir, 'greet.py'), 'utf-8')
    expect(diskContent).toBe('print("externally modified")\n')
  })
})
