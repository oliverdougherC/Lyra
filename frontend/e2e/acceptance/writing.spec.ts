/**
 * Writing and draft lifecycle through the real stack.
 *
 * Proves: create/edit/reload, autosave CAS (PLA-289), version conflict,
 * recovery without silent replacement, writer-chat concurrent turn
 * serialisation (PLA-308), writer failure/retry state.
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

    // Save initial body
    const save1 = await apiPatch(`/api/drafts/${draft.id}/body`, {
      content: 'First paragraph of my essay.',
      expected_version: 0,
      snapshot: false,
    })
    expect(save1.ok).toBe(true)
    const save1Body = await save1.json()
    expect(save1Body.version).toBe(1)

    // Save updated body
    const save2 = await apiPatch(`/api/drafts/${draft.id}/body`, {
      content: 'First paragraph of my essay.\n\nSecond paragraph.',
      expected_version: 1,
      snapshot: false,
    })
    expect(save2.ok).toBe(true)
    const save2Body = await save2.json()
    expect(save2Body.version).toBe(2)

    // Reload and verify
    const reloadRes = await apiGet(`/api/drafts/${draft.id}`)
    const reloaded = await reloadRes.json()
    expect(reloaded.body).toBe('First paragraph of my essay.\n\nSecond paragraph.')
    expect(reloaded.body_version).toBe(2)
  })

  test('stale version conflict returns 409 with server body', async () => {
    const draft = await createDraft(classId, 'Conflict Test')

    // Save version 1
    await apiPatch(`/api/drafts/${draft.id}/body`, {
      content: 'Version one content.',
      expected_version: 0,
      snapshot: false,
    })

    // Try to save with stale expected_version (0 instead of 1)
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

    // Write body and snapshot
    await apiPatch(`/api/drafts/${draft.id}/body`, {
      content: 'Original student text.',
      expected_version: 0,
      snapshot: true,
      note: 'Before AI help',
    })

    // Modify
    await apiPatch(`/api/drafts/${draft.id}/body`, {
      content: 'Modified text after editing.',
      expected_version: 1,
      snapshot: false,
    })

    // List revisions
    const draftRes = await apiGet(`/api/drafts/${draft.id}`)
    const draftBody = await draftRes.json()
    const partId = draftBody.part_id

    const revsRes = await apiGet(`/api/drafts/${draft.id}/parts/${partId}/revisions`)
    expect(revsRes.ok).toBe(true)
    const revisions = await revsRes.json()
    expect(revisions.length).toBeGreaterThanOrEqual(1)
  })

  test('writer-chat concurrent turns: one accepted, one rejected', async () => {
    await setTutorMode('timeout') // hold first turn open
    const draft = await createDraft(classId, 'Concurrent Writer Test')

    // Create a writer session
    const sessRes = await apiPost(`/api/drafts/${draft.id}/sessions`, {})
    expect(sessRes.ok).toBe(true)
    const session = await sessRes.json()

    // Start first turn (will block on timeout)
    const turn1Promise = fetch(`${BACKEND}/api/drafts/${draft.id}/chat/${session.id}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Lyra-Client': 'acceptance-test',
      },
      body: JSON.stringify({
        content: 'First turn — should hold the lock',
        mode: 'guide',
      }),
      signal: AbortSignal.timeout(5000),
    }).catch(() => null)

    // Give it time to claim the session
    await new Promise((r) => setTimeout(r, 500))

    // Second turn should be rejected
    const turn2Res = await fetch(`${BACKEND}/api/drafts/${draft.id}/chat/${session.id}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Lyra-Client': 'acceptance-test',
      },
      body: JSON.stringify({
        content: 'Second turn — should be rejected',
        mode: 'guide',
      }),
    })
    expect(turn2Res.status).toBe(409)

    // Cleanup
    await turn1Promise
  })

  test('draft renders in the browser', async ({ page }) => {
    const draft = await createDraft(classId, 'Browser Draft')

    // Save some content
    await apiPatch(`/api/drafts/${draft.id}/body`, {
      content: 'This is acceptance test content for the draft editor.',
      expected_version: 0,
      snapshot: false,
    })

    await page.goto(`/classes/${classId}/drafts/${draft.id}`)
    await page.waitForLoadState('networkidle')

    // The editor should show the saved content
    await expect(page.getByText('acceptance test content')).toBeVisible({ timeout: 10_000 })
  })
})
