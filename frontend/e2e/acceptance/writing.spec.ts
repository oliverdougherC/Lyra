/**
 * Writing and draft lifecycle through the real stack.
 *
 * Proves: create/edit/reload, autosave CAS (PLA-289), version conflict with
 * server body, recovery without silent replacement, writer-chat concurrent
 * turn serialisation (PLA-308) with deterministic barriers, browser-driven
 * editor interaction.
 */

import { test, expect } from '@playwright/test'
import {
  apiGet,
  apiPost,
  apiPatch,
  createClass,
  createDraft,
  clearTutorState,
  setTutorMode,
  waitForBarrier,
  releaseBarrier,
  BACKEND,
} from './helpers'

test.describe('Writing', () => {
  let classId: number

  test.beforeAll(async () => {
    const cls = await createClass('Acceptance: Writing')
    classId = cls.id
  })

  test.afterEach(async () => {
    await clearTutorState()
  })

  test('create a draft and verify empty body', async () => {
    const draft = await createDraft(classId, 'My Essay')

    const res = await apiGet(`/api/drafts/${draft.id}`)
    expect(res.ok).toBe(true)
    const body = await res.json()
    expect(body.title).toBe('My Essay')
    expect(body.body_version).toBe(0)
  })

  test('save body with CAS and reload to verify persistence', async () => {
    const draft = await createDraft(classId, 'Persistence Test')

    const save1 = await apiPatch(`/api/drafts/${draft.id}/body`, {
      content: 'First paragraph of my essay.',
      expected_version: 0,
      snapshot: false,
    })
    expect(save1.ok).toBe(true)
    const save1Body = await save1.json()
    expect(save1Body.version).toBe(1)

    const save2 = await apiPatch(`/api/drafts/${draft.id}/body`, {
      content: 'First paragraph of my essay.\n\nSecond paragraph.',
      expected_version: 1,
      snapshot: false,
    })
    expect(save2.ok).toBe(true)
    const save2Body = await save2.json()
    expect(save2Body.version).toBe(2)

    const reloadRes = await apiGet(`/api/drafts/${draft.id}`)
    const reloaded = await reloadRes.json()
    expect(reloaded.body).toBe('First paragraph of my essay.\n\nSecond paragraph.')
    expect(reloaded.body_version).toBe(2)
  })

  test('stale version conflict returns 409 with server body', async () => {
    const draft = await createDraft(classId, 'Conflict Test')

    await apiPatch(`/api/drafts/${draft.id}/body`, {
      content: 'Version one content.',
      expected_version: 0,
      snapshot: false,
    })

    const conflictRes = await apiPatch(`/api/drafts/${draft.id}/body`, {
      content: 'Conflicting content from stale client.',
      expected_version: 0,
      snapshot: false,
    })
    expect(conflictRes.status).toBe(409)
    const conflict = await conflictRes.json()
    expect(conflict.code).toBe('stale_body_version')
    expect(conflict.current_version).toBe(1)
    expect(conflict.server_body).toBe('Version one content.')
  })

  test('snapshot creates a recoverable revision', async () => {
    const draft = await createDraft(classId, 'Snapshot Test')

    await apiPatch(`/api/drafts/${draft.id}/body`, {
      content: 'Original student text.',
      expected_version: 0,
      snapshot: true,
      note: 'Before AI help',
    })

    await apiPatch(`/api/drafts/${draft.id}/body`, {
      content: 'Modified text after editing.',
      expected_version: 1,
      snapshot: false,
    })

    const draftRes = await apiGet(`/api/drafts/${draft.id}`)
    const draftBody = await draftRes.json()
    const partId = draftBody.part_id

    const revsRes = await apiGet(`/api/drafts/${draft.id}/parts/${partId}/revisions`)
    expect(revsRes.ok).toBe(true)
    const revisions = await revsRes.json()
    expect(revisions.length).toBeGreaterThanOrEqual(1)
  })

  test('PLA-308: writer-chat concurrent turns with deterministic barrier', async () => {
    // Use barrier mode so the first turn is deterministically held until we release it
    await setTutorMode('barrier')
    const draft = await createDraft(classId, 'Concurrent Writer Test')

    const sessRes = await apiPost(`/api/drafts/${draft.id}/sessions`, {})
    expect(sessRes.ok).toBe(true)
    const session = await sessRes.json()

    // Start first turn -- it will be held at the tutor barrier
    const turn1Promise = fetch(`${BACKEND}/api/drafts/${draft.id}/chat/${session.id}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Lyra-Client': 'acceptance-test',
      },
      body: JSON.stringify({
        content: 'First turn -- should hold the lock',
        mode: 'guide',
      }),
    })

    // Wait for the barrier to confirm the request arrived
    await waitForBarrier()

    // Second turn should be rejected because first turn holds the claim
    const turn2Res = await fetch(`${BACKEND}/api/drafts/${draft.id}/chat/${session.id}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Lyra-Client': 'acceptance-test',
      },
      body: JSON.stringify({
        content: 'Second turn -- should be rejected',
        mode: 'guide',
      }),
    })
    expect(turn2Res.status).toBe(409)

    // Release the barrier so the first turn completes cleanly
    await releaseBarrier('First turn released.')
    const turn1Res = await turn1Promise
    expect(turn1Res.status).toBe(200)
  })

  test('browser: draft editor loads saved content', async ({ page }) => {
    const draft = await createDraft(classId, 'Browser Draft')

    await apiPatch(`/api/drafts/${draft.id}/body`, {
      content: 'This is acceptance test content for the draft editor.',
      expected_version: 0,
      snapshot: false,
    })

    await page.goto(`/classes/${classId}/drafts/${draft.id}`)
    await page.waitForLoadState('networkidle')

    await expect(page.getByText('acceptance test content')).toBeVisible({ timeout: 10_000 })
  })

  test('browser: conflict dialog appears on stale version save', async ({ page }) => {
    const draft = await createDraft(classId, 'Browser Conflict Test')

    // Save initial content
    await apiPatch(`/api/drafts/${draft.id}/body`, {
      content: 'Initial content.',
      expected_version: 0,
      snapshot: false,
    })

    // Load the draft in the browser
    await page.goto(`/classes/${classId}/drafts/${draft.id}`)
    await page.waitForLoadState('networkidle')
    await expect(page.getByText('Initial content')).toBeVisible({ timeout: 10_000 })

    // Simulate a concurrent save from another client (bump the version)
    await apiPatch(`/api/drafts/${draft.id}/body`, {
      content: 'Content updated by another tab.',
      expected_version: 1,
      snapshot: false,
    })

    // The browser's next autosave should detect the 409 conflict.
    // Type something to trigger the save engine.
    const editor = page.locator('[aria-label="Draft document"]')
    await expect(editor).toBeVisible({ timeout: 10_000 })
    await editor.click()
    await page.keyboard.type(' appended text')

    // Wait for the conflict dialog to appear (deterministic: the save engine
    // fires on debounce, gets 409, enters 'conflict' state, dialog renders)
    await expect(page.getByText('This draft was changed somewhere else')).toBeVisible({
      timeout: 10_000,
    })

    // Verify the server still has the concurrent update (not silently replaced)
    const serverRes = await apiGet(`/api/drafts/${draft.id}`)
    const serverDraft = await serverRes.json()
    expect(serverDraft.body).toBe('Content updated by another tab.')
  })
})
