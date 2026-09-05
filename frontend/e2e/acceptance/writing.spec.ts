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
  apiPut,
  createClass,
  createDraft,
  clearTutorState,
  setTutorMode,
  waitForBarrier,
  releaseBarrier,
  readSSEFrames,
  getTutorRequests,
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

  test('writer tools fit short and narrow viewports without hiding the composer', async ({
    page,
  }) => {
    const draft = await createDraft(classId, 'Compact writer verification')
    const brief = await apiPut(`/api/drafts/${draft.id}/brief`, {
      assignment_type: 'Comparative essay',
      summary: 'Explain the first and second laws using a worked example and course readings.',
      audience: 'First-year physics students',
      length_target: '1,500 words',
    })
    expect(brief.ok).toBe(true)
    await page.goto(`/#/classes/${classId}/drafts/${draft.id}`)
    const composer = page.getByRole('textbox', { name: 'Message Lyra', exact: true })
    for (const viewport of [
      { width: 640, height: 360 },
      { width: 375, height: 667 },
      { width: 1280, height: 720 },
    ]) {
      await page.setViewportSize(viewport)
      await expect(composer).toBeVisible()
      await expect
        .poll(async () => {
          const box = await composer.boundingBox()
          return (
            box !== null &&
            box.height >= 36 &&
            box.y >= 0 &&
            box.y + box.height <= viewport.height &&
            box.x >= 0 &&
            box.x + box.width <= viewport.width
          )
        })
        .toBe(true)
      await composer.fill('Keep this question while I inspect the document.')
      await expect(page.getByRole('button', { name: 'Send message', exact: true })).toBeEnabled()
    }
    await page.setViewportSize({ width: 640, height: 360 })
    await page.getByRole('combobox', { name: 'Draft tool', exact: true }).click()
    await page.getByRole('option', { name: 'Document', exact: true }).click()
    await expect(page.getByRole('textbox', { name: 'Draft document', exact: true })).toBeVisible()
    await page.getByRole('combobox', { name: 'Draft tool', exact: true }).click()
    await page.getByRole('option', { name: 'Assistant', exact: true }).click()
    await expect(composer).toHaveValue('Keep this question while I inspect the document.')
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

  test('snapshot creates a recoverable revision', async ({ page }) => {
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

    // Rendered provenance (writer simplification pass): in the history sheet, versions are
    // labelled by who wrote them - this student snapshot reads "Your edit", while model
    // whole-document generations would read "Generated" in the same list (the label split
    // is unit-pinned in tests/revision-history.test.tsx).
    await page.goto(`/classes/${classId}/drafts/${draft.id}`)
    await page.waitForLoadState('networkidle')
    await page.locator('[aria-label="Draft tool"]').click()
    await page.getByRole('option', { name: 'History' }).click()
    const historySheet = page.locator('[data-slot="sheet-content"]')
    await expect(historySheet.getByText('Your edit')).toBeVisible()
    await expect(historySheet.getByText('Generated')).toHaveCount(0)
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

  test('PLA-310: a REAL durable effect lands before the turn fails, then retry is refused', async () => {
    // The fixture drives the production writer tool loop deterministically: round one
    // issues a real `save_brief` call (a genuine durable effect that commits through the
    // production tool path and links to the attempt), then the follow-up model round fails.
    // This proves PLA-310's retry guard against a REAL production failure sequence -- not
    // an ownership row manufactured from an acceptance-only endpoint.
    await setTutorMode('writer-effect-then-fail')
    const draft = await createDraft(classId, 'Durable Effect Failure Test')

    const sessRes = await apiPost(`/api/drafts/${draft.id}/sessions`, {})
    expect(sessRes.ok).toBe(true)
    const session = await sessRes.json()

    // Start the writer turn. It will land a real brief, then fail on the next round.
    const turn1Res = await fetch(`${BACKEND}/api/drafts/${draft.id}/chat/${session.id}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Lyra-Client': 'acceptance-test',
      },
      body: JSON.stringify({
        content: 'Please record a brief for this essay before you continue.',
        mode: 'guide',
      }),
    })
    expect(turn1Res.status).toBe(200)

    // Consume the SSE stream. The real effect lands first (an activity/brief frame), then
    // the turn reports a failed attempt (an error frame).
    const frames = await readSSEFrames(turn1Res)
    const sawBrief = frames.some((f) => f.type === 'brief')
    const sawError = frames.some((f) => f.type === 'error')
    expect(sawBrief, 'the real save_brief effect should have landed before the failure').toBe(true)
    expect(sawError, 'the attempt should report a failed turn after the effect landed').toBe(true)

    // The model was called exactly TWICE: once for the tool call (save_brief), once for
    // the follow-up round that fails. No more, no less.
    const modelCalls = (await getTutorRequests()).length
    expect(modelCalls).toBe(2)

    // Poll for the attempt state to settle rather than sleeping a fixed duration.
    let attemptData: { found: boolean; id: number; state: string } | null = null
    const settleDeadline = Date.now() + 10_000
    while (Date.now() < settleDeadline) {
      const attemptRes = await fetch(`${BACKEND}/_acceptance/writer-latest-attempt/${session.id}`, {
        headers: { 'X-Lyra-Client': 'acceptance-test' },
      })
      if (attemptRes.ok) {
        const data = (await attemptRes.json()) as { found: boolean; id: number; state: string }
        if (data.found && data.state === 'failed') {
          attemptData = data
          break
        }
      }
      await new Promise((r) => setTimeout(r, 200))
    }
    expect(attemptData, 'attempt should settle to failed state').toBeTruthy()
    expect(attemptData!.state).toBe('failed')
    const attemptId = attemptData!.id

    // The real durable effect E exists EXACTLY ONCE, and its ownership row belongs to this
    // attempt (linked through the production tool path, not injected).
    const targetsRes = await fetch(`${BACKEND}/_acceptance/writer-attempt-targets/${attemptId}`, {
      headers: { 'X-Lyra-Client': 'acceptance-test' },
    })
    const targets = (await targetsRes.json()) as { targets: Array<{ target_kind: string }> }
    expect(targets.targets.length).toBe(1)
    expect(targets.targets[0].target_kind).toBe('brief')

    // The exact brief content matches the fixture's deterministic save_brief call.
    const briefRes = await apiGet(`/api/drafts/${draft.id}/brief`)
    expect(briefRes.ok).toBe(true)
    const brief = (await briefRes.json()) as { summary?: string; assignment_type?: string }
    expect(brief.summary).toBe(
      'An acceptance-test essay on thermodynamics for the Fall 2026 readiness pass.',
    )
    expect(brief.assignment_type).toBe('essay')

    // Retry must be refused by PLA-310 because E already landed, with the structured code.
    const retryRes = await fetch(`${BACKEND}/api/drafts/${draft.id}/chat/${session.id}/retry`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Lyra-Client': 'acceptance-test',
      },
    })
    expect(retryRes.status).toBe(409)
    const retryBody = (await retryRes.json()) as { code?: string }
    expect(retryBody.code).toBe('writer_retry_has_effects')

    // No second model/tool execution duplicated E: still exactly one target on the attempt,
    // and the model was NOT called again (the retry was refused before any model invocation).
    const targets2Res = await fetch(`${BACKEND}/_acceptance/writer-attempt-targets/${attemptId}`, {
      headers: { 'X-Lyra-Client': 'acceptance-test' },
    })
    const targets2 = (await targets2Res.json()) as { targets: Array<unknown> }
    expect(targets2.targets.length).toBe(1)
    expect((await getTutorRequests()).length).toBe(modelCalls)

    // E remains visible through the product API after a simulated "reload" (re-fetch).
    const briefAfterRetry = await apiGet(`/api/drafts/${draft.id}/brief`)
    expect(briefAfterRetry.ok).toBe(true)
    const briefReloaded = (await briefAfterRetry.json()) as { summary?: string }
    expect(briefReloaded.summary).toBe(
      'An acceptance-test essay on thermodynamics for the Fall 2026 readiness pass.',
    )
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
