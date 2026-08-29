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
  readSSEFrames,
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

  test('writer retry after failure: no durable effects allows new attempt', async () => {
    await setTutorMode('error-before-stream')
    const draft = await createDraft(classId, 'Retry No Effects Test')

    const sessRes = await apiPost(`/api/drafts/${draft.id}/sessions`, {})
    expect(sessRes.ok).toBe(true)
    const session = await sessRes.json()

    // Send a turn that will fail pre-stream (no durable effects produced)
    const turn1Res = await fetch(`${BACKEND}/api/drafts/${draft.id}/chat/${session.id}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Lyra-Client': 'acceptance-test',
      },
      body: JSON.stringify({
        content: 'This turn should fail without effects',
        mode: 'guide',
      }),
    })
    // Consume the SSE stream (error frame expected, then stream ends)
    await readSSEFrames(turn1Res)

    // Verify messages: the user message was persisted
    const msgsRes = await apiGet(`/api/sessions/${session.id}/messages`)
    expect(msgsRes.ok).toBe(true)
    const msgs = await msgsRes.json()
    const userMsgs = msgs.filter((m: { role: string }) => m.role === 'user')
    expect(userMsgs.length).toBe(1)
    expect(userMsgs[0].content).toBe('This turn should fail without effects')

    // Retry: since there are no durable effects, a new attempt should be created
    await setTutorMode('success')
    const retryRes = await fetch(`${BACKEND}/api/drafts/${draft.id}/chat/${session.id}/retry`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Lyra-Client': 'acceptance-test',
      },
    })
    expect(retryRes.status).toBe(200)
    const retryFrames = await readSSEFrames(retryRes)
    const retryTokens = retryFrames.filter((f) => f.type === 'token')
    expect(retryTokens.length).toBeGreaterThan(0)

    // No duplicate user message -- still only one
    const msgs2Res = await apiGet(`/api/sessions/${session.id}/messages`)
    const msgs2 = await msgs2Res.json()
    const userMsgs2 = msgs2.filter((m: { role: string }) => m.role === 'user')
    expect(userMsgs2.length).toBe(1)
  })

  test('writer retry after success: replays stored response', async () => {
    await setTutorMode('success')
    const draft = await createDraft(classId, 'Retry Replay Test')

    const sessRes = await apiPost(`/api/drafts/${draft.id}/sessions`, {})
    expect(sessRes.ok).toBe(true)
    const session = await sessRes.json()

    // Send a successful turn
    const turn1Res = await fetch(`${BACKEND}/api/drafts/${draft.id}/chat/${session.id}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Lyra-Client': 'acceptance-test',
      },
      body: JSON.stringify({
        content: 'Help me with this essay',
        mode: 'guide',
      }),
    })
    expect(turn1Res.status).toBe(200)
    await readSSEFrames(turn1Res)

    // Record the assistant response
    const msgsRes = await apiGet(`/api/sessions/${session.id}/messages`)
    const msgs = await msgsRes.json()
    const assistant1 = msgs.filter((m: { role: string }) => m.role === 'assistant')
    expect(assistant1.length).toBe(1)
    const originalContent = assistant1[0].content

    // Retry: should replay the stored response without re-running the model
    const retryRes = await fetch(`${BACKEND}/api/drafts/${draft.id}/chat/${session.id}/retry`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Lyra-Client': 'acceptance-test',
      },
    })
    expect(retryRes.status).toBe(200)
    await readSSEFrames(retryRes)

    // The replayed response should be the same content
    const msgs2Res = await apiGet(`/api/sessions/${session.id}/messages`)
    const msgs2 = await msgs2Res.json()
    const assistant2 = msgs2.filter((m: { role: string }) => m.role === 'assistant')
    expect(assistant2.length).toBe(1)
    expect(assistant2[0].content).toBe(originalContent)
  })

  test('PLA-310: durable effect then failure blocks retry', async () => {
    await setTutorMode('error-before-stream')
    const draft = await createDraft(classId, 'Durable Effect Failure Test')

    const sessRes = await apiPost(`/api/drafts/${draft.id}/sessions`, {})
    expect(sessRes.ok).toBe(true)
    const session = await sessRes.json()

    // Send a turn that fails (no durable effects produced by guide mode)
    const turn1Res = await fetch(`${BACKEND}/api/drafts/${draft.id}/chat/${session.id}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Lyra-Client': 'acceptance-test',
      },
      body: JSON.stringify({
        content: 'This turn will fail then get an injected effect',
        mode: 'guide',
      }),
    })
    await readSSEFrames(turn1Res)

    // Wait briefly for the attempt state to settle (the SSE error may race
    // with the database commit)
    await new Promise((r) => setTimeout(r, 500))

    // Find the failed attempt via the acceptance endpoint
    const attemptRes = await fetch(
      `${BACKEND}/_acceptance/writer-latest-attempt/${session.id}`,
      { headers: { 'X-Lyra-Client': 'acceptance-test' } },
    )
    const attemptBody = await attemptRes.text()
    expect(attemptRes.ok, `writer-latest-attempt returned ${attemptRes.status}: ${attemptBody}`).toBe(
      true,
    )
    const attemptData = JSON.parse(attemptBody)
    expect(attemptData.found, `attempt lookup returned: ${attemptBody}`).toBe(true)
    expect(attemptData.state).toBe('failed')
    const attemptId: number = attemptData.id

    // Inject a durable effect (simulates a tool that created a brief before failure)
    const injectRes = await fetch(`${BACKEND}/_acceptance/writer-inject-effect`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Lyra-Client': 'acceptance-test',
      },
      body: JSON.stringify({ attempt_id: attemptId, target_kind: 'brief', target_id: 99999 }),
    })
    if (!injectRes.ok) {
      const errBody = await injectRes.text()
      throw new Error(`writer-inject-effect failed ${injectRes.status}: ${errBody}`)
    }

    // Confirm the effect landed
    const targetsRes = await fetch(`${BACKEND}/_acceptance/writer-attempt-targets/${attemptId}`, {
      headers: { 'X-Lyra-Client': 'acceptance-test' },
    })
    const targets = await targetsRes.json()
    expect(targets.targets.length).toBe(1)
    expect(targets.targets[0].target_kind).toBe('brief')

    // Retry should be blocked by PLA-310 because the attempt has durable effects
    const retryRes = await fetch(`${BACKEND}/api/drafts/${draft.id}/chat/${session.id}/retry`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Lyra-Client': 'acceptance-test',
      },
    })
    expect(retryRes.status).toBe(409)
    const retryBody = await retryRes.json()
    expect(retryBody.code).toBe('writer_retry_has_effects')

    // Confirm the effect was not duplicated (still exactly one)
    const targets2Res = await fetch(`${BACKEND}/_acceptance/writer-attempt-targets/${attemptId}`, {
      headers: { 'X-Lyra-Client': 'acceptance-test' },
    })
    const targets2 = await targets2Res.json()
    expect(targets2.targets.length).toBe(1)
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
