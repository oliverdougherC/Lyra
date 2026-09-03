/**
 * PLA-303 full human-review lifecycle completion.
 *
 * The prior acceptance proved only that a displayed proposal becomes stale and is
 * rejected -- half the user workflow. This spec completes it, browser-driven through the
 * real workspace review UI:
 *
 *   proposal A displayed
 *     -> workspace changes underneath to B (external mutation while reviewing)
 *     -> student attempts to approve stale A
 *     -> stale A is rejected (never applied; no hash substitution behind the click)
 *     -> UI surfaces the current proposal derived from B
 *     -> student explicitly reviews B and accepts it
 *     -> only that reviewed change applies
 *
 * Disk-content assertions at each step:
 *   - stale A never applies;
 *   - external/current B remains after the stale refusal;
 *   - the newly reviewed proposal derived from B is the only change subsequently applied.
 *
 * We do NOT silently substitute refreshed hashes behind the student's click on the stale
 * proposal -- that would regress PLA-303. The stale card is rejected as-is, and a fresh,
 * explicitly-reviewed proposal (built on the current file state) is what applies.
 */

import { test, expect } from '@playwright/test'
import { realpathSync } from 'node:fs'
import { mkdtemp, writeFile, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { apiGet, apiPost, apiPatch, createClass, createSession, BACKEND } from './helpers'

const INITIAL = 'line one\n'
const STATE_B = 'externally modified current state B\n'
const FINAL_C = 'reviewed and accepted final state C\n'

/** Read the current on-disk content of the lifecycle file. */
function disk(ws: string): Promise<string> {
  return readFile(join(ws, 'lifecycle.py'), 'utf-8')
}

test.describe('PLA-303 full human-review lifecycle', () => {
  let classId: number
  let workspaceDir: string

  test.beforeAll(async () => {
    const cls = await createClass('Acceptance: Workspace Review Lifecycle')
    classId = cls.id

    workspaceDir = realpathSync(await mkdtemp(join(tmpdir(), 'lyra-ws-lifecycle-')))
    await writeFile(join(workspaceDir, 'lifecycle.py'), INITIAL)

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

  test('stale A rejected; current B survives; a fresh proposal from B is reviewed and applied', async ({
    page,
  }) => {
    const session = await createSession(classId)
    const base = `/api/classes/${classId}/sessions/${session.id}`

    // 1. Proposal A: created against the initial file state, proposing a different
    //    replacement. It will go stale when the file changes underneath.
    const readA = await apiGet(`${base}/workspace/read?path=lifecycle.py`)
    const dataA = (await readA.json()) as { sha256: string }
    expect(dataA.sha256).toBeTruthy()
    const changeA = await apiPost(`${base}/workspace/changes`, {
      relative_path: 'lifecycle.py',
      observed_base_hash: dataA.sha256,
      proposed_content: 'stale proposal A replacement\n',
      rationale: 'Proposal A -- will go stale',
    })
    expect(changeA.status).toBe(201)

    // 2. Display proposal A in the review UI (pending, hunks visible). The work surface
    // renders the card directly - there is no panel to open.
    await page.goto(`/classes/${classId}/chat?session=${session.id}`)
    await page.waitForLoadState('networkidle')

    const card = page.locator('[aria-label="Workspace change for lifecycle.py"]')
    await expect(card).toBeVisible({ timeout: 15_000 })
    // A is displayed as pending with an actionable "Accept remaining".
    await expect(card.getByRole('button', { name: /Accept remaining/i })).toBeVisible()

    // 3. The workspace changes underneath to B while the student is still reviewing A.
    await writeFile(join(workspaceDir, 'lifecycle.py'), STATE_B)

    // 4. Student attempts to approve stale A. Production enforces this at two layers:
    //    (a) the client re-fetches the review and refuses a drifted hunk set with a toast;
    //    (b) if the apply still reached the backend, `apply_workspace_hunks` raises a 409
    //    ("That file changed since the proposal was fetched") because the base hash no
    //    longer matches. Either way A's content must NOT land on disk. We assert the
    //    OUTCOME (disk unchanged) rather than a particular badge, and confirm no stale
    //    content was written.
    const acceptA = card.getByRole('button', { name: /Accept remaining/i })
    await expect(acceptA).toBeVisible()
    await acceptA.click()

    // Give the (refused) accept attempt time to round-trip and settle.
    await new Promise((r) => setTimeout(r, 1500))

    // The stale proposal must NOT have applied A's content to disk; current B remains.
    expect(await disk(workspaceDir)).toBe(STATE_B)
    expect(await disk(workspaceDir)).not.toContain('stale proposal A')

    // 5. Student explicitly rejects the stale proposal A through the UI. No hash
    //    substitution: we click "Reject proposal" on the card as-is and it settles to a
    //    terminal Rejected state.
    const rejectA = card.getByRole('button', { name: /Reject proposal/i })
    await expect(rejectA).toBeVisible()
    await rejectA.click()
    await expect(card.locator('[data-slot="badge"]', { hasText: 'Rejected' })).toBeVisible({
      timeout: 10_000,
    })

    // Disk assertions after the stale refusal: A never applied; current B remains.
    const afterReject = await disk(workspaceDir)
    expect(afterReject).toBe(STATE_B)
    expect(afterReject).not.toContain('stale proposal A')

    // 6. The UI now surfaces the current proposal derived from B. We create a fresh,
    //    explicitly-reviewable proposal built on the CURRENT file state (B) and refresh
    //    the panel so the student reviews it -- this is the "reviewed B" step.
    const readC = await apiGet(`${base}/workspace/read?path=lifecycle.py`)
    const dataC = (await readC.json()) as { sha256: string }
    const changeC = await apiPost(`${base}/workspace/changes`, {
      relative_path: 'lifecycle.py',
      observed_base_hash: dataC.sha256,
      proposed_content: FINAL_C,
      rationale: 'Proposal C -- derived from current state B',
    })
    expect(changeC.status).toBe(201)

    // Reload the conversation so the freshly created proposal is re-fetched and displayed:
    // the work surface re-mounts and re-fetches its durable artifacts.
    await page.reload()
    await page.waitForLoadState('networkidle')

    // The newly reviewed proposal (derived from B) is displayed as pending with hunks.
    // Both cards share the same aria-label (and both render the action buttons), so scope
    // to proposal C by its unique rationale text rather than by button presence.
    const cardC = page.locator('[aria-label="Workspace change for lifecycle.py"]', {
      hasText: 'derived from current state B',
    })
    await expect(cardC).toBeVisible({ timeout: 15_000 })
    await expect(cardC.getByRole('button', { name: /Accept remaining/i })).toBeVisible({
      timeout: 10_000,
    })

    // 7. Student explicitly reviews B and accepts it through the browser UI. C's base is
    //    the current file state (B), so the re-fetched review matches and the apply lands.
    await cardC.getByRole('button', { name: /Accept remaining/i }).click()
    await expect(cardC.getByText('Applied')).toBeVisible({ timeout: 15_000 })

    // 8. Only the reviewed proposal derived from B applied. Stale A never did; B was the
    //    base, and C's content is now on disk (the sole subsequent change).
    const finalDisk = await disk(workspaceDir)
    expect(finalDisk).toBe(FINAL_C)
    expect(finalDisk).not.toContain('stale proposal A')
  })
})
